"""Config and options flow for PC Power."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import AgentAuthError, AgentClient, AgentError
from .const import (
    CONF_DISKS,
    CONF_MACHINE_ID,
    CONF_TOKEN,
    DEFAULT_PORT,
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
    MONITOR_FIELD_PREFIX,
    OPT_OFFLINE_W,
    OPT_RGB_W,
    OPT_SCAN_INTERVAL,
    OPT_USB_OVERRIDES,
    USB_FIELD_PREFIX,
)
from .energy import (
    default_disks_w,
    estimate_monitors,
    monitor_default_w,
    monitor_keys,
    usb_default_w,
    usb_key,
    usb_label,
)


async def _probe(hass, host: str, port: int, token: str) -> dict:
    return await AgentClient(async_get_clientsession(hass), host, port, token).metrics()


class PcPowerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._name: str | None = None

    async def _try_create(self, host: str, port: int, token: str, errors: dict) -> ConfigFlowResult | None:
        try:
            data = await _probe(self.hass, host, port, token)
        except AgentAuthError:
            errors["base"] = "invalid_auth"
            return None
        except AgentError:
            errors["base"] = "cannot_connect"
            return None
        await self.async_set_unique_id(data["machine_id"])
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        return self.async_create_entry(
            title=data.get("hostname") or host,
            data={
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_TOKEN: token,
                CONF_MACHINE_ID: data["machine_id"],
                CONF_DISKS: data.get("disks") or {},
            },
            options={**DEFAULTS, OPT_DISKS_W: default_disks_w(data.get("disks"))},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if result := await self._try_create(
                user_input[CONF_HOST], user_input[CONF_PORT], user_input[CONF_TOKEN], errors
            ):
                return result
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
                vol.Required(CONF_TOKEN): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        props = discovery_info.properties
        machine_id = props.get("machine_id")
        if not machine_id:
            return self.async_abort(reason="not_supported")
        self._host = str(discovery_info.ip_address)
        self._port = discovery_info.port or DEFAULT_PORT
        self._name = props.get("hostname") or discovery_info.hostname.removesuffix(".local.")
        await self.async_set_unique_id(machine_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if result := await self._try_create(self._host, self._port, user_input[CONF_TOKEN], errors):
                return result
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            description_placeholders={"name": self._name, "host": f"{self._host}:{self._port}"},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PcPowerOptionsFlow()


class PcPowerOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    def _metrics(self) -> dict:
        coordinator = getattr(self.config_entry, "runtime_data", None)
        return getattr(coordinator, "metrics", None) or {}

    def _usb_devices(self) -> list[dict]:
        return self._metrics().get("usb") or []

    def _monitors(self) -> list[dict]:
        return self._metrics().get("monitors") or []

    async def _next_after_init(self) -> ConfigFlowResult:
        if self._usb_devices():
            return await self.async_step_usb()
        return await self._next_after_usb()

    async def _next_after_usb(self) -> ConfigFlowResult:
        if self._monitors():
            return await self.async_step_monitors()
        return self.async_create_entry(data=self._options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options = {**self.config_entry.options, **user_input}
            return await self._next_after_init()
        o = {**DEFAULTS, **self.config_entry.options}
        disks = self.config_entry.data.get(CONF_DISKS) or {}
        o.setdefault(OPT_DISKS_W, default_disks_w(disks))
        watts = vol.All(vol.Coerce(float), vol.Range(min=0, max=2000))
        schema = vol.Schema(
            {
                vol.Required(OPT_SCAN_INTERVAL, default=o[OPT_SCAN_INTERVAL]): vol.All(
                    vol.Coerce(int), vol.Range(min=2, max=300)
                ),
                vol.Required(OPT_DISKS_W, default=o[OPT_DISKS_W]): watts,
                vol.Required(OPT_CONTROLLER_W, default=o[OPT_CONTROLLER_W]): watts,
                vol.Required(OPT_FANS_W, default=o[OPT_FANS_W]): watts,
                vol.Required(OPT_FANS_DYNAMIC, default=o[OPT_FANS_DYNAMIC]): bool,
                vol.Required(OPT_RGB_W, default=o[OPT_RGB_W]): watts,
                vol.Required(OPT_BOARD_W, default=o[OPT_BOARD_W]): watts,
                vol.Required(OPT_EFFICIENCY, default=o[OPT_EFFICIENCY]): vol.All(
                    vol.Coerce(float), vol.Range(min=0.5, max=1.0)
                ),
                vol.Required(OPT_OFFLINE_W, default=o[OPT_OFFLINE_W]): watts,
                vol.Required(OPT_MONITOR_STANDBY_W, default=o[OPT_MONITOR_STANDBY_W]): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=20)
                ),
                vol.Required(OPT_GRACE_POLLS, default=o[OPT_GRACE_POLLS]): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=60)
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "hdd": str(disks.get("hdd", 0)),
                "ssd": str(disks.get("ssd", 0)),
                "nvme": str(disks.get("nvme", 0)),
            },
        )

    async def async_step_usb(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        devices = self._usb_devices()
        overrides: dict[str, float] = dict(self.config_entry.options.get(OPT_USB_OVERRIDES) or {})
        if user_input is not None:
            for dev in devices:
                key = usb_key(dev)
                field = USB_FIELD_PREFIX + key
                if field not in user_input:
                    continue
                value = round(float(user_input[field]), 2)
                # only store real overrides, so improved built-in estimates still apply later
                if value == usb_default_w(dev)[0]:
                    overrides.pop(key, None)
                else:
                    overrides[key] = value
            self._options[OPT_USB_OVERRIDES] = overrides
            return await self._next_after_usb()

        fields: dict = {}
        lines = []
        seen: set[str] = set()
        for dev in devices:
            key = usb_key(dev)
            if key in seen:
                continue
            seen.add(key)
            default, basis = usb_default_w(dev)
            current = overrides.get(key, default)
            fields[vol.Required(USB_FIELD_PREFIX + key, default=current)] = vol.All(
                vol.Coerce(float), vol.Range(min=0, max=15)
            )
            lines.append(f"- **{USB_FIELD_PREFIX + key}**: {usb_label(dev)} ({basis}, {default} W)")
        return self.async_show_form(
            step_id="usb",
            data_schema=vol.Schema(fields),
            description_placeholders={"devices": "\n".join(lines)},
        )

    async def async_step_monitors(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        monitors = self._monitors()
        keys = monitor_keys(monitors)
        overrides: dict[str, float] = dict(self.config_entry.options.get(OPT_MONITOR_OVERRIDES) or {})
        if user_input is not None:
            for key, mon in zip(keys, monitors):
                field = MONITOR_FIELD_PREFIX + key
                if field not in user_input:
                    continue
                value = round(float(user_input[field]), 1)
                if value == monitor_default_w(mon):
                    overrides.pop(key, None)
                else:
                    overrides[key] = value
            return self.async_create_entry(data={**self._options, OPT_MONITOR_OVERRIDES: overrides})

        _, rows = estimate_monitors(monitors, overrides)
        fields: dict = {}
        lines = []
        for key, mon, row in zip(keys, monitors, rows):
            fields[vol.Required(MONITOR_FIELD_PREFIX + key, default=overrides.get(key, monitor_default_w(mon)))] = vol.All(
                vol.Coerce(float), vol.Range(min=1, max=500)
            )
            lines.append(
                f"- **{MONITOR_FIELD_PREFIX + key}**: {row['device']}, now {row['state']}"
                f" at brightness {row['brightness']} -> {row['watts']} W"
            )
        return self.async_show_form(
            step_id="monitors",
            data_schema=vol.Schema(fields),
            description_placeholders={"monitors": "\n".join(lines)},
        )
