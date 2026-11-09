"""Sensors for PC Power."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PcPowerConfigEntry, PcPowerCoordinator
from .entity import PcPowerEntity

Coord = PcPowerCoordinator


def _r(v: float | None, n: int = 1) -> float | None:
    return None if v is None else round(v, n)


def _live(c: Coord, *path: str) -> Any:
    cur: Any = c.live()
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _gpu_max(c: Coord, key: str) -> float | None:
    vals = [g.get(key) for g in (_live(c, "gpus") or []) if g.get(key) is not None]
    return max(vals) if vals else None


def _idle_since(c: Coord) -> datetime | None:
    s = _live(c, "session") or {}
    ts = s.get("idle_since") if s.get("idle") else None
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


def _ram_max(c: Coord) -> float | None:
    vals = _live(c, "thermals", "ram_c") or []
    return max(vals) if vals else None


def _interfaces(c: Coord) -> dict | None:
    net = _live(c, "network")
    if not net:
        return None
    out = {}
    for i in net.get("interfaces") or []:
        wol = i.get("wol")
        out[i["name"]] = {
            "type": i.get("type"),
            "up": i.get("up"),
            "mac": i.get("mac"),
            "ipv4": i.get("ipv4"),
            "ipv6": i.get("ipv6"),
            "wake_on_lan": None if wol is None else ("enabled" if wol.get("enabled") else
                                                     "supported, disabled" if wol.get("supported") else "unsupported"),
        }
    return {"primary_interface": net.get("primary"), "interfaces": out}


@dataclass(frozen=True, kw_only=True)
class PcPowerSensorDescription(SensorEntityDescription):
    value: Callable[[Coord], Any]
    attrs: Callable[[Coord], dict | None] | None = None
    live_only: bool = True
    present: Callable[[Coord], bool] = lambda c: True


POWER = dict(device_class=SensorDeviceClass.POWER, state_class=SensorStateClass.MEASUREMENT,
             native_unit_of_measurement=UnitOfPower.WATT, suggested_display_precision=1, live_only=False)
ENERGY = dict(device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL_INCREASING,
              native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=3, live_only=False)
TEMP = dict(device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS, suggested_display_precision=0)
PCT = dict(state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=PERCENTAGE,
           suggested_display_precision=0)

SENSORS: tuple[PcPowerSensorDescription, ...] = (
    PcPowerSensorDescription(key="cpu_power", value=lambda c: _r(c.data.cpu_w), **POWER,
                             attrs=lambda c: {"source": _live(c, "cpu", "name"), "method": _live(c, "cpu", "method")}),
    PcPowerSensorDescription(key="gpu_power", value=lambda c: _r(c.data.gpu_w), **POWER,
                             present=lambda c: c.inventory.get("has_gpu", True),
                             attrs=lambda c: {"gpus": [f"{g.get('name')} ({g.get('method')})"
                                                       for g in _live(c, "gpus") or []],
                                              "power_limit_w": _gpu_max(c, "power_limit_w")}),
    PcPowerSensorDescription(key="usb_power", value=lambda c: _r(c.data.usb_w), **POWER,
                             attrs=lambda c: {"devices": [f"{r['device']}: {r['watts']} W ({r['basis']})"
                                                          for r in c.usb_rows]}),
    PcPowerSensorDescription(key="wall_power", value=lambda c: _r(c.data.wall_w), **POWER,
                             attrs=lambda c: {"fans_w": _r(c.data.fans_w), "fan_speed_fraction": c.fans_fraction,
                                              "static_w": _r(c.accountant.profile.static_w),
                                              "efficiency": c.accountant.profile.efficiency}),
    PcPowerSensorDescription(key="energy", value=lambda c: _r(c.data.energy_kwh, 5), **ENERGY),
    PcPowerSensorDescription(key="monitors_power", value=lambda c: _r(c.data.monitors_w), **POWER,
                             present=lambda c: bool(c.inventory.get("monitors")),
                             attrs=lambda c: {"monitors": [f"{r['device']}: {r['state']}, brightness "
                                                           f"{r['brightness']}, {r['watts']} W" for r in c.monitor_rows]}),
    PcPowerSensorDescription(key="monitors_energy", value=lambda c: _r(c.data.monitors_kwh, 5), **ENERGY,
                             present=lambda c: bool(c.inventory.get("monitors"))),
    PcPowerSensorDescription(key="cpu_temp", value=lambda c: _r(_live(c, "thermals", "cpu_temp_c")), **TEMP),
    PcPowerSensorDescription(key="gpu_temp", value=lambda c: _gpu_max(c, "temp_c"), **TEMP,
                             present=lambda c: c.inventory.get("has_gpu", True)),
    PcPowerSensorDescription(key="cpu_util", value=lambda c: _live(c, "cpu_util_pct"), **PCT),
    PcPowerSensorDescription(key="gpu_util", value=lambda c: _gpu_max(c, "util_pct"), **PCT,
                             present=lambda c: c.inventory.get("has_gpu", True)),
    PcPowerSensorDescription(key="memory_used", value=lambda c: _live(c, "memory", "used_pct"), **PCT),
    PcPowerSensorDescription(key="ram_temp", value=lambda c: _ram_max(c), **TEMP,
                             entity_registry_enabled_default=False,
                             present=lambda c: bool(c.inventory.get("ram_sensors"))),
    PcPowerSensorDescription(key="idle_since", value=_idle_since, device_class=SensorDeviceClass.TIMESTAMP,
                             present=lambda c: c.inventory.get("has_session", False)),
    PcPowerSensorDescription(key="ip_address", value=lambda c: (c.inventory.get("primary_ip") or "").split("/")[0] or None,
                             attrs=_interfaces, live_only=False, entity_category=EntityCategory.DIAGNOSTIC),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PcPowerConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    entities: list[SensorEntity] = [PcPowerSensor(c, d) for d in SENSORS if d.present(c)]
    entities += [NvmeTempSensor(c, dev) for dev in c.inventory.get("nvme") or []]
    entities += [FanSpeedSensor(c, fid) for fid in c.inventory.get("fans") or []]
    async_add_entities(entities)


class PcPowerSensor(PcPowerEntity, SensorEntity):
    entity_description: PcPowerSensorDescription

    def __init__(self, coordinator: Coord, description: PcPowerSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self.live_only = description.live_only

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict | None:
        fn = self.entity_description.attrs
        return fn(self.coordinator) if fn and self.coordinator.live() else None


class NvmeTempSensor(PcPowerEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 0
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: Coord, device: str) -> None:
        super().__init__(coordinator, "nvme_temp", f"nvme_temp_{device}")
        self._device = device
        self._attr_translation_placeholders = {"device": device}

    @property
    def native_value(self) -> float | None:
        for n in _live(self.coordinator, "thermals", "nvme") or []:
            if n.get("device") == self._device:
                return _r(n.get("temp_c"))
        return None


class FanSpeedSensor(PcPowerEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "rpm"
    _attr_icon = "mdi:fan"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: Coord, fan_id: str) -> None:
        super().__init__(coordinator, "fan_speed", f"fan_{fan_id}")
        self._fan_id = fan_id
        self._attr_translation_placeholders = {"fan": fan_id.rsplit("_", 1)[-1].replace("fan", "")}

    @property
    def native_value(self) -> int | None:
        for f in _live(self.coordinator, "thermals", "fans") or []:
            if f.get("id") == self._fan_id:
                return f.get("rpm")
        return None

    @property
    def extra_state_attributes(self) -> dict:
        return {"learned_max_rpm": self.coordinator.accountant.state.fan_max_rpm.get(self._fan_id)}
