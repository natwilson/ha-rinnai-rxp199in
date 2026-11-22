"""Switch entities for the Rinnai BLE heater."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RinnaiConfigEntry
from .entity import RinnaiEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RinnaiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RinnaiReleaseSwitch(entry.runtime_data)])


class RinnaiReleaseSwitch(RinnaiEntity, SwitchEntity):
    """On = Home Assistant stays off the heater so another device can connect.

    The heater takes one Bluetooth connection at a time. Turning this on
    lets the Rinnai app or a technician's tool connect; it turns itself off
    after 15 minutes (use the release_link action for another duration).
    On an RXP199iN the app then connected with its own pairing and Home
    Assistant resumed on its existing bond afterwards. Disconnect the app
    with its Disconnect button or by force-closing it; in the background it
    keeps the connection.
    """

    _attr_name = "Release Bluetooth Link"
    _attr_icon = "mdi:bluetooth-off"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "release_link")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.released

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_release_link()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_reconnect_now()
