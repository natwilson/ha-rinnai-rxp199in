"""Event entities for the Rinnai BLE heater."""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RinnaiConfigEntry
from .entity import RinnaiEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RinnaiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RinnaiFalseStartEvent(entry.runtime_data)])


class RinnaiFalseStartEvent(RinnaiEntity, EventEntity):
    """Fires once per false start: the fan ran but the burner never lit.

    Attributes carry the run's duration (s), top fan speed (Hz) and top flow
    (gal/min), so automations and the logbook can tell short blips apart.
    """

    _attr_name = "False Start"
    _attr_event_types = ["false_start"]
    _attr_icon = "mdi:fan-alert"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "last_false_start")
        self._seen = self._seq()

    def _seq(self) -> int:
        return (self._value or {}).get("seq", 0)

    @property
    def available(self) -> bool:
        # Nothing to show until the first false start, but still usable.
        return self.coordinator.last_update_success

    @callback
    def _handle_coordinator_update(self) -> None:
        if (seq := self._seq()) > self._seen:
            self._seen = seq
            event = {k: v for k, v in self._value.items() if k != "seq"}
            self._trigger_event("false_start", event)
        super()._handle_coordinator_update()
