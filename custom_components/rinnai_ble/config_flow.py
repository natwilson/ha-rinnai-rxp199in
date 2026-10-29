"""Config flow: Bluetooth auto-discovery or manual pick, plus options."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from . import rinnai_protocol as R
from .const import (
    ADAPTER_AUTO,
    CONF_ADAPTER,
    CONF_ADDRESS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)


def _is_rinnai(info: BluetoothServiceInfoBleak) -> bool:
    uuids = {u.lower() for u in info.service_uuids}
    return R.SVC_UUID in uuids or (info.name or "").startswith("Rinnai")


class RinnaiConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._discovered: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RinnaiOptionsFlow()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._create(self._address)
        return self.async_show_form(step_id="confirm")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_ADDRESS], raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self._create(user_input[CONF_ADDRESS])

        configured = self._async_current_ids(include_ignore=False)
        for info in bluetooth.async_discovered_service_info(self.hass):
            if _is_rinnai(info) and info.address not in configured:
                self._discovered[info.address] = f"{info.name} ({info.address})"
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(self._discovered)}),
        )

    def _create(self, address: str) -> ConfigFlowResult:
        return self.async_create_entry(title=f"Rinnai {address}", data={CONF_ADDRESS: address})


class RinnaiOptionsFlow(OptionsFlow):
    """Poll interval and adapter pinning (bonds are stored per adapter)."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        current_adapter = options.get(CONF_ADAPTER, ADAPTER_AUTO)
        choices = {ADAPTER_AUTO: "Automatic (best local adapter)"}
        # List every scanner, not only ones seeing the heater right now: it
        # stops advertising while connected.
        try:
            from habluetooth import BaseHaRemoteScanner, get_manager

            for scanner in get_manager().async_current_scanners():
                kind = "proxy" if isinstance(scanner, BaseHaRemoteScanner) else "USB/local"
                choices[scanner.source] = f"{scanner.name} [{kind}]"
        except Exception:  # noqa: BLE001
            pass
        choices.setdefault(current_adapter, f"{current_adapter} (not currently visible)")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
                    vol.Required(CONF_ADAPTER, default=current_adapter): vol.In(choices),
                }
            ),
        )
