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
from .const import (
    DOMAIN,
    SERVICE_EXPERIMENTAL_WRITE_K,
    SERVICE_RECONNECT,
    SERVICE_RELEASE_LINK,
)
from .coordinator import DEFAULT_RELEASE_MINUTES, RinnaiCoordinator

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.BUTTON,
    Platform.SWITCH,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

RinnaiConfigEntry = ConfigEntry[RinnaiCoordinator]

ATTR_MODE = "mode"
ATTR_SETPOINT = "setpoint"
ATTR_CONFIRM = "confirm"
ATTR_MINUTES = "minutes"

RELEASE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_MINUTES, default=DEFAULT_RELEASE_MINUTES): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=240)
        )
    }
)

EXPERIMENT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_MODE): vol.Coerce(int),
        vol.Optional(ATTR_SETPOINT): vol.Coerce(int),
        vol.Required(ATTR_CONFIRM): cv.boolean,
    }
)


def _only_coordinator(hass: HomeAssistant) -> RinnaiCoordinator:
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if len(entries) != 1:
        raise ServiceValidationError(
            f"Expected exactly one loaded Rinnai heater, found {len(entries)}."
        )
    return entries[0].runtime_data


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
        coordinator = _only_coordinator(hass)
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

    async def _release_link(call: ServiceCall) -> None:
        """Hand the heater's single Bluetooth connection to the app or a technician."""
        await _only_coordinator(hass).async_release_link(call.data[ATTR_MINUTES])

    async def _reconnect(call: ServiceCall) -> None:
        await _only_coordinator(hass).async_reconnect_now()

    hass.services.async_register(DOMAIN, SERVICE_RELEASE_LINK, _release_link, schema=RELEASE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RECONNECT, _reconnect)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RinnaiConfigEntry) -> bool:
    coordinator = RinnaiCoordinator(hass, entry)
    # A heater that is busy, out of range or slow to open its session must not
    # fail setup: the entities load unavailable and the coordinator keeps
    # retrying, so dashboards and automations survive a bad start.
    await coordinator.async_refresh()
    entry.runtime_data = coordinator
    coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: RinnaiConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RinnaiConfigEntry) -> bool:
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
    return unloaded
