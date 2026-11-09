"""Polling coordinator for PC Power."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import AgentAuthError, AgentClient, AgentError, send_magic_packet
from .const import (
    CONF_TOKEN,
    DEFAULTS,
    DOMAIN,
    OPT_BOARD_W,
    OPT_CONTROLLER_W,
    OPT_DISKS_W,
    OPT_EFFICIENCY,
    OPT_FANS_DYNAMIC,
    OPT_FANS_W,
    OPT_GRACE_POLLS,
    OPT_MONITOR_OVERRIDES,
    OPT_MONITOR_STANDBY_W,
    OPT_OFFLINE_W,
    OPT_RGB_W,
    OPT_SCAN_INTERVAL,
    OPT_USB_OVERRIDES,
    WOL_PORT,
)
from .energy import (
    Accountant,
    AccountState,
    Profile,
    Reading,
    Snapshot,
    estimate_monitors,
    estimate_usb,
    fan_fraction,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1

type PcPowerConfigEntry = ConfigEntry[PcPowerCoordinator]


def _opt(entry: ConfigEntry, key: str):
    return entry.options.get(key, DEFAULTS.get(key))


def profile_from_entry(entry: ConfigEntry) -> Profile:
    return Profile(
        disks_w=float(_opt(entry, OPT_DISKS_W) or 0.0),
        controller_w=float(_opt(entry, OPT_CONTROLLER_W)),
        fans_w=float(_opt(entry, OPT_FANS_W)),
        fans_dynamic=bool(_opt(entry, OPT_FANS_DYNAMIC)),
        rgb_w=float(_opt(entry, OPT_RGB_W) or 0.0),
        board_w=float(_opt(entry, OPT_BOARD_W)),
        efficiency=float(_opt(entry, OPT_EFFICIENCY)),
        offline_w=float(_opt(entry, OPT_OFFLINE_W)),
        grace_polls=int(_opt(entry, OPT_GRACE_POLLS)),
    )


def _sum(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def reading_from_metrics(
    data: dict, now: float, usb_w: float = 0.0, fans_fraction: float | None = None, monitors_w: float = 0.0
) -> Reading:
    """Timestamps use HA's clock (not the agent's) so offline accounting never mixes clocks."""
    cpu = data.get("cpu") or {}
    gpus = [g for g in data.get("gpus") or [] if g.get("available")]
    cpu_ok = bool(cpu.get("available"))
    return Reading(
        session_id=data["session_id"],
        timestamp=now,
        cpu_w=cpu.get("power_w") if cpu_ok else None,
        cpu_j=cpu.get("energy_j") if cpu else None,
        gpu_w=_sum([g.get("power_w") for g in gpus]),
        # sum over *all* GPUs so a GPU flapping unavailable doesn't make the total jump
        gpu_j=_sum([g.get("energy_j") for g in data.get("gpus") or []]),
        usb_w=usb_w,
        fans_fraction=fans_fraction,
        monitors_w=monitors_w,
        awake_s=data.get("awake_s"),
    )


def inventory_from_metrics(data: dict, monitor_rows: list[dict], prev: dict) -> dict:
    """What we need to keep creating entities / waking the PC while it is off."""
    net = data.get("network") or {}
    ifaces = net.get("interfaces") or []
    primary = next((i for i in ifaces if i.get("name") == net.get("primary")), None)
    wired = [i for i in ifaces if i.get("type") == "ethernet" and i.get("mac")]
    # prefer interfaces reporting WoL support; unknown (None) still counts as a candidate
    wol_macs = [i["mac"] for i in wired if (i.get("wol") or {}).get("supported", True)]
    thermals = data.get("thermals") or {}
    fans_seen = set(prev.get("fans") or [])
    fans_seen |= {f["id"] for f in thermals.get("fans") or [] if (f.get("rpm") or 0) > 0}
    return {
        "hostname": data.get("hostname"),
        "agent_version": data.get("agent_version"),
        "capabilities": data.get("capabilities") or {},
        "wol_macs": wol_macs,
        "ipv4": [c for i in ifaces for c in i.get("ipv4") or []],
        "primary_ip": ((primary or {}).get("ipv4") or [None])[0],
        "monitors": [{"key": r["key"], "device": r["device"], "bus": r["bus"]} for r in monitor_rows]
        or (prev.get("monitors") or []),
        "fans": sorted(fans_seen),
        "nvme": [n["device"] for n in thermals.get("nvme") or []],
        "ram_sensors": len(thermals.get("ram_c") or []),
        "has_session": data.get("session") is not None,
        "has_gpu": bool(data.get("gpus")),
    }


class PcPowerCoordinator(DataUpdateCoordinator[Snapshot]):
    """Polls one agent and runs the energy accountant."""

    config_entry: PcPowerConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PcPowerConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=int(_opt(entry, OPT_SCAN_INTERVAL))),
            always_update=True,
        )
        self.client = AgentClient(
            async_get_clientsession(hass),
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_TOKEN],
        )
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.accountant = Accountant(profile_from_entry(entry))
        self.metrics: dict | None = None  # last raw agent payload (stale while offline)
        self.inventory: dict = {}
        self.usb_rows: list[dict] = []
        self.monitor_rows: list[dict] = []
        self.fans_fraction: float | None = None
        self._usb_overrides: dict = dict(entry.options.get(OPT_USB_OVERRIDES) or {})
        self._monitor_overrides: dict = dict(entry.options.get(OPT_MONITOR_OVERRIDES) or {})
        self._monitor_standby_w = float(_opt(entry, OPT_MONITOR_STANDBY_W))
        self._auth_failed = False

    # ------------------------------------------------------------------ persistence

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        account = data.get("account", data if "energy_wh" in data else None)  # v0.1/0.2 stored it flat
        self.accountant.state = AccountState.from_dict(account)
        self.inventory = data.get("inventory") or {}
        self._update_monitor_standby()

    def _data_to_save(self) -> dict:
        return {"account": self.accountant.state.to_dict(), "inventory": self.inventory}

    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 30)

    async def async_save_now(self) -> None:
        await self._store.async_save(self._data_to_save())

    def _update_monitor_standby(self) -> None:
        self.accountant.profile.monitors_off_w = self._monitor_standby_w * len(self.inventory.get("monitors") or [])

    # ------------------------------------------------------------------ polling

    @property
    def online(self) -> bool:
        return bool(self.data and self.data.online)

    def live(self) -> dict | None:
        """Agent payload, only while the PC is online."""
        return self.metrics if self.online else None

    async def _async_update_data(self) -> Snapshot:
        try:
            data = await self.client.metrics()
            usb_w, usb_rows = estimate_usb(data.get("usb"), self._usb_overrides)
            mon_w, mon_rows = estimate_monitors(data.get("monitors"), self._monitor_overrides, self._monitor_standby_w)
            st = self.accountant.state
            fans = (data.get("thermals") or {}).get("fans")
            frac, st.fan_max_rpm = fan_fraction(fans, st.fan_max_rpm)
            reading = reading_from_metrics(data, time.time(), usb_w, frac, mon_w)
        except AgentAuthError:
            if not self._auth_failed:
                _LOGGER.error("%s: agent rejected the token; reconfigure the integration", self.config_entry.title)
                self._auth_failed = True
            snap = self.accountant.on_failure(time.time())
        except (AgentError, KeyError, TypeError, ValueError) as err:
            _LOGGER.debug("%s: poll failed: %s", self.config_entry.title, err)
            snap = self.accountant.on_failure(time.time())
        else:
            self._auth_failed = False
            self.metrics = data
            self.usb_rows, self.monitor_rows, self.fans_fraction = usb_rows, mon_rows, frac
            self.inventory = inventory_from_metrics(data, mon_rows, self.inventory)
            self._update_monitor_standby()
            snap = self.accountant.on_reading(reading)
        self._save()
        return snap

    # ------------------------------------------------------------------ actions

    async def async_wake(self) -> None:
        macs = self.inventory.get("wol_macs") or []
        if not macs:
            raise AgentError("no wired network interface known for Wake-on-LAN")
        sent = await self.hass.async_add_executor_job(
            send_magic_packet, macs, self.inventory.get("ipv4") or [], WOL_PORT
        )
        _LOGGER.info("%s: sent %d Wake-on-LAN packet(s) to %s", self.config_entry.title, sent, ", ".join(macs))

    async def async_power(self, action: str) -> None:
        await self.client.power(action)
        _LOGGER.warning("%s: requested %s", self.config_entry.title, action)

    async def async_lock(self) -> None:
        await self.client.lock()
        # LockedHint flips a moment after the desktop has actually locked
        self.hass.loop.call_later(2, lambda: self.hass.async_create_task(self.async_request_refresh()))

    async def async_set_brightness(self, bus: int, value: int) -> None:
        await self.client.set_brightness(bus, value)
        for m in (self.metrics or {}).get("monitors") or []:
            if m.get("bus") == bus:
                m["brightness"] = value
        self.async_update_listeners()
