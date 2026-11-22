"""Connection and polling for the Rinnai BLE heater.

Design, in short:

* One persistent connection, opened with the \\I handshake the app sends.
* A failed poll is not an outage. Last-good data is kept and entities stay
  available until nothing valid has arrived for STALE_AFTER seconds.
* A stalled \\L is first retried after re-sending \\I on the same link. The
  link is only dropped after repeated stalls or a real Bluetooth error, and
  reconnects back off so a busy adapter is not hammered.
* While the heater is doing anything (fan, flow, burner, pump) it is polled
  every FAST_POLL_INTERVAL seconds, so short events such as false starts are
  seen; otherwise at the configured interval.
* The heater takes one connection at a time. The link can be handed to the
  Rinnai app or a technician (async_release_link), and in on-demand mode it
  is dropped after every idle poll. Bonds coexist: the app connected on its
  own pairing and this integration resumed on its existing bond.
* The heater only advertises while nothing is connected. Silence while we are
  not connected is reported as "in use elsewhere", and hearing it again is
  the signal to reconnect straight away.
* A setup or poll failure never unloads the entry; health (link state, last
  failure stage, connection count) is exposed for the diagnostic entities.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import analysis as A
from . import rinnai_protocol as R
from .const import (
    ADAPTER_AUTO,
    CONF_ADAPTER,
    CONF_ADDRESS,
    CONF_FAST_POLL,
    CONF_MIN_FIRING_RATE,
    CONF_ON_DEMAND,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STALE_AFTER = 300           # seconds without valid data before going unavailable
L_STALLS_BEFORE_RECONNECT = 3
FAULT_POLL_INTERVAL = 600   # \P is as long as \L, so read it sparingly
QUERY_TIMEOUT = 6.0
HANDSHAKE_TIMEOUT = 5.0
QUERY_GAP = 0.3             # pause between queries on the link
MAX_BACKOFF = 300
# Hard limits on Bluetooth calls. BlueZ/D-Bus calls can otherwise wait forever
# (seen in the field: setup hung for minutes with no connection made).
CONNECT_TIMEOUT = 75.0      # whole connect, including bleak-retry-connector retries
STEP_TIMEOUT = 15.0         # start_notify, pair
DISCONNECT_TIMEOUT = 10.0
POLL_TIMEOUT = 60.0         # one full poll cycle on an open link
FAST_POLL_INTERVAL = 5      # seconds, while the heater is active
K_EXPERIMENT_SETTLE = 2.0   # seconds between an experimental write and the read-back
# A freshly bonded link has been seen to ignore the first \\I and answer a
# minute later, so one unanswered handshake is not a reason to drop the link.
HANDSHAKE_ATTEMPTS = 3
# Links that connect and subscribe but never answer anything. With the "P "
# name (no bond stored on the heater) a few are enough to call the bond lost.
SILENT_LINKS_BOND_HINTED = 3
SILENT_LINKS_BOND_LOST = 10
# The heater advertises only while nothing is connected to it. Home Assistant
# skips its advertisement callbacks when the data has not changed, so the
# time it last heard the heater is read from the Bluetooth history instead.
ADV_STALE = 30.0            # not heard for this long = connected elsewhere (or off / out of range)
ADV_CHECK_INTERVAL = 5      # seconds between looks at the history
ADV_RECONNECT_MIN_GAP = 60.0  # at most one advertisement-triggered reconnect per minute
DEFAULT_RELEASE_MINUTES = 15

# Values of last_failure_stage / link_state (sensor options).
FAILURE_STAGES = ["none", "not_visible", "connect", "subscribe", "handshake", "not_bonded", "poll"]
LINK_STATES = ["connected", "idle", "released", "in_use_elsewhere", "reconnecting", "bond_lost"]


class _Transient(Exception):
    """A poll problem that does not need the link torn down."""


class RinnaiCoordinator(DataUpdateCoordinator[dict]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}:{entry.data[CONF_ADDRESS]}",
            update_interval=timedelta(seconds=interval),
        )
        self.address: str = entry.data[CONF_ADDRESS]
        # Bonds are stored per adapter, so a pinned adapter is strict.
        self.adapter: str = entry.options.get(CONF_ADAPTER, ADAPTER_AUTO)
        self.serial: str | None = None
        self.min_firing_rate: float = entry.options.get(
            CONF_MIN_FIRING_RATE, A.DEFAULT_MIN_FIRING_RATE
        )
        self._lockout = A.LockoutDetector()
        self._false_starts = A.FalseStartDetector()
        self._base_interval = timedelta(seconds=interval)
        self.fast_poll: bool = entry.options.get(CONF_FAST_POLL, True)
        self.false_starts_today = 0
        self._false_start_day = dt_util.now().date()
        self._false_start_seq = 0

        self._client: BleakClientWithServiceCache | None = None
        self._lock = asyncio.Lock()
        self._reasm = R.Reassembler()
        self._waiting: tuple[str | None, asyncio.Future] | None = None
        self._cache: dict = {}
        self._last_success: float | None = None
        self._last_fault_read: float | None = None
        self._l_stalls = 0
        self._backoff = 0.0
        self._next_connect = 0.0

        # Release the link after each idle poll instead of holding it.
        self.on_demand: bool = entry.options.get(CONF_ON_DEMAND, False)
        self._released_until: float | None = None
        # Health, for the diagnostic entities.
        self.connections = 0
        self.last_failure_stage = "none"
        self.last_failure: str | None = None
        self.last_good_frame = None          # aware datetime of the last valid poll
        self.bond_lost = False
        self._silent_links = 0
        self._adv_name: str | None = None
        self.seconds_since_advertisement: float | None = None
        self._was_advertising: bool | None = None
        self._was_in_use = False
        self._link_dropped_at = 0.0          # when our own connection last ended
        self._last_adv_reconnect = 0.0
        self._unsub_adv: CALLBACK_TYPE | None = None

    # ---- BLE plumbing --------------------------------------------------------

    @property
    def _pinned(self) -> bool:
        return bool(self.adapter) and self.adapter != ADAPTER_AUTO

    def _pick_device(self) -> BLEDevice | None:
        """Choose which adapter to connect through.

        Pinned: only that adapter, never another radio (it would have no bond).
        Automatic: the local adapter with the best signal; a proxy only if no
        local adapter can see the heater.
        """
        seen = bluetooth.async_scanner_devices_by_address(
            self.hass, self.address, connectable=True
        )
        if self._pinned:
            for dev in seen:
                if dev.scanner.source.upper() == self.adapter.upper():
                    return dev.ble_device
            return None

        if seen:
            local = [d for d in seen if not _is_remote(d.scanner)] or seen
            best = max(local, key=lambda d: d.advertisement.rssi or -127)
            return best.ble_device
        # The heater stops advertising while connected; fall back to the
        # last known device so a quick reconnect still works.
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    def _on_notify(self, _char, data: bytearray) -> None:
        for reg, payload in self._reasm.feed(bytes(data)):
            if self._waiting is None:
                continue
            want, fut = self._waiting
            if want in (None, reg) and not fut.done():
                fut.set_result(payload)

    def _on_disconnect(self, client) -> None:
        if client is self._client:
            _LOGGER.debug("Rinnai %s disconnected", self.address)
            self._client = None
            self._reasm.reset()
            self._link_dropped_at = time.monotonic()

    async def _connect(self) -> None:
        wait = self._next_connect - time.monotonic()
        if wait > 0:
            raise _Transient(f"waiting {wait:.0f}s before reconnecting")

        device = self._pick_device()
        if device is None:
            self._schedule_backoff()
            self._failed("not_visible")
            where = f"pinned adapter {self.adapter}" if self._pinned else "any adapter"
            raise _Transient(f"heater not currently visible to {where}")

        stage = "connect"
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                _LOGGER.debug("Rinnai %s: connecting", self.address)
                self._client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    self.address,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=3,
                )
                self._reasm.reset()
                stage = "subscribe"
                _LOGGER.debug("Rinnai %s: connected; subscribing", self.address)
                await self._start_notify()
                stage = "handshake"
                _LOGGER.debug("Rinnai %s: subscribed; sending \\I", self.address)
                if not await self._handshake(HANDSHAKE_ATTEMPTS, accept_status=True):
                    self._silent_links += 1
                    raise _Transient("heater did not answer the \\I session handshake")
        except TimeoutError as err:
            await self._teardown()
            self._schedule_backoff()
            self._failed(stage)
            raise _Transient(f"Bluetooth {stage} step timed out") from err
        except UpdateFailed:
            await self._teardown()
            self._schedule_backoff()
            self._failed("not_bonded")
            self._set_bond_lost(True)
            raise
        except Exception:
            await self._teardown()
            self._schedule_backoff()
            self._failed(stage)
            if self._silent_links >= SILENT_LINKS_BOND_LOST or (
                self._silent_links >= SILENT_LINKS_BOND_HINTED and self.unbonded_hint
            ):
                self._set_bond_lost(True)
            raise
        self._backoff = 0.0
        self.connections += 1
        _LOGGER.debug("Rinnai %s connected and session open", self.address)

    async def _start_notify(self) -> None:
        """Subscribe to responses, securing the link once if it is not encrypted.

        A refused subscribe does not by itself mean the bond is gone: with a
        good bond stored, the link can still come up unencrypted, and pair()
        on the bonded device is what encrypts it. (1.5.0 briefly dropped that
        retry and could not reconnect after a re-pair, while the same bond
        subscribed fine from bluetoothctl.) Only when the subscribe is still
        refused afterwards is the bond reported as lost; a real new pairing
        needs the heater's button and an interactive agent, so pair() cannot
        create one here.
        """
        assert self._client is not None
        try:
            async with asyncio.timeout(STEP_TIMEOUT):
                await self._client.start_notify(R.NOTIFY_UUID, self._on_notify)
            return
        except BleakError as err:
            if not _is_auth_error(err):
                raise
        _LOGGER.debug("Notify refused for lack of encryption; trying pair()")
        try:
            async with asyncio.timeout(STEP_TIMEOUT):
                await self._client.pair()
            async with asyncio.timeout(STEP_TIMEOUT):
                await self._client.start_notify(R.NOTIFY_UUID, self._on_notify)
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(
                "Heater refused an encrypted connection. It is not bonded with "
                "this adapter: press the Bluetooth button on the heater and pair "
                "again (see README, 'Bonding')."
            ) from err

    def _schedule_backoff(self) -> None:
        base = self._base_interval.total_seconds()
        self._backoff = min(MAX_BACKOFF, self._backoff * 2 if self._backoff else base)
        self._next_connect = time.monotonic() + self._backoff

    async def _teardown(self) -> None:
        client, self._client = self._client, None
        self._reasm.reset()
        if client is not None:
            self._link_dropped_at = time.monotonic()
            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def async_shutdown(self) -> None:
        if self._unsub_adv is not None:
            self._unsub_adv()
            self._unsub_adv = None
        await super().async_shutdown()
        await self._teardown()

    # ---- Health, bond state, advertisements ----------------------------------

    @property
    def unbonded_hint(self) -> bool:
        """The heater advertises "P Rinnai RX ..." while it has no bond stored.

        A hint only: the prefix was seen to come and go during pairing.
        """
        return bool(self._adv_name) and self._adv_name.startswith("P ")

    @property
    def released(self) -> bool:
        return self._released_until is not None and time.monotonic() < self._released_until

    @property
    def link_state(self) -> str:
        if self.released:
            return "released"
        if self._client is not None and self._client.is_connected:
            return "connected"
        if self.in_use_elsewhere:
            return "in_use_elsewhere"
        if self.bond_lost:
            return "bond_lost"
        if self.on_demand and self._backoff == 0 and self._last_success is not None:
            return "idle"
        return "reconnecting"

    def _failed(self, stage: str) -> None:
        self.last_failure_stage = stage

    def _notify_health(self) -> None:
        """Push health changes; the base class stays quiet on repeated failures."""
        self.hass.loop.call_soon(self.async_update_listeners)

    def _set_bond_lost(self, lost: bool) -> None:
        if lost == self.bond_lost:
            return
        self.bond_lost = lost
        issue_id = f"bond_lost_{self.address}"
        if lost:
            _LOGGER.warning(
                "Rinnai %s: the heater no longer accepts this adapter's bond; "
                "pair again (see README, 'Bonding')", self.address,
            )
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="bond_lost",
                translation_placeholders={"address": self.address},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @property
    def in_use_elsewhere(self) -> bool:
        """True when the heater has gone quiet while we are not connected.

        It advertises only while nothing is connected, so silence means another
        device (the Rinnai app, a technician's tool) holds the connection, or
        the heater's Bluetooth is off or out of range; those look the same
        from here. Unknown (never heard, no Bluetooth history) counts as False.
        """
        if self._client is not None:
            return False
        age = self.seconds_since_advertisement
        if age is None or age <= ADV_STALE:
            return False
        # It was silent because of our own connection until just now.
        return time.monotonic() - self._link_dropped_at > ADV_STALE

    def _advertisement_age(self) -> float | None:
        """Seconds since any connectable scanner last heard the heater."""
        try:
            info = bluetooth.async_last_service_info(
                self.hass, self.address, connectable=True
            )
        except Exception:  # noqa: BLE001  (no Bluetooth manager, e.g. in tests)
            return None
        if info is None:
            return None
        self._adv_name = info.name or self._adv_name
        return max(0.0, time.monotonic() - info.time)

    def async_start(self) -> None:
        """Start watching whether the heater is advertising."""
        self._unsub_adv = async_track_time_interval(
            self.hass, self._check_advertising, timedelta(seconds=ADV_CHECK_INTERVAL)
        )
        self._check_advertising()

    @callback
    def _check_advertising(self, _now=None) -> None:
        now = time.monotonic()
        age = self.seconds_since_advertisement = self._advertisement_age()

        in_use = self.in_use_elsewhere
        if in_use != self._was_in_use:
            self._was_in_use = in_use
            _LOGGER.info(
                "Rinnai %s: %s", self.address,
                "not advertising; another device is probably connected to it"
                if in_use else "advertising again",
            )
            self.async_update_listeners()

        advertising = age is not None and age <= ADV_STALE
        came_back = advertising and self._was_advertising is False
        self._was_advertising = advertising
        if (
            came_back
            and self._client is None
            and not self.released
            and self._backoff > 0                             # last attempt failed
            and now - self._link_dropped_at > ADV_STALE       # not our own link ending
            and now - self._last_adv_reconnect > ADV_RECONNECT_MIN_GAP
        ):
            _LOGGER.debug("Rinnai %s is advertising again; reconnecting now", self.address)
            self._last_adv_reconnect = now
            self._backoff = 0.0
            self._next_connect = 0.0
            self.hass.async_create_task(self.async_request_refresh())

    async def async_release_link(self, minutes: float = DEFAULT_RELEASE_MINUTES) -> None:
        """Disconnect and stay off the heater so the app or a technician can connect."""
        async with self._lock:
            self._released_until = time.monotonic() + minutes * 60
            await self._teardown()
        _LOGGER.info("Rinnai %s: Bluetooth link released for %s min", self.address, minutes)
        self.async_update_listeners()

    async def async_reconnect_now(self) -> None:
        """End a release or a backoff wait and connect on the next refresh."""
        self._released_until = None
        self._backoff = 0.0
        self._next_connect = 0.0
        await self.async_refresh()

    # ---- Request/response ----------------------------------------------------

    async def _query(
        self, reg: str, data: str = "", *, any_reply: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        client = self._client
        if client is None or not client.is_connected:
            raise BleakError("not connected")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting = (None if any_reply else reg, fut)
        self._reasm.reset()
        try:
            async with asyncio.timeout(timeout or QUERY_TIMEOUT):
                for frag in R.build_write(reg, data):
                    await client.write_gatt_char(R.WRITE_UUID, frag, response=False)
                return await fut
        except TimeoutError:
            _LOGGER.debug("No \\%s reply (%s)", reg, self._reasm.buffered)
            return None
        finally:
            self._waiting = None

    async def _read(self, reg: str, data: str = "", attempts: int = 2) -> str | None:
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(QUERY_GAP)
            if (reply := await self._query(reg, data)) is not None:
                return reply
        return None

    async def _handshake(self, attempts: int = 1, *, accept_status: bool = False) -> bool:
        """Open the session the way the app does: write \\I with 26 zeros.

        Any valid frame in response means the session is open. With
        accept_status, a \\K reply also counts: \\K keeps answering when \\I
        was missed, and a later \\L stall re-sends \\I anyway.
        """
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(QUERY_GAP)
            reply = await self._query(
                "I", "0" * 26, any_reply=True, timeout=HANDSHAKE_TIMEOUT
            )
            if reply is not None:
                return True
            if accept_status:
                await asyncio.sleep(QUERY_GAP)
                if await self._query("K", R.K_QUERY) is not None:
                    _LOGGER.debug("\\I unanswered but \\K replied; session treated as open")
                    return True
        return False

    # ---- Experimental write (PROTOCOL.md §8) ---------------------------------

    async def async_experimental_write_k(self, data: str) -> dict:
        r"""Write one \K payload and report the status before and after.

        Callers must build ``data`` with ``rinnai_protocol.build_k_experiment``,
        which is what keeps this to one-byte changes of known values.
        """
        async with self._lock:
            try:
                if self._client is None or not self._client.is_connected:
                    await self._connect()
                async with asyncio.timeout(POLL_TIMEOUT):
                    before = await self._read("K", R.K_QUERY)
                    await asyncio.sleep(QUERY_GAP)
                    reply = await self._query("K", data)
                    await asyncio.sleep(K_EXPERIMENT_SETTLE)
                    after = await self._read("K", R.K_QUERY)
            except (UpdateFailed, _Transient) as err:
                raise HomeAssistantError(str(err)) from err
            except Exception as err:  # noqa: BLE001  (bleak / D-Bus errors)
                await self._teardown()
                raise HomeAssistantError(f"{type(err).__name__}: {err}") from err
        _LOGGER.warning(
            "Rinnai experimental \\K write %s: before=%s reply=%s after=%s",
            data, before, reply, after,
        )
        return {
            "sent": data,
            "before": before,
            "reply": reply,
            "after": after,
            "replied": reply is not None,
            "status_changed": None not in (before, after) and before != after,
        }

    # ---- Polling -------------------------------------------------------------

    async def _poll(self) -> dict:
        fresh: dict = {}

        live = await self._read("L")
        if live is None:
            # The heater sometimes stops answering \L until the session is
            # re-opened; re-sending \I usually brings it back without a reconnect.
            _LOGGER.debug("\\L stalled; re-sending \\I on the same link")
            if await self._handshake():
                live = await self._read("L", attempts=1)
        if live is not None and (parsed := R.parse_L(live)):
            fresh.update(parsed)
            self._l_stalls = 0
        else:
            self._l_stalls += 1

        await asyncio.sleep(QUERY_GAP)
        if (status := await self._read("K", "0" * 18)) is not None:
            fresh.update(R.parse_K(status))

        now = time.monotonic()
        if self._last_fault_read is None or now - self._last_fault_read >= FAULT_POLL_INTERVAL:
            await asyncio.sleep(QUERY_GAP)
            if (fault := await self._read("P", attempts=1)) is not None:
                fresh.update(R.parse_P(fault))
                self._last_fault_read = now

        if self.serial is None:
            await asyncio.sleep(QUERY_GAP)
            if (info := await self._read("N", attempts=1)) is not None:
                self.serial = R.parse_N(info).get("serial")

        if self._l_stalls >= L_STALLS_BEFORE_RECONNECT:
            _LOGGER.warning(
                "Rinnai \\L has not answered for %d polls; reconnecting", self._l_stalls
            )
            self._l_stalls = 0
            await self._teardown()

        return fresh

    async def _async_update_data(self) -> dict:
        async with self._lock:
            if self._released_until is not None:
                if self.released:
                    return self._degraded("Bluetooth link released for another device")
                self._released_until = None
            try:
                if self._client is None or not self._client.is_connected:
                    await self._connect()
                async with asyncio.timeout(POLL_TIMEOUT):
                    fresh = await self._poll()
            except UpdateFailed as err:
                return self._degraded(str(err), hard=True)
            except _Transient as err:
                return self._degraded(str(err))
            except Exception as err:  # noqa: BLE001  (bleak / D-Bus / proxy errors)
                await self._teardown()
                if self.last_failure_stage == "none" or self._backoff == 0:
                    self._failed("poll")
                return self._degraded(f"{type(err).__name__}: {err}")

            if not fresh:
                self._failed("poll")
                return self._degraded("no valid frames returned")

            self._cache.update(fresh)
            self._derive(time.monotonic())
            self._last_success = time.monotonic()
            self.last_good_frame = dt_util.utcnow()
            self._silent_links = 0
            self._set_bond_lost(False)
            self._set_pace()
            if self.on_demand and not self._active():
                # Leave the heater free for the app, a technician or a button.
                await self._teardown()
            return dict(self._cache)

    def _active(self) -> bool:
        d = self._cache
        return bool(
            d.get("fan_frequency") or d.get("water_flow_rate")
            or d.get("combustion") or d.get("pump_running")
        )

    def _set_pace(self) -> None:
        """Poll fast while the heater is active, at the base interval otherwise."""
        active = self.fast_poll and self._active()
        self.update_interval = (
            timedelta(seconds=FAST_POLL_INTERVAL) if active else self._base_interval
        )

    def restore_false_starts(self, day, count: int) -> None:
        """Carry today's count over a restart (called by the sensor)."""
        if day == self._false_start_day:
            self.false_starts_today += count
            self._cache["false_starts_today"] = self.false_starts_today
            if self.data is not None:
                self.data["false_starts_today"] = self.false_starts_today

    def _derive(self, now: float) -> None:
        """Compute values that need more than one register, from the merged cache."""
        d = self._cache
        if "outgoing_temp" in d and "inlet_temp" in d:
            d["temperature_rise"] = d["outgoing_temp"] - d["inlet_temp"]
        if "hex_outlet_temp" in d and "outgoing_temp" in d:
            d["hex_overshoot"] = d["hex_outlet_temp"] - d["outgoing_temp"]
        flow = d.get("water_flow_rate")
        if flow is not None and "setpoint_temp" in d and "inlet_temp" in d:
            demand = A.heat_demand(flow, d["setpoint_temp"], d["inlet_temp"])
            d["heat_demand"] = demand
            d["firing_margin"] = A.firing_margin(demand, self.min_firing_rate)
        if flow is not None and "combustion" in d:
            d["silent_lockout"] = self._lockout.update(
                flow, d["combustion"], d.get("diagnostic_code_active"), now
            )

        today = dt_util.now().date()
        if today != self._false_start_day:
            self._false_start_day, self.false_starts_today = today, 0
        event = self._false_starts.update(
            d.get("fan_frequency"), d.get("combustion"), d.get("pump_running"),
            d.get("hex_outlet_temp"), d.get("inlet_temp"), flow, now,
        )
        if event is not None:
            self.false_starts_today += 1
            self._false_start_seq += 1
            d["last_false_start"] = {**event, "seq": self._false_start_seq}
            _LOGGER.debug("Rinnai false start: %s", event)
        d["false_starts_today"] = self.false_starts_today

    def _degraded(self, reason: str, hard: bool = False) -> dict:
        """Ride out a bad poll on last-good data, or fail once data is stale."""
        self.update_interval = self._base_interval
        if self.in_use_elsewhere:
            reason = (
                "The heater is not advertising, so another device (for example "
                "the Rinnai app) is probably connected to it, or its Bluetooth "
                f"is off or out of range. {reason}"
            )
        self.last_failure = reason
        self._notify_health()
        age = None if self._last_success is None else time.monotonic() - self._last_success
        if not hard and self._cache and age is not None and age < STALE_AFTER:
            _LOGGER.debug("Rinnai poll failed (%s); keeping data from %.0fs ago", reason, age)
            return dict(self._cache)
        raise UpdateFailed(reason)


def _is_remote(scanner) -> bool:
    try:
        from habluetooth import BaseHaRemoteScanner
    except ImportError:  # pragma: no cover
        return False
    return isinstance(scanner, BaseHaRemoteScanner)


def _is_auth_error(err: Exception) -> bool:
    text = str(err).lower()
    return any(s in text for s in ("encrypt", "authenticat", "insufficient", "not paired"))
