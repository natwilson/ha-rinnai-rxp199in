"""Coordinator behaviour against a simulated heater.

Needs the Home Assistant test harness:
    pip install pytest-homeassistant-custom-component
    pytest tests
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import patch

import pytest
from bleak.exc import BleakError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError
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
        self.k_writes: list[str] = []   # data of every \K write
        self.obey_k = False             # True: nonzero \K bytes are applied as commands
        self.answer_l = True            # False: \L silently ignored
        self.l_needs_handshake = False  # \L ignored until the next \I
        self.answer_i = True
        self.ignore_i = 0               # ignore this many \\I writes, then answer
        self.silent = False             # connects and subscribes, answers nothing
        self.refuse_notify = False      # subscribe rejected: link not encrypted
        self.encrypt_on_pair = False    # pair() on the bonded device encrypts the link
        self.pair_calls = 0
        self.duplicate = False          # duplicate a middle fragment of every reply
        self.drop_after = None          # raise on the Nth write
        self.l_payload = L_EXAMPLE
        self.k_payload = K_EXAMPLE
        self.disconnects = 0

    async def start_notify(self, _uuid, cb):
        if self.refuse_notify:
            raise BleakError("[org.bluez.Error.NotPermitted] Insufficient Authentication")
        self.notify = cb

    async def pair(self):
        self.pair_calls += 1
        if self.encrypt_on_pair:
            self.refuse_notify = False
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
        for reg, payload in self.reasm.feed(bytes(data)):
            self.writes.append(reg)
            if reg == "K":
                self.k_writes.append(payload)
                if self.obey_k:
                    status = bytearray.fromhex(self.k_payload)
                    for i, value in enumerate(bytes.fromhex(payload)):
                        if value:
                            status[i] = value
                    self.k_payload = status.hex().upper()
            asyncio.get_running_loop().call_soon(self._respond, reg)

    def _respond(self, reg: str) -> None:
        if self.silent:
            return
        if reg == "I":
            if self.ignore_i > 0:
                self.ignore_i -= 1
                return
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
    monkeypatch.setattr(C, "CONNECT_TIMEOUT", 1.0)
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
    heater.silent = True                      # heater keeps refusing the session
    real = C.time.monotonic
    monkeypatch.setattr(C.time, "monotonic", lambda: real() + C.STALE_AFTER + 1)
    coord._next_connect = 0
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_failed_handshake_backs_off(coord, heater):
    heater.silent = True
    with pytest.raises(UpdateFailed, match="handshake"):
        await coord._async_update_data()
    assert coord._backoff > 0
    assert coord.last_failure_stage == "handshake"
    assert heater.writes.count("I") == C.HANDSHAKE_ATTEMPTS
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
        ent = reg.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_false_starts_today")
        assert hass.states.get(ent).state == "0"
        ent = reg.async_get_entity_id("event", DOMAIN, f"{ADDRESS}_last_false_start")
        assert hass.states.get(ent).state == "unknown"   # available, nothing fired yet

        for payload in (fan_run(359, 2, hex_f=78), fan_run(0, hex_f=78)):
            heater.l_payload = payload
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
        state = hass.states.get(ent)
        assert state.attributes["event_type"] == "false_start"
        assert state.attributes["max_fan_frequency"] == 359
        ent = reg.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_false_starts_today")
        assert hass.states.get(ent).state == "1"

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


def fan_run(fan_hz: int, flow_tenths: int = 0, hex_f: int | None = None) -> str:
    """\\L payload with the fan at fan_hz (and optionally flow / HEX outlet °F)."""
    l = f"{fan_hz:04X}" + L_EXAMPLE[4:]
    l = l[:8] + f"{flow_tenths:04X}" + l[12:]
    if hex_f is not None:
        l = l[:16] + f"{hex_f:04X}" + l[20:]
    return l


async def test_polls_fast_while_active_then_slows(coord, heater):
    await coord._async_update_data()
    assert coord.update_interval == coord._base_interval
    heater.l_payload = fan_run(359, 2)
    await coord._async_update_data()
    assert coord.update_interval.total_seconds() == C.FAST_POLL_INTERVAL
    heater.l_payload = fan_run(0)
    await coord._async_update_data()
    assert coord.update_interval == coord._base_interval


async def test_fast_poll_option_off(hass, heater):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDRESS}, options={"fast_poll": False})
    entry.add_to_hass(hass)
    c = C.RinnaiCoordinator(hass, entry)
    c._cache.update(fan_frequency=359)
    c._set_pace()
    assert c.update_interval == c._base_interval


async def test_false_start_counted_and_reported(coord, heater):
    data = await coord._async_update_data()
    assert data["false_starts_today"] == 0
    # HEX at 78 °F with 76 °F inlet: cold, so not the purge after a burn.
    heater.l_payload = fan_run(359, 2, hex_f=78)
    await coord._async_update_data()
    heater.l_payload = fan_run(0, hex_f=78)
    data = await coord._async_update_data()
    assert data["false_starts_today"] == 1
    assert data["last_false_start"]["seq"] == 1
    assert data["last_false_start"]["max_fan_frequency"] == 359


async def test_short_burn_is_not_a_false_start(coord, heater):
    await coord._async_update_data()
    heater.l_payload, heater.k_payload = fan_run(359, 15), drawing(15, True)[1]
    await coord._async_update_data()
    heater.l_payload, heater.k_payload = fan_run(0), K_EXAMPLE
    data = await coord._async_update_data()
    assert data["false_starts_today"] == 0


async def test_count_restored_only_for_today(coord):
    from homeassistant.util import dt as dt_util

    coord.restore_false_starts(dt_util.now().date(), 4)
    assert coord.false_starts_today == 4
    coord.restore_false_starts(date(2000, 1, 1), 9)
    assert coord.false_starts_today == 4


async def test_experimental_write_reports_an_ignored_command(coord, heater, monkeypatch):
    monkeypatch.setattr(C, "K_EXPERIMENT_SETTLE", 0)
    result = await coord.async_experimental_write_k(R.build_k_experiment(mode=2))
    assert heater.k_writes == [R.K_QUERY, "000200000000000000", R.K_QUERY]
    assert result["replied"] is True
    assert result["status_changed"] is False
    assert result["before"] == result["after"] == K_EXAMPLE


async def test_experimental_write_reports_an_accepted_command(coord, heater, monkeypatch):
    monkeypatch.setattr(C, "K_EXPERIMENT_SETTLE", 0)
    heater.obey_k = True
    result = await coord.async_experimental_write_k(R.build_k_experiment(setpoint=115))
    assert result["status_changed"] is True
    assert R.parse_K(result["before"])["setpoint_temp"] == 120
    assert R.parse_K(result["after"])["setpoint_temp"] == 115


# ---- 1.5.0: connection robustness ---------------------------------------------


async def test_handshake_is_retried_on_the_same_link(coord, heater):
    heater.ignore_i = 2
    orig = coord._handshake                   # \\K must not rescue this one

    async def _no_status(attempts=1, *, accept_status=False):
        return await orig(attempts, accept_status=False)

    coord._handshake = _no_status
    data = await coord._async_update_data()
    assert data["outgoing_temp"] == 96
    assert heater.writes.count("I") == 3
    assert heater.disconnects == 0


async def test_status_reply_counts_as_an_open_session(coord, heater):
    heater.answer_i = False                   # \\I never answered, \\K is
    data = await coord._async_update_data()
    assert data["outgoing_temp"] == 96
    assert heater.disconnects == 0
    assert coord.connections == 1


async def test_unencrypted_link_is_secured_with_pair_and_is_not_a_lost_bond(coord, heater):
    # Seen on the real heater: good bond, subscribe refused until pair() is called.
    heater.refuse_notify = True
    heater.encrypt_on_pair = True
    data = await coord._async_update_data()
    assert data["outgoing_temp"] == 96
    assert heater.pair_calls == 1
    assert coord.bond_lost is False
    assert coord.last_failure_stage == "none"


async def test_refused_encryption_reports_lost_bond(hass, coord, heater):
    from homeassistant.helpers import issue_registry as ir

    heater.refuse_notify = True
    with pytest.raises(UpdateFailed, match="not bonded"):
        await coord._async_update_data()
    assert heater.pair_calls == 1             # tried once; still refused
    assert coord.bond_lost is True
    assert coord.last_failure_stage == "not_bonded"
    assert coord.link_state == "bond_lost"
    issue_id = f"bond_lost_{ADDRESS}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    heater.refuse_notify = False              # bonded again
    coord._next_connect = 0
    await coord._async_update_data()
    assert coord.bond_lost is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_silent_links_with_unbonded_name_mean_lost_bond(coord, heater):
    heater.silent = True
    coord._adv_name = "P Rinnai RX  1234567890"
    for n in range(C.SILENT_LINKS_BOND_HINTED):
        assert coord.bond_lost is False
        coord._next_connect = 0
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
    assert coord.bond_lost is True


async def test_one_silent_link_is_not_a_lost_bond(coord, heater):
    heater.silent = True
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord.bond_lost is False
    heater.silent = False
    coord._next_connect = 0
    await coord._async_update_data()
    assert coord._silent_links == 0


async def test_release_link_disconnects_and_stays_off(coord, heater):
    await coord._async_update_data()
    await coord.async_release_link(15)
    assert heater.disconnects == 1
    assert coord.released and coord.link_state == "released"
    writes = len(heater.writes)
    data = await coord._async_update_data()   # served from cache, heater untouched
    assert data["outgoing_temp"] == 96
    assert len(heater.writes) == writes

    await coord.async_reconnect_now()
    assert not coord.released
    assert len(heater.writes) > writes
    assert coord.link_state == "connected"


async def test_release_ends_by_itself(coord, heater, monkeypatch):
    await coord._async_update_data()
    await coord.async_release_link(1)
    real = C.time.monotonic
    monkeypatch.setattr(C.time, "monotonic", lambda: real() + 61)
    data = await coord._async_update_data()
    assert not coord.released
    assert data["outgoing_temp"] == 96
    assert coord.connections == 2


async def test_on_demand_frees_the_link_when_idle_and_holds_it_when_active(hass, heater):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDRESS}, options={"on_demand": True})
    entry.add_to_hass(hass)
    c = C.RinnaiCoordinator(hass, entry)

    async def _connect(*_a, **_k):
        heater.is_connected = True
        heater.reasm.reset()
        return heater

    with patch.object(C, "establish_connection", _connect), patch.object(
        C.RinnaiCoordinator, "_pick_device", return_value=object()
    ):
        data = await c._async_update_data()
        assert data["outgoing_temp"] == 96
        assert heater.disconnects == 1 and c._client is None
        assert c.link_state == "idle"

        heater.l_payload = fan_run(359, 2)    # heater running: keep the link, poll fast
        await c._async_update_data()
        assert heater.disconnects == 1 and c._client is not None
        assert c.update_interval.total_seconds() == C.FAST_POLL_INTERVAL

        heater.l_payload = fan_run(0)
        await c._async_update_data()
        assert heater.disconnects == 2
        assert c.connections == 2


class _Heard:
    """What the Bluetooth history says about the heater's last advertisement."""

    def __init__(self, clock, name="Rinnai RX  1234567890"):
        self.clock, self.name, self.at = clock, name, clock[0]

    def info(self, *_a, **_k):
        return self if self.at is not None else None

    @property
    def time(self):
        return self.at


async def test_silence_means_in_use_elsewhere_and_its_return_reconnects(
    hass, coord, heater, monkeypatch
):
    heater.silent = True
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    # Freeze the clock only now: the timeouts above need the real one.
    clock = [C.time.monotonic()]
    monkeypatch.setattr(C.time, "monotonic", lambda: clock[0])
    heard = _Heard(clock)
    monkeypatch.setattr(C.bluetooth, "async_last_service_info", heard.info)
    refreshes = []

    async def _refresh():
        refreshes.append(clock[0])

    monkeypatch.setattr(coord, "async_request_refresh", _refresh)

    clock[0] += 5                             # advertising right after our own attempt
    heard.at = clock[0]
    coord._check_advertising()
    await hass.async_block_till_done()
    assert coord.in_use_elsewhere is False and refreshes == []
    assert coord.link_state == "reconnecting"

    clock[0] += 120                           # silent for two minutes: the app has it
    coord._check_advertising()
    assert coord.in_use_elsewhere is True
    assert coord.link_state == "in_use_elsewhere"
    assert coord.seconds_since_advertisement == pytest.approx(120)
    coord._next_connect = clock[0] + 300
    with pytest.raises(UpdateFailed, match="another device"):
        await coord._async_update_data()

    clock[0] += 60                            # the app lets go; the heater is heard again
    heard.at, heard.name = clock[0], "P Rinnai RX  1234567890"
    coord._check_advertising()
    await hass.async_block_till_done()
    assert coord.in_use_elsewhere is False
    assert len(refreshes) == 1 and coord._next_connect == 0
    assert coord.unbonded_hint is True

    coord._backoff = 300                      # failed again; no reconnect storm
    clock[0] += 40
    coord._check_advertising()                # silent again
    clock[0] += 5
    heard.at = clock[0]
    coord._check_advertising()                # back within the minute
    await hass.async_block_till_done()
    assert len(refreshes) == 1


async def test_own_connection_is_not_in_use_elsewhere(coord, heater, monkeypatch):
    await coord._async_update_data()          # we hold the link; heater is silent
    clock = [C.time.monotonic()]
    monkeypatch.setattr(C.time, "monotonic", lambda: clock[0])
    heard = _Heard(clock)
    monkeypatch.setattr(C.bluetooth, "async_last_service_info", heard.info)
    clock[0] += 600
    coord._check_advertising()
    assert coord.in_use_elsewhere is False and coord.link_state == "connected"

    await coord.async_release_link(15)        # just let go: silence so far was ours
    coord._check_advertising()
    assert coord.in_use_elsewhere is False
    clock[0] += 45                            # still silent: someone else connected
    coord._check_advertising()
    assert coord.in_use_elsewhere is True
    assert coord.link_state == "released"     # released wins; the binary sensor shows the rest


async def test_unknown_history_is_not_in_use(coord, heater):
    heater.silent = True
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    coord._check_advertising()                # no Bluetooth manager in this test
    assert coord.seconds_since_advertisement is None
    assert coord.in_use_elsewhere is False


async def test_setup_survives_an_unreachable_heater(hass, heater):
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import entity_registry as er

    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

    hass.config.units = US_CUSTOMARY_SYSTEM
    heater.silent = True
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDRESS}, unique_id=ADDRESS)
    entry.add_to_hass(hass)

    async def _connect(*_a, **_k):
        heater.is_connected = True
        heater.reasm.reset()
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
        ent = reg.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_outgoing_temp")
        assert hass.states.get(ent).state == "unavailable"
        ent = reg.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_last_failure_stage")
        assert hass.states.get(ent).state == "handshake"
        link = reg.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_link_state")
        assert hass.states.get(link).state == "reconnecting"
        busy = reg.async_get_entity_id("binary_sensor", DOMAIN, f"{ADDRESS}_in_use_elsewhere")
        assert hass.states.get(busy).state == "off"

        button = reg.async_get_entity_id("button", DOMAIN, f"{ADDRESS}_reconnect")
        with pytest.raises(HomeAssistantError, match="Bluetooth LED"):   # still unreachable
            await hass.services.async_call(
                "button", "press", {"entity_id": button}, blocking=True
            )

        heater.silent = False                 # heater comes back; the button reconnects
        await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
        await hass.async_block_till_done()
        ent = reg.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_outgoing_temp")
        assert hass.states.get(ent).state == "96"
        assert hass.states.get(link).state == "connected"

        switch = reg.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_release_link")
        await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
        await hass.async_block_till_done()
        assert hass.states.get(switch).state == "on"
        assert hass.states.get(link).state == "released"
        await hass.services.async_call(DOMAIN, "reconnect", {}, blocking=True)
        await hass.async_block_till_done()
        assert hass.states.get(switch).state == "off"

        await hass.services.async_call(DOMAIN, "release_link", {"minutes": 5}, blocking=True)
        assert entry.runtime_data.released
        assert await hass.config_entries.async_unload(entry.entry_id)
