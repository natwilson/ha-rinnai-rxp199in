"""The Rinnai RX/RXP BLE integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import RinnaiCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

RinnaiConfigEntry = ConfigEntry[RinnaiCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: RinnaiConfigEntry) -> bool:
    coordinator = RinnaiCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: RinnaiConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RinnaiConfigEntry) -> bool:
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
    return unloaded
