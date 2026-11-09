"""Buttons: Wake-on-LAN, suspend, power off, reboot."""
from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import AgentError
from .coordinator import PcPowerConfigEntry, PcPowerCoordinator
from .entity import PcPowerEntity

ICONS = {"suspend": "mdi:power-sleep", "poweroff": "mdi:power", "reboot": "mdi:restart"}


async def async_setup_entry(
    hass: HomeAssistant, entry: PcPowerConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    entities: list[ButtonEntity] = []
    if c.inventory.get("wol_macs"):
        entities.append(WakeButton(c, "wake"))
    caps = c.inventory.get("capabilities") or {}
    if caps.get("lock"):
        entities.append(LockButton(c, "lock"))
    for action in caps.get("power") or []:
        if action in ICONS:
            entities.append(PowerButton(c, action))
    async_add_entities(entities)


class WakeButton(PcPowerEntity, ButtonEntity):
    _attr_icon = "mdi:power-on"
    live_only = False

    @property
    def extra_state_attributes(self) -> dict:
        return {"mac_addresses": self.coordinator.inventory.get("wol_macs")}

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_wake()
        except AgentError as err:
            raise HomeAssistantError(str(err)) from err


class LockButton(PcPowerEntity, ButtonEntity):
    _attr_icon = "mdi:monitor-lock"

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_lock()
        except AgentError as err:
            raise HomeAssistantError(f"locking failed: {err}") from err


class PowerButton(PcPowerEntity, ButtonEntity):
    def __init__(self, coordinator: PcPowerCoordinator, action: str) -> None:
        super().__init__(coordinator, action)
        self._action = action
        self._attr_icon = ICONS[action]
        if action == "reboot":
            self._attr_device_class = ButtonDeviceClass.RESTART

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_power(self._action)
        except AgentError as err:
            raise HomeAssistantError(f"{self._action} failed: {err}") from err
