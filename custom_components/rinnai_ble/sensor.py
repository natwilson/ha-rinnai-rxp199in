"""Sensor entities for the Rinnai BLE heater."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import RinnaiConfigEntry
from .coordinator import FAILURE_STAGES, LINK_STATES
from .entity import RinnaiEntity

SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="outgoing_temp", name="Outgoing Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="inlet_temp", name="Inlet Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="hex_outlet_temp", name="HEX Outlet Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="exhaust_temp", name="Exhaust Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="freeze_protection_temp", name="Freeze Protection Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="lime_detecting_temp", name="Lime Detecting Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="water_flow_rate", name="Water Flow Rate",
        native_unit_of_measurement="gal/min",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water",
    ),
    SensorEntityDescription(
        key="fan_frequency", name="Fan Frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="fan_current", name="Fan Current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="bypass_position", name="Bypass Flow Control Position",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:valve",
    ),
    SensorEntityDescription(
        key="setpoint_temp", name="Setpoint Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Everything that passes through the heat exchanger, so recirculation runs
    # count too. Not a substitute for a water meter.
    SensorEntityDescription(
        key="water_through_heater", name="Water Through Heater",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:water-sync",
    ),
    SensorEntityDescription(
        key="total_heat_energy", name="Total Heat Energy",
        native_unit_of_measurement="kcal",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:fire",
    ),
    SensorEntityDescription(
        key="combustion_cycles", name="Combustion Cycles",
        native_unit_of_measurement="cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:counter",
    ),
    SensorEntityDescription(
        key="combustion_hours", name="Combustion Hours",
        native_unit_of_measurement="h",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock-outline",
    ),
    SensorEntityDescription(
        key="pump_cycles", name="Pump Cycles",
        native_unit_of_measurement="cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:counter",
    ),
    SensorEntityDescription(
        key="pump_hours", name="Pump Hours",
        native_unit_of_measurement="h",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock-outline",
    ),
    SensorEntityDescription(
        key="temperature_rise", name="Temperature Rise",
        device_class=SensorDeviceClass.TEMPERATURE_DELTA,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-chevron-up",
    ),
    # Low-flow lockout indicators (see analysis.py). Heat Demand is what the
    # current draw needs; Low-Fire Margin is how far that sits above the
    # burner's minimum firing rate, and goes negative below it.
    SensorEntityDescription(
        key="heat_demand", name="Heat Demand",
        native_unit_of_measurement=UnitOfPower.BTU_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire-circle",
    ),
    SensorEntityDescription(
        key="firing_margin", name="Low-Fire Margin",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gauge-low",
    ),
    # Heat exchanger outlet minus delivered water: the bypass valve diluting
    # overheated water. Large values precede an overheat cut-out.
    SensorEntityDescription(
        key="hex_overshoot", name="HEX Overshoot",
        device_class=SensorDeviceClass.TEMPERATURE_DELTA,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:thermometer-alert",
    ),
    SensorEntityDescription(
        key="diagnostic_code", name="Diagnostic Code",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alert-circle-outline",
    ),
    # Raw frames, disabled by default. Enable them to decode unmapped fields
    # (see PROTOCOL.md) without a packet capture.
    SensorEntityDescription(
        key="unknown_l16", name="Unknown L Byte 16",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:help-circle-outline",
    ),
    SensorEntityDescription(
        key="unknown_l39", name="Unknown L Counter 39-40",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:help-circle-outline",
    ),
    SensorEntityDescription(
        key="raw_k", name="Raw Status Frame K",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:code-braces",
    ),
    SensorEntityDescription(
        key="raw_l", name="Raw Diagnostic Frame L",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:code-braces",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RinnaiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            *(RinnaiSensor(coordinator, d) for d in SENSORS),
            RinnaiFalseStartsSensor(coordinator),
            *(RinnaiHealthSensor(coordinator, d) for d in HEALTH_SENSORS),
        ]
    )


class RinnaiSensor(RinnaiEntity, SensorEntity):
    def __init__(self, coordinator, description: SensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self._value


# Connection health. These read the coordinator itself, not heater data, and
# stay available while the heater is unreachable: that is when they matter.
HEALTH_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="link_state", name="Bluetooth Link State",
        device_class=SensorDeviceClass.ENUM, options=LINK_STATES,
        entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:bluetooth",
    ),
    SensorEntityDescription(
        key="last_failure_stage", name="Last Connection Failure",
        device_class=SensorDeviceClass.ENUM, options=FAILURE_STAGES,
        entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:bluetooth-off",
    ),
    SensorEntityDescription(
        key="connections", name="Bluetooth Connections",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:bluetooth-connect",
    ),
    SensorEntityDescription(
        key="last_good_frame", name="Last Good Frame",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class RinnaiHealthSensor(RinnaiEntity, SensorEntity):
    def __init__(self, coordinator, description: SensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return getattr(self.coordinator, self._key)

    @property
    def extra_state_attributes(self):
        if self._key != "last_failure_stage":
            return None
        return {"reason": self.coordinator.last_failure}


class RinnaiFalseStartsSensor(RinnaiEntity, RestoreSensor):
    """Fan runs today that ended without the burner lighting (see analysis.py).

    Counted by the integration, so the count is restored across restarts and
    resets at local midnight.
    """

    entity_description = SensorEntityDescription(
        key="false_starts_today", name="False Starts Today",
        native_unit_of_measurement="starts",
        state_class=SensorStateClass.TOTAL,
        icon="mdi:fan-alert",
    )

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "false_starts_today")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        data = await self.async_get_last_sensor_data()
        if last is None or data is None or data.native_value is None:
            return
        reset = dt_util.parse_datetime(str(last.attributes.get("last_reset", "")))
        if reset is not None:
            self.coordinator.restore_false_starts(
                dt_util.as_local(reset).date(), int(float(data.native_value))
            )
            self.async_write_ha_state()

    @property
    def native_value(self):
        return self._value

    @property
    def last_reset(self) -> datetime:
        return dt_util.start_of_local_day()
