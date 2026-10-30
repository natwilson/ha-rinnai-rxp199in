"""Binary sensors for the Rinnai BLE heater."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RinnaiConfigEntry
from .entity import RinnaiEntity


@dataclass(frozen=True, kw_only=True)
class RinnaiBinaryDescription(BinarySensorEntityDescription):
    data_key: str
    """Key in the coordinator data (differs from `key` only for legacy unique IDs)."""


BINARY_SENSORS: tuple[RinnaiBinaryDescription, ...] = (
    # \K byte 6 -- off during post-combustion fan purge, unlike any fan-based rule.
    RinnaiBinaryDescription(
        key="combustion", data_key="combustion", name="Combustion",
        device_class=BinarySensorDeviceClass.HEAT, icon="mdi:fire",
    ),
    # \K byte 1 reads 2 while the recirculation pump runs.
    RinnaiBinaryDescription(
        key="pump_running", data_key="pump_running", name="Recirculation Pump",
        device_class=BinarySensorDeviceClass.RUNNING, icon="mdi:pump",
    ),
    # \P non-zero = active diagnostic code.
    RinnaiBinaryDescription(
        key="fault", data_key="diagnostic_code_active", name="Diagnostic Code Active",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    # Water flowing with the burner off for 60 s and no fault code: the
    # low-flow overheat cut-out. Clears only once flow stops.
    RinnaiBinaryDescription(
        key="silent_lockout", data_key="silent_lockout", name="Silent Lockout",
        device_class=BinarySensorDeviceClass.PROBLEM, icon="mdi:water-alert",
    ),
    # \L bytes 21-22: a capability flag, always 1, not pump run state.
    RinnaiBinaryDescription(
        key="internal_pump", data_key="internal_pump_installed",
        name="Internal Pump Installed", icon="mdi:pump",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RinnaiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(RinnaiBinarySensor(entry.runtime_data, d) for d in BINARY_SENSORS)


class RinnaiBinarySensor(RinnaiEntity, BinarySensorEntity):
    entity_description: RinnaiBinaryDescription

    def __init__(self, coordinator, description: RinnaiBinaryDescription) -> None:
        super().__init__(coordinator, description.data_key)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        return self._value
