"""Connection and polling for the Rinnai BLE heater.

Design, in short:

* One persistent connection, opened with the \\I handshake the app sends.
* A failed poll is not an outage. Last-good data is kept and entities stay
  available until nothing valid has arrived for STALE_AFTER seconds.
* A stalled \\L is first retried after re-sending \\I on the same link. The
  link is only dropped after repeated stalls or a real Bluetooth error, and
  reconnects back off so a busy adapter is not hammered.
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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import rinnai_protocol as R
from .const import (
    ADAPTER_AUTO,
    CONF_ADAPTER,
    CONF_ADDRESS,
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

    async def _connect(self) -> None:
        wait = self._next_connect - time.monotonic()
        if wait > 0:
            raise _Transient(f"waiting {wait:.0f}s before reconnecting")

        device = self._pick_device()
        if device is None:
            self._schedule_backoff()
            where = f"pinned adapter {self.adapter}" if self._pinned else "any adapter"
            raise _Transient(f"heater not currently visible to {where}")

        _LOGGER.debug("Connecting to Rinnai %s", self.address)
        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self.address,
                disconnected_callback=self._on_disconnect,
                max_attempts=3,
            )
            self._reasm.reset()
            await self._start_notify()
            if not await self._handshake():
                raise _Transient("heater did not answer the \\I session handshake")
        except Exception:
            await self._teardown()
            self._schedule_backoff()
            raise
        self._backoff = 0.0
        _LOGGER.debug("Rinnai %s connected and session open", self.address)

    async def _start_notify(self) -> None:
        """Subscribe to responses, pairing once only if the link is not encrypted.

        A bonded adapter encrypts the link on demand, so pair() is normally
        unnecessary. If the bond is missing we try once, then explain.
        """
        assert self._client is not None
        try:
            await self._client.start_notify(R.NOTIFY_UUID, self._on_notify)
            return
        except BleakError as err:
            if not _is_auth_error(err):
                raise
        _LOGGER.debug("Notify refused for lack of encryption; trying pair()")
        try:
            await self._client.pair()
            await self._client.start_notify(R.NOTIFY_UUID, self._on_notify)
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(
                "Heater refused an encrypted connection. It is not bonded with "
                "this adapter: press the Bluetooth button on the heater and pair "
                "again (see README, 'Bonding')."
            ) from err

    def _schedule_backoff(self) -> None:
        base = self.update_interval.total_seconds() if self.update_interval else 30
        self._backoff = min(MAX_BACKOFF, self._backoff * 2 if self._backoff else base)
        self._next_connect = time.monotonic() + self._backoff

    async def _teardown(self) -> None:
        client, self._client = self._client, None
        self._reasm.reset()
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self._teardown()

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
            for frag in R.build_write(reg, data):
                await client.write_gatt_char(R.WRITE_UUID, frag, response=False)
            return await asyncio.wait_for(fut, timeout or QUERY_TIMEOUT)
        except asyncio.TimeoutError:
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

    async def _handshake(self) -> bool:
        # The app writes \I with 26 zeros at the start of every session. Any
        # valid frame in response means the session is open.
        reply = await self._query("I", "0" * 26, any_reply=True, timeout=HANDSHAKE_TIMEOUT)
        return reply is not None

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
            try:
                if self._client is None or not self._client.is_connected:
                    await self._connect()
                fresh = await self._poll()
            except UpdateFailed as err:
                return self._degraded(str(err), hard=True)
            except _Transient as err:
                return self._degraded(str(err))
            except Exception as err:  # noqa: BLE001  (bleak / D-Bus / proxy errors)
                await self._teardown()
                return self._degraded(f"{type(err).__name__}: {err}")

        if not fresh:
            return self._degraded("no valid frames returned")

        if "outgoing_temp" in fresh and "inlet_temp" in fresh:
            fresh["temperature_rise"] = fresh["outgoing_temp"] - fresh["inlet_temp"]
        self._cache.update(fresh)
        self._last_success = time.monotonic()
        return dict(self._cache)

    def _degraded(self, reason: str, hard: bool = False) -> dict:
        """Ride out a bad poll on last-good data, or fail once data is stale."""
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
