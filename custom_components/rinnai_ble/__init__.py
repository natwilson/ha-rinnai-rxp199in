"""The Rinnai RX/RXP BLE integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import rinnai_protocol as R
from .const import DOMAIN, SERVICE_EXPERIMENTAL_WRITE_K
from .coordinator import RinnaiCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.EVENT]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

RinnaiConfigEntry = ConfigEntry[RinnaiCoordinator]

ATTR_MODE = "mode"
ATTR_SETPOINT = "setpoint"
ATTR_CONFIRM = "confirm"

EXPERIMENT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_MODE): vol.Coerce(int),
        vol.Optional(ATTR_SETPOINT): vol.Coerce(int),
        vol.Required(ATTR_CONFIRM): cv.boolean,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def _experimental_write_k(call: ServiceCall) -> ServiceResponse:
        """Developer experiment from PROTOCOL.md section 8. Not a control feature."""
        if not call.data[ATTR_CONFIRM]:
            raise ServiceValidationError(
                "Set confirm: true to send an experimental write to the heater."
            )
        try:
            data = R.build_k_experiment(
                mode=call.data.get(ATTR_MODE), setpoint=call.data.get(ATTR_SETPOINT)
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if len(entries) != 1:
            raise ServiceValidationError(
                f"Expected exactly one loaded Rinnai heater, found {len(entries)}."
            )
        coordinator: RinnaiCoordinator = entries[0].runtime_data
        result = await coordinator.async_experimental_write_k(data)
        await coordinator.async_request_refresh()
        return result

    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPERIMENTAL_WRITE_K,
        _experimental_write_k,
        schema=EXPERIMENT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


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
