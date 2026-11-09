"""Monitor brightness (DDC/CI) for PC Power."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import AgentError
from .coordinator import PcPowerConfigEntry, PcPowerCoordinator
from .entity import PcPowerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PcPowerConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    if not (c.inventory.get("capabilities") or {}).get("monitor_brightness"):
        return
    async_add_entities(MonitorBrightness(c, m) for m in c.inventory.get("monitors") or [])


class MonitorBrightness(PcPowerEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:brightness-6"

    def __init__(self, coordinator: PcPowerCoordinator, monitor: dict) -> None:
        super().__init__(coordinator, "monitor_brightness", f"monitor_brightness_{monitor['key']}")
        self._key = monitor["key"]
        self._attr_translation_placeholders = {"monitor": monitor["device"]}

    def _row(self) -> dict | None:
        if not self.coordinator.online:
            return None
        return next((r for r in self.coordinator.monitor_rows if r["key"] == self._key), None)

    @property
    def available(self) -> bool:
        row = self._row()
        return super().available and row is not None and row["state"] == "on"

    @property
    def native_value(self) -> float | None:
        row = self._row()
        if row is None:
            return None
        for m in (self.coordinator.metrics or {}).get("monitors") or []:
            if m.get("bus") == row["bus"]:
                return m.get("brightness")
        return row.get("brightness")

    async def async_set_native_value(self, value: float) -> None:
        row = self._row()
        if row is None:
            raise HomeAssistantError("monitor is not available")
        try:
            await self.coordinator.async_set_brightness(row["bus"], int(value))
        except AgentError as err:
            raise HomeAssistantError(f"setting brightness failed: {err}") from err
