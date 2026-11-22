"""Button entities for the Rinnai BLE heater."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RinnaiConfigEntry
from .entity import RinnaiEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RinnaiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RinnaiReconnectButton(entry.runtime_data)])


class RinnaiReconnectButton(RinnaiEntity, ButtonEntity):
    """Ends a release or a reconnect wait and connects straight away."""

    _attr_name = "Reconnect Now"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "reconnect")

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self.coordinator.async_reconnect_now()
        if not self.coordinator.last_update_success:
            # Shown as a pop-up in the UI: the nearest thing to a tooltip.
            # The heater's front Bluetooth LED is solid while any device is
            # connected and off while the heater is free.
            if self.coordinator.in_use_elsewhere:
                raise HomeAssistantError(
                    "The heater is not advertising, so another device (for example "
                    "the Rinnai app) is probably connected to it: the Bluetooth LED "
                    "on the front of the heater will be solid. Disconnect that "
                    "device (in the Rinnai app, press Disconnect or force-close "
                    "the app), then press Reconnect Now again. Do not press the "
                    "heater's Bluetooth button; that starts pairing mode."
                )
            raise HomeAssistantError(
                "Could not connect to the heater. If the Bluetooth LED on the "
                "front of the heater is solid, another device is connected: "
                "disconnect it first. If the LED is off the heater is free; "
                "press Reconnect Now again (right after pairing it can take "
                "about a minute). Do not press the heater's Bluetooth button "
                "unless you mean to pair. "
                f"Last error: {self.coordinator.last_failure}"
            )
