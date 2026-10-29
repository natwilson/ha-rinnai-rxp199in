# Changelog

Newest first. Each entry records *why*, since most of these were hard-won.

## 1.0.0 — connection layer rewrite
The protocol work was solid; the instability was in how the link was managed.
The coordinator and reassembler were rewritten; the codec's parsers, entities
and unique IDs are unchanged, so upgrading keeps entity IDs and history.

- **A failed poll is no longer an outage.** Previously any single failed poll
  raised `UpdateFailed` and flipped every entity to Unavailable, which was most
  of the visible "disconnects". Now last-good data is served until nothing
  valid has arrived for 5 minutes.
- **Fragments are slotted by sequence number** using the count in the first
  fragment. A duplicated fragment used to corrupt the whole `\L` frame (it was
  appended twice and failed its checksum); now it just overwrites its own slot.
- **`\I` is re-sent on a stalled `\L`** before giving up on the link (the
  keepalive PROTOCOL.md §3 suggested but was never tried). The link is rebuilt
  only after three consecutive stalls or a real Bluetooth error.
- **Reconnect backoff** (30 s doubling to 5 min) instead of reconnecting every
  poll, which kept a shared CSR8510 adapter busy for other devices.
- **`pair()` is no longer called on every connect**, only once if the link
  turns out not to be encrypted, with a clear "not bonded" error.
- Clean shutdown on unload/restart; `runtime_data`, shared base entity,
  modern options flow. Minimum Home Assistant 2025.2.
- Test suite: codec tests plus coordinator tests against a simulated heater
  (duplicated fragments, stalls, dropped links, stale data, backoff).
- Packaged for HACS. Heater MAC removed from the docs.

## 0.10.1 — fix the reconnect crash loop (hundreds/thousands of Unavailable/day)
A real bug, mine. When `\L` stalled twice, the reconnect logic set
`self._client = None` **mid-cycle**, then the same cycle immediately tried to
query `\K`, calling `write_gatt_char` on the now-`None` client:

    Error polling Rinnai heater: 'NoneType' object has no attribute 'write_gatt_char'

That crash failed the whole update, and the pattern repeated every poll — the
"reconnect" meant to *fix* stalls was instead guaranteeing them, flipping the
device to Unavailable and back on every cycle.

Fixes:
- `_query` now returns `None` immediately if the client is missing or
  disconnected, instead of crashing.
- A stalled `\L` no longer tears the link down mid-cycle. `\K` is still read
  (combustion/setpoint stay live), and teardown is deferred to the **end** of
  the cycle so the **next** cycle reconnects cleanly. Threshold raised 2 → 3.
- New `_teardown()` helper: null the client, reset the reassembler, disconnect —
  in that order, used everywhere.
- The `\I` handshake reply is now **verified**. A connected-but-unresponsive
  session (the real source of the endless stalls) is dropped immediately rather
  than polled, so the next cycle starts fresh.

If Unavailable events drop from thousands/day to near zero, this was it. Any
residual stalls are then genuine contention — most likely the **phone app**
periodically grabbing the heater's single connection slot. Force-quit the app
(or disable the recirc schedule in it) and compare.
## 0.10.0 — strict adapter pinning
The adapter option now *locks*. Previously, pinning still allowed two fall-through
paths onto another radio: the no-advertisement fallback, and a
pinned-but-not-visible case that dropped through to "any adapter" — either could
land on an ESPHome proxy, which has no bond, breaking every `\L` sensor.

Now, when an adapter is pinned:
- the connection uses **only** that adapter;
- if it cannot currently reach the heater, the integration waits (reports
  unavailable) rather than roaming onto a proxy;
- "Automatic" still prefers local adapters and only ever uses a proxy when no
  local adapter can see the device at all.

The **Options → adapter** picker now lists *all* adapters (from the manager,
tagged `[USB/local]` or `[proxy]`), so your USB dongle appears even when the
heater is mid-connection and not advertising. Pin the `[USB/local]` one.

## 0.9.2
Restored `BinarySensorDeviceClass.HEAT` on Combustion (reverts 0.9.1). All three
binary sensors carry semantic device classes.

## 0.9.1
Removed the HEAT device class to get plain On/Off. Reverted in 0.9.2.

## 0.9.0
- **Diagnostic Code Active / Diagnostic Code** from `\P`. All zeros = "No Active
  Code", so non-zero is a fault. Records intermittent faults that clear before
  you can inspect the unit. Polled every ~10 min (`\P` is as long as `\L`).
- **Temperature Rise** (outgoing − inlet), computed locally.
- Documented what `\N` and `\M` are and are not.

## 0.8.1 — the fix for the long-running stalls
Failure detail showed *no notification data arrived at all* — the heater simply
stopped answering `\L` while `\K` kept working. Comparing against the app's
capture revealed it writes **`\I` (26 zeros) at the start of every session**,
which we never replicated. Added the handshake on connect (byte-for-byte
identical to the app) plus auto-reconnect after two consecutive `\L` stalls.
**Stable since.**

## 0.8.0
- **Recirculation Pump** binary sensor — `\K` byte 1 reads `2` while running.
- Pump Frequency searched for and **not found**; left unmapped rather than
  guessed.
- Noted: a recirc press actually fires the burner.
- Per-attempt timeout 6 s to bound total poll time.

## 0.7.1 / 0.7.2
- Corrected a wrong theory: `00:1A:7D` is Cambridge Silicon Radio, and a
  *genuine* CSR8510 adapter reports it too — the adapter was fine. The decisive
  evidence was that `\K` worked, and `\K`/`\L` share one encrypted characteristic.
- Timeout 4 s → 10 s (short windows were truncating slow fragments).
- Failure logging now reports **what arrived**, separating "nothing" from
  "partial" — this is what led to 0.8.1.
- Benign connect/pair messages moved to debug.

## 0.7.0
Added adapter pinning (`CONF_ADAPTER`) and an adapter picker, since **bonds are
per adapter**. Useful, though not the cause of the failures at the time.

## 0.6.0 / 0.6.1
- "Internal Pump" corrected to **Internal Pump Installed** — the field is a
  capability flag (always 1), not run state.
- Setpoint confirmed (changing the unit to 125 °F was reflected).
- Configurable poll interval via options flow.
- Reassembler cleared before each attempt.

## 0.5.1
Retries per poll and a last-known-good cache, so one dropped `\L` no longer
blanks every sensor.

## 0.5.0 — the big decode
Discovered the `\L` frame has **two alignments**: words 0–7 at even offsets,
everything after byte 16 at **odd** offsets. That unlocked Combustion
Cycles/Hours, Pump Cycles/Hours, Bypass Position and the pump capability flag.
Counters store hundreds (hence the app's round numbers).

## 0.4.0
- **Combustion** read from `\K` byte 6 instead of derived. The fan runs during
  post-combustion purge, which the derived rule got wrong.
- **Total Heat Energy** from `\L` bytes 37–38 ×100 — confirmed twice against the
  app, 10× finer than the `\M` value, which turned out to be stale.

## 0.3.0 / 0.3.1
- Lime Detecting Temperature found at byte offset 42.
- Raw `\K` / `\L` diagnostic sensors added — these made every later decode
  possible without another packet capture.
- **Checksum verification** added after duplicated fragments produced a 103-char
  frame and bogus values. Corrupt frames are now dropped.

## 0.2.0 / 0.2.1
Prefer a local adapter over proxies; fall back to a direct lookup when the
device is not advertising (it stops while connected).

## 0.1.x
Initial release: codec verified byte-for-byte against captured app traffic,
coordinator, config flow, sensors. 0.1.1 added `pair()` after discovering the
characteristics require an encrypted link — the real fix turned out to be
pressing the unit's Bluetooth button.
