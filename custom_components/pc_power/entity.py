"""Base entity for PC Power."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MACHINE_ID, DOMAIN
from .coordinator import PcPowerCoordinator


class PcPowerEntity(CoordinatorEntity[PcPowerCoordinator]):
    _attr_has_entity_name = True
    live_only = True  # unavailable while the PC is offline

    def __init__(self, coordinator: PcPowerCoordinator, key: str, unique_suffix: str | None = None) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        mid = entry.data[CONF_MACHINE_ID]
        self._attr_unique_id = f"{mid}_{unique_suffix or key}"
        self._attr_translation_key = key
        inv = coordinator.inventory
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mid)},
            name=entry.title,
            manufacturer="PC Power",
            model="Linux PC",
            sw_version=inv.get("agent_version"),
        )

    @property
    def available(self) -> bool:
        return super().available and (not self.live_only or self.coordinator.online)
