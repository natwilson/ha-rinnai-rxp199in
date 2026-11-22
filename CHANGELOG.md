# Changelog

Newest first. Each entry records *why*, since most of these were hard-won.

## 1.5.0 — connection robustness, and sharing the link
Prompted by a morning lost to re-bonding after the phone app was used
(2026-11-22). The integration failed setup with "did not answer the `\I`
session handshake", and the same error appeared once more *after* a good
bond, then cleared by itself: so that error is not proof of a lost bond.
In hindsight the bond was probably never lost that morning; the host's
record was removed on the strength of that one error. The heater was later
shown to hold the app's bond and Home Assistant's at the same time.

- **`\I` handshake retried** three times on the same link, and a `\K` reply
  counts as an open session, before the link is dropped.
- **Setup no longer fails when the heater is unreachable.** Entities load
  unavailable and the coordinator keeps retrying, instead of "Failed setup,
  will retry" with every entity gone.
- **Shows when another device has the heater.** It only advertises while
  nothing is connected, so 30 s of silence while Home Assistant is not
  connected turns on **In Use By Another Device** and sets the link state to
  `in_use_elsewhere`; failure messages say so too. Off, out of range and
  Bluetooth-off look the same.
- **Reconnects when the heater is heard again.** The backoff wait (up to
  5 min) is skipped, at most once a minute. The last-heard time is read from
  Home Assistant's Bluetooth history every 5 s, because its advertisement
  callbacks are skipped when the advertisement has not changed.
- **Release Bluetooth Link** switch, `rinnai_ble.release_link` action and
  **Reconnect Now** button / `rinnai_ble.reconnect` action, to keep Home
  Assistant off the single connection without disabling the integration.
  Tested end to end: released, the Rinnai app connected with its own pairing
  (no button press), was disconnected (once with its Disconnect button, once
  by force-closing it), and Home Assistant resumed on its existing bond both
  times. The heater held both bonds.
- **Reconnect Now explains a failure.** If the press cannot reach the heater
  it raises an error with the reason and how to read the heater's Bluetooth
  LED: solid means a device is connected, off means the heater is free.
- **Lost bond detection.** A subscribe still refused after `pair()`, or repeated silent
  links (3 with the `P ` name, 10 without), raises a repair notice with the
  pairing steps. It clears on the next good poll. The `P ` prefix is a hint
  only: it came and went during pairing.
- **The `pair()` retry after a refused subscribe stays.** It was removed
  during 1.5.0 development on the theory that it could only time out, and
  the integration then could not reconnect after a re-pair even though the
  same bond subscribed fine from `bluetoothctl`. With a good bond the link
  can come up unencrypted, and `pair()` is what encrypts it.
- **Health sensors:** link state, last failure stage (reason as attribute),
  connection count, last good frame. Available while the heater is not.
- **On-demand link option.** Disconnects after every idle poll and holds the
  link only while the heater is active, so the app can connect between
  polls. Also a way to soak-test repeated connect/disconnect cycles.
- Bonding notes: the one-shot `bluetoothctl pair` hung in pairing mode; the
  interactive session with `agent on` worked first time.

## 1.4.2 — Rinnai icon
- Ships the Rinnai icon and logo in `custom_components/rinnai_ble/brand/`, which
  Home Assistant 2026.3 and newer use for custom integrations. Older versions
  keep showing the placeholder.

## 1.4.1 — discover heaters that advertise as `P Rinnai RX …`
- Some units (reported on an RX199i, issue #1) advertise the name
  `P Rinnai RX  <serial>` and no service UUID, so neither the manifest matcher
  nor the manual picker found them. The name is now matched anywhere in the
  string, and the manifest has local-name matchers for both forms.
- Same report: **Internal Pump Installed** reads on for that non-pump RX199i,
  so that flag may not mean what its name says. Not changed yet; more data
  needed.

## 1.4.0 — recirculation start and setpoint, via an opt-in `\K` write
- **`rinnai_ble.experimental_write_k`** action. Sends the app's all-zero `\K`
  query with exactly one byte changed (mode byte 1, or setpoint byte 4) and
  returns the status before and after. Refuses every other byte and value, and
  needs `confirm: true`. The integration stays read-only unless it is called.
- **It works.** On an RXP199iN, `mode: 2` starts a recirculation run that the
  heater ends itself (73 s from a cold loop), and `setpoint` sets the setpoint
  (120-140 °F confirmed on the unit display; below 120 is clamped in crossover
  mode; ignored while water is flowing). PROTOCOL.md §8 has the full results.
- Why: recirculation cannot be captured from the app (1.3.0), so the only
  software route left was to test whether the read layout is also the write
  layout. It is, for these two bytes.
- README: dashboard screenshot, the dashboard itself in `extras/dashboard.yaml`,
  and a script example for a recirculation button.

## 1.3.0 — false starts and fast polling while active
- **False Start** event entity and **False Starts Today** sensor. A false
  start is a fan run with no combustion, pump run or heat exchanger warming:
  a flow blip woke the heater but the burner never lit. Four days of history
  on an RXP199iN showed about 15, mostly about 30 s long.
- **Fast polling while active** (option, on by default): every 5 s while the
  fan, burner, pump or flow is active, back to the poll interval when idle or
  after a failed poll. At 30 s, short burns and false starts were
  indistinguishable.
- Reconnect backoff now starts from the configured interval, not the current
  (possibly fast) one.
- PROTOCOL.md: false-start observations; corrected §8 — the Rinnai app cannot
  start recirculation (only a paired BLE button can), so it can't be captured.

## 1.2.0 — low-flow lockout detection
- **Heat Demand** (BTU/h), **Low-Fire Margin** (%), **HEX Overshoot** and a
  **Silent Lockout** problem sensor, for the summer low-flow overheat cut-out
  that sends cold water without a fault code. Logic lives in `analysis.py`
  (no Home Assistant imports) with its own tests.
- **Minimum firing rate** option (default 15,000 BTU/h, RXP199iN).
- **Blueprint** `silent_lockout_alert` for notifications on lockout and recovery.
- **Temperature Rise** now uses the temperature-difference device class, so
  Celsius users see a correct converted rise (previously converted as an
  absolute temperature).

## 1.1.0 — Water Through Heater, findings from a live shower test
- **Water Through Heater** sensor (`\L` bytes 33–34, gallons, lifetime). Rose
  by 10 during a shower plus recirculation run of about 10 gallons, consistent
  with Total Heat Energy and the temperature rise. Counts recirculated water
  too, so it is not a water meter.
- **Unknown L Byte 16** and **Unknown L Counter 39–40** diagnostic sensors,
  disabled by default, so changes (for example the app's recirculation motor
  High/Medium/Low setting) can be spotted without a packet capture.
- Confirmed live: `\K` byte 1 = 2 during a button-started recirculation run;
  recirculation fires the burner and reports pump flow; the combustion flag
  drops to 0 during the 300 Hz post-purge. 42/42 polls succeeded over 21 minutes.

## 1.0.1 — hard timeouts on every Bluetooth step
Found on the first real install: setup hung for minutes without ever
connecting, blocking Home Assistant's startup, because a BlueZ/D-Bus call
had no timeout of its own. Every Bluetooth step is now bounded: the whole
connect (75 s, including retries), subscribe/pair (15 s each), disconnect
(10 s), each query (6 s) and each poll (60 s). A stuck step now fails with
"Bluetooth <step> step timed out" and goes through the normal backoff.
Debug logging names each connect stage.

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
