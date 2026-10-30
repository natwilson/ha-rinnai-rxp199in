"""Coordinator behaviour against a simulated heater.

Needs the Home Assistant test harness:
    pip install pytest-homeassistant-custom-component
    pytest tests
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from bleak.exc import BleakError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.rinnai_ble import coordinator as C
from custom_components.rinnai_ble import rinnai_protocol as R
from custom_components.rinnai_ble.const import CONF_ADDRESS, DOMAIN

from .test_protocol import K_EXAMPLE, L_EXAMPLE

ADDRESS = "AA:BB:CC:DD:EE:FF"
N_EXAMPLE = "0" * 10 + "1234567890" + "0" * 68
P_EXAMPLE = "0" * 98


class FakeHeater:
    """Answers queries like the RXP199iN, with switchable misbehaviour."""

    def __init__(self) -> None:
        self.is_connected = True
        self.notify = None
        self.reasm = R.Reassembler()
        self.writes: list[str] = []
        self.answer_l = True            # False: \L silently ignored
        self.l_needs_handshake = False  # \L ignored until the next \I
        self.answer_i = True
        self.duplicate = False          # duplicate a middle fragment of every reply
        self.drop_after = None          # raise on the Nth write
        self.l_payload = L_EXAMPLE
        self.k_payload = K_EXAMPLE
        self.disconnects = 0

    async def start_notify(self, _uuid, cb):
        self.notify = cb

    async def pair(self):
        return True

    async def disconnect(self):
        self.is_connected = False
        self.disconnects += 1

    async def write_gatt_char(self, _uuid, data, response=False):
        if self.drop_after is not None:
            self.drop_after -= 1
            if self.drop_after < 0:
                self.is_connected = False
                raise BleakError("[org.bluez.Error.NotConnected] Not Connected")
        for reg, _data in self.reasm.feed(bytes(data)):
            self.writes.append(reg)
            asyncio.get_running_loop().call_soon(self._respond, reg)

    def _respond(self, reg: str) -> None:
        if reg == "I":
            if not self.answer_i:
                return
            self.l_needs_handshake = False
            payload = "00"
        elif reg == "L":
            if not self.answer_l or self.l_needs_handshake:
                return
            payload = self.l_payload
        else:
            payload = {"K": self.k_payload, "N": N_EXAMPLE, "P": P_EXAMPLE}[reg]
        frags = R.fragment(R.build_frame(reg, payload))
        if self.duplicate and len(frags) > 2:
            frags.insert(2, frags[1])
        for f in frags:
            self.notify(None, bytearray(f))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(C, "QUERY_TIMEOUT", 0.05)
    monkeypatch.setattr(C, "HANDSHAKE_TIMEOUT", 0.05)
    monkeypatch.setattr(C, "QUERY_GAP", 0)
    monkeypatch.setattr(C, "CONNECT_TIMEOUT", 0.3)
    monkeypatch.setattr(C, "STEP_TIMEOUT", 0.1)
    monkeypatch.setattr(C, "DISCONNECT_TIMEOUT", 0.1)
    monkeypatch.setattr(C, "POLL_TIMEOUT", 2)


@pytest.fixture
def heater():
    return FakeHeater()


@pytest.fixture
async def coord(hass, heater):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDRESS})
    entry.add_to_hass(hass)
    c = C.RinnaiCoordinator(hass, entry)

    async def _connect(*_a, **_k):
        heater.is_connected = True
        heater.reasm.reset()
        return heater

    with patch.object(C, "establish_connection", _connect), patch.object(
        C.RinnaiCoordinator, "_pick_device", return_value=object()
    ):
        yield c


async def test_happy_path(coord, heater):
    data = await coord._async_update_data()
    assert data["outgoing_temp"] == 96
    assert data["temperature_rise"] == 20
    assert data["combustion"] is False
    assert data["setpoint_temp"] == 120
    assert data["diagnostic_code_active"] is False
    assert coord.serial == "1234567890"
    assert heater.writes[0] == "I"


async def test_duplicated_fragments_still_parse(coord, heater):
    heater.duplicate = True
    data = await coord._async_update_data()
    assert data["total_heat_energy"] == 3_013_000


async def test_l_stall_recovered_by_handshake_without_reconnect(coord, heater):
    await coord._async_update_data()
    heater.l_needs_handshake = True
    data = await coord._async_update_data()
    assert data["outgoing_temp"] == 96
    assert heater.disconnects == 0
    assert heater.writes.count("I") == 2


async def test_persistent_l_stall_reconnects_but_stays_available(coord, heater):
    await coord._async_update_data()
    heater.answer_l = False
    for _ in range(C.L_STALLS_BEFORE_RECONNECT):
        data = await coord._async_update_data()
        assert data["outgoing_temp"] == 96   # last-good value kept
        assert "combustion" in data          # \K still live
    assert heater.disconnects == 1


async def test_dropped_link_rides_on_cache_then_goes_stale(coord, heater, monkeypatch):
    await coord._async_update_data()
    heater.drop_after = 0
    data = await coord._async_update_data()   # write fails -> teardown, cached data
    assert data["outgoing_temp"] == 96
    assert coord._client is None

    heater.drop_after = None
    heater.answer_i = False                   # heater keeps refusing the session
    real = C.time.monotonic
    monkeypatch.setattr(C.time, "monotonic", lambda: real() + C.STALE_AFTER + 1)
    coord._next_connect = 0
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_failed_handshake_backs_off(coord, heater):
    heater.answer_i = False
    with pytest.raises(UpdateFailed, match="handshake"):
        await coord._async_update_data()
    assert coord._backoff > 0
    with pytest.raises(UpdateFailed, match="waiting"):
        await coord._async_update_data()


async def test_shutdown_disconnects(coord, heater):
    await coord._async_update_data()
    await coord.async_shutdown()
    assert heater.disconnects == 1


async def test_unique_ids_match_previous_releases(coord):
    from custom_components.rinnai_ble.binary_sensor import BINARY_SENSORS, RinnaiBinarySensor
    from custom_components.rinnai_ble.sensor import SENSORS, RinnaiSensor

    ids = {RinnaiBinarySensor(coord, d).unique_id for d in BINARY_SENSORS}
    assert ids == {
        f"{ADDRESS}_{k}"
        for k in ("combustion", "pump_running", "fault", "internal_pump", "silent_lockout")
    }
    assert RinnaiSensor(coord, SENSORS[0]).unique_id == f"{ADDRESS}_outgoing_temp"


async def test_full_setup_creates_entities_and_unloads(hass, heater):
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import entity_registry as er
    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDRESS}, unique_id=ADDRESS)
    entry.add_to_hass(hass)

    async def _connect(*_a, **_k):
        heater.is_connected = True
        return heater

    with patch.object(C, "establish_connection", _connect), patch.object(
        C.RinnaiCoordinator, "_pick_device", return_value=object()
    ), patch("homeassistant.components.bluetooth_adapters.async_setup", return_value=True), patch(
        "homeassistant.loader.Integration.dependencies", new_callable=lambda: property(lambda s: [])
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        reg = er.async_get(hass)
        ent = reg.async_get_entity_id("binary_sensor", DOMAIN, f"{ADDRESS}_combustion")
        assert hass.states.get(ent).state == "off"
        ent = reg.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_outgoing_temp")
        assert hass.states.get(ent).state == "96"

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert heater.disconnects == 1


async def test_config_flow_imports(hass):
    from custom_components.rinnai_ble import config_flow

    assert config_flow.RinnaiConfigFlow.VERSION == 1


async def test_hung_connect_times_out_instead_of_blocking(coord, heater):
    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    with patch.object(C, "establish_connection", _hang):
        with pytest.raises(UpdateFailed, match="connect step timed out"):
            await asyncio.wait_for(coord._async_update_data(), 5)
    assert coord._backoff > 0


async def test_hung_subscribe_and_disconnect_time_out(coord, heater):
    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    heater.start_notify = _hang
    heater.disconnect = _hang
    with pytest.raises(UpdateFailed, match="subscribe step timed out"):
        await asyncio.wait_for(coord._async_update_data(), 5)
    assert coord._client is None


async def test_hung_write_counts_as_missed_reply(coord, heater):
    await coord._async_update_data()

    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    heater.write_gatt_char = _hang
    data = await asyncio.wait_for(coord._async_update_data(), 5)
    assert data["outgoing_temp"] == 96   # served from cache, not blocked


def drawing(flow_tenths: int, burning: bool) -> tuple[str, str]:
    """(\\L, \\K) payloads for a draw: flow in 0.1 gal/min, burner on or off."""
    l = L_EXAMPLE[:8] + f"{flow_tenths:04X}" + L_EXAMPLE[12:]
    k = K_EXAMPLE[:12] + ("01" if burning else "00") + K_EXAMPLE[14:]
    return l, k


async def test_derived_heat_demand_and_margin(coord, heater):
    heater.l_payload, heater.k_payload = drawing(7, True)
    data = await coord._async_update_data()
    # 0.7 gal/min x (120 setpoint - 76 inlet) x 500
    assert data["heat_demand"] == 15400
    assert data["firing_margin"] == pytest.approx(2.7)
    assert data["hex_overshoot"] == 3
    assert data["silent_lockout"] is False


async def test_margin_unknown_when_idle(coord, heater):
    data = await coord._async_update_data()
    assert data["heat_demand"] == 0
    assert data["firing_margin"] is None


async def test_silent_lockout_after_sustained_unheated_flow(coord, heater, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(C.time, "monotonic", lambda: clock[0])
    heater.l_payload, heater.k_payload = drawing(7, True)
    assert (await coord._async_update_data())["silent_lockout"] is False

    heater.k_payload = drawing(7, False)[1]          # burner cuts out, water still flowing
    for step in (31, 31):
        clock[0] += step
        assert (await coord._async_update_data())["silent_lockout"] is False
    clock[0] += 31
    assert (await coord._async_update_data())["silent_lockout"] is True

    heater.l_payload = drawing(0, False)[0]          # tap closed
    clock[0] += 31
    assert (await coord._async_update_data())["silent_lockout"] is False


async def test_min_firing_rate_option(hass, heater):
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ADDRESS: ADDRESS}, options={"min_firing_rate": 10000}
    )
    entry.add_to_hass(hass)
    c = C.RinnaiCoordinator(hass, entry)
    assert c.min_firing_rate == 10000
