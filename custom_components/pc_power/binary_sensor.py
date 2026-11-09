"""Binary sensors for PC Power."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PcPowerConfigEntry, PcPowerCoordinator
from .entity import PcPowerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PcPowerConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    entities: list[BinarySensorEntity] = [PcPowerOnline(c, "online")]
    if c.inventory.get("has_session"):
        entities += [SessionFlag(c, "screen_locked", "locked"), SessionFlag(c, "user_idle", "idle")]
    async_add_entities(entities)


class PcPowerOnline(PcPowerEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    live_only = False

    @property
    def is_on(self) -> bool:
        return self.coordinator.online


class SessionFlag(PcPowerEntity, BinarySensorEntity):
    """logind LockedHint / IdleHint for the active graphical session (set by the desktop)."""

    def __init__(self, coordinator: PcPowerCoordinator, key: str, field: str) -> None:
        super().__init__(coordinator, key)
        self._field = field
        self._attr_icon = "mdi:monitor-lock" if field == "locked" else "mdi:sleep"

    @property
    def is_on(self) -> bool | None:
        session = (self.coordinator.live() or {}).get("session")
        if not session or self._field not in session:
            return None
        return bool(session[self._field])

    @property
    def extra_state_attributes(self) -> dict | None:
        session = (self.coordinator.live() or {}).get("session")
        if not session:
            return None
        return {"user": session.get("user"), "desktop": session.get("desktop"), "session_type": session.get("type")}
