"""Base entity shared by all Rinnai platforms."""
from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import RinnaiCoordinator


class RinnaiEntity(CoordinatorEntity[RinnaiCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: RinnaiCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        # Unique IDs match pre-1.0 releases so existing entities carry over.
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            identifiers={(DOMAIN, coordinator.address)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name="Rinnai Water Heater",
            serial_number=coordinator.serial,
        )

    @property
    def _value(self):
        return (self.coordinator.data or {}).get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._key in (self.coordinator.data or {})
