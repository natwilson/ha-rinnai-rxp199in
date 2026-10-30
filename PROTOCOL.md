# Rinnai RX / RXP BLE Protocol

Reverse-engineered from a PacketLogger (BTSnoop) capture of the Rinnai Central
iOS app talking to an **RXP199iN**. Every field below was validated against the
app's own Diagnostic screen. Nothing here is guessed; unmapped fields are listed
as unmapped.

This document is the durable artifact. The integration is just one consumer of
it — an ESP32 display, a CLI tool, or an ESPHome component can be written from
this file alone.

---

## 1. Transport

| Item | Value |
|---|---|
| Service | `C0850001-ECA2-441E-9F57-CFAB262FE9C0` |
| Command characteristic | `C0850003-...` — write **without response** |
| Response characteristic | `C0850002-...` — **notify** |
| Encryption | **Required.** Characteristics need a bonded link |
| Device name | `Rinnai RX  <serial>` (two spaces) |
| Address type | random |

A second service, `0000D0FF-3C17-D293-8E48-14FE2E4DA212` (chars `FFD1`–`FFE0`),
is readable **without** bonding but is **static** — module ID/metadata only. It
does not change when the unit fires. Ignore it.

### Bonding (the hard part)

The heater accepts a bond **only while in pairing mode**, opened by pressing the
**Bluetooth button on the front of the unit** (LED blinks). Without that press,
pairing fails with `org.bluez.Error.AuthenticationFailed` — on any adapter. This
is not an adapter problem, and there is no passkey (Just Works).

```bash
# integration disabled, phone app closed, then:
bluetoothctl
remove AA:BB:CC:DD:EE:FF     # clear any stale record
power on
agent on
default-agent
scan on                      # wait for the device to appear
# --- press the Bluetooth button on the unit now ---
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
info AA:BB:CC:DD:EE:FF       # want Paired: yes / Bonded: yes
```

Notes:
- `trust` succeeds even when no bond exists. Only `Bonded: yes` counts.
- **Bonds are stored per adapter.** Re-pair if you change dongles.
- The device stops advertising while connected, so a lingering `bluetoothctl`
  session will block Home Assistant from connecting.
- Only one central at a time. Keep the phone app closed during normal operation.

---

## 2. Frame format

```
<STX> "03" "\" <REG> <ASCII-hex data> <ETX> <checksum> <CR>
 0x02  addr       reg                  0x03   2 chars    0x0d
```

- **Address** is always `03` (ASCII `0x30 0x33`).
- **Data** is ASCII hex characters, uppercase, or empty for bare reads.
- **Checksum** = low byte of the sum of every byte from the address through
  `<ETX>` inclusive, formatted as two uppercase hex chars.

```python
def checksum(body: bytes) -> int:      # body = addr .. ETX inclusive
    return sum(body) & 0xFF
```

### GATT fragmentation

App-layer frames are split across GATT packets, 18 body bytes per fragment:

| Fragment | Header |
|---|---|
| first | `[0x01][total_count]` then body |
| subsequent | `[seq]` then body (seq is 1-based, 2, 3, …) |

Applies to both directions. **Reassemble by scanning for a complete
`<STX>…<CR>` and verifying the checksum**, not by trusting the fragment count —
fragments occasionally arrive duplicated, which produces an over-long frame that
must be rejected.

### Verified example frames

```
query  \L : 01 01 02 30 33 5C 4C 03 30 45 0D          ("\x0203\L\x030E\r")
query  \K : 0102 0230335C4B303030...  +  02 30303030300336440D   (18 zeros of data)
query  \I : 0102 0230335C4930303030... + 02 30303030...0345420D (26 zeros)
```

These reproduce the app's writes byte-for-byte.

---

## 3. Session handshake — required

**The app writes `\I` with 26 zeros at the start of every session, before
polling anything.** Skipping it works at first, then the heater silently stops
answering `\L` (nothing arrives at all) while short `\K` queries keep working.

Send `\I` immediately after subscribing to notifications. Re-sending it on the
same link when `\L` stops answering usually restores the session without a
reconnect; the integration does this since 1.0.0.

---

## 4. Registers

| Reg | Data sent | Reply | Purpose |
|---|---|---|---|
| `\I` | 26 zeros | short | **Session handshake.** Required at connect |
| `\K` | 18 zeros | 18 chars | Status + setpoint + mode flags |
| `\L` | none | 98 chars | **Live diagnostics.** The main payload |
| `\M` | none | 44 chars | Unknown. `3005` repeated ×9, static since first capture |
| `\N` | none | 88 chars | Unit info + serial. Not static — see §7 |
| `\P` | none | 98 chars | Diagnostic/error codes. All zeros = no active code |

`\K` and `\I` are the only registers the app writes **with a data payload**,
which strongly suggests `\K` is also the **command** register (setpoint changes,
recirculation). The command byte positions are **not yet known** — see §8.

---

## 5. `\L` — live diagnostics (98 hex chars / 49 bytes)

**This frame has two sections with different alignment.** Words 0–7 are 16-bit
big-endian at **even** offsets; everything after byte 16 is 16-bit big-endian at
**odd** offsets. Missing that cost a lot of time — a word-aligned scan finds
nothing past byte 16.

### Section A — even-aligned words (bytes 0–15)

| Word | Bytes | Field | Unit |
|---|---|---|---|
| w0 | 0–1 | Fan Frequency | Hz |
| w1 | 2–3 | Fan Current | ×10 → mA |
| w2 | 4–5 | Water Flow Rate | ×0.1 → gal/min |
| w3 | 6–7 | Outgoing Temperature | °F |
| w4 | 8–9 | HEX Outlet Temperature | °F |
| w5 | 10–11 | Inlet Temperature | °F |
| w6 | 12–13 | Exhaust Temperature | °F |
| w7 | 14–15 | Freeze Protection Temperature | °F |

### Section B — odd-aligned words (byte 17 onward)

| Bytes | Field | Unit |
|---|---|---|
| 19–20 | Bypass Flow Control Position | degrees |
| 21–22 | Internal pump **installed** (capability flag, always 1) | bool |
| 23–24 | Combustion Hours | ×100 |
| 25–26 | Combustion Cycles | ×100 |
| 27–28 | Pump Hours | ×100 |
| 29–30 | Pump Cycles | ×100 |
| 33–34 | Water Through Heater | gallons |
| 37–38 | Total Heat Energy | ×100 → kcal |
| 41–42 | Lime Detecting Temperature | °F |

Counters have genuine **100-unit hardware resolution** — the app's round numbers
(7300 cycles, 400 hours) are the register granularity, not app rounding.

### Unmapped in `\L`

| Bytes | Observed | Guess |
|---|---|---|
| 16 | constant `8` | Water Flow Control Position ("Open")? |
| 17–18 | always 0 | ? |
| 31–32 | always 0 | Total Bath Fill Amount? |
| 35–36 | always 0 | ? |
| 39–40 | 2188, later 2591; did not move during a 13-minute burn | slow counter |
| 43–48 | `01A7 01A7 0000` | constants / config |

**Water Through Heater (33–34)** rose by 10 during a shower plus recirculation
run that moved about 10 gallons (cross-checked against Total Heat Energy and the
temperature rise), and not at all while idle. The lifetime value (30,031 gal over
500 combustion hours, about 1 gal/min) fits too. It counts recirculated water as
well as water drawn at a tap.

**Pump Frequency was not found.** All three always-zero candidates stayed zero
even during a full recirculation run, so either the pump is fixed-speed and
truly reports 0 Hz, or the field is elsewhere. A second recirculation run
(during the 1.1.0 shower test) again moved no byte that the burner alone does not
explain. The app exposes a **recirculation motor setting (High / Medium /
Low)**; it is likely stored somewhere, possibly byte 16. To find it, change the
setting in the app (with Home Assistant's integration disabled, since only one
client can connect) and compare raw frames before and after.

### Observed: shower with a recirculation run first

- Recirculation fires the burner and the heater reports pump flow as
  Water Flow Rate (1.2 gal/min here) with no tap open. `\K` byte 1 = 2 for the
  run, then back to 1 while combustion continues for the shower.
- While firing: fan 100–200 Hz, outgoing held within ±3 °F of setpoint, bypass
  valve 11–22° (35° at idle).
- After the tap closes: `\K` byte 6 drops to 0 immediately, and the fan runs
  at 300 Hz (word 0 = `012C`) for about 3 minutes of purge.

### Worked example

```
0000000000000060 0063004C0055 0050 08 0000 0023 0001 0004 0049 0000 0003 0000
5B29 0000 75B2 088C 0064 01A7 01A7 0000
```
→ fan 0 Hz, 0 mA, flow 0.0, outgoing 96 °F, HEX 99 °F, inlet 76 °F,
exhaust 85 °F, freeze 80 °F, bypass 35°, combustion hours 400,
combustion cycles 7300, pump hours 0, pump cycles 300,
total heat energy 3,013,000 kcal, lime 100 °F

---

## 6. `\K` — status (18 hex chars / 9 bytes)

```
02 01 01 01 78 02 00 00 00
 0  1  2  3  4  5  6  7  8
```

| Byte | Field | Values |
|---|---|---|
| 1 | Operating mode | `1` idle, `2` recirculation pump running |
| 4 | **Setpoint temperature** °F | `0x78` = 120 (confirmed: changing to 125 was reflected) |
| 6 | **Combustion** | `1` burning, `0` idle **and during post-combustion purge** |

Byte 6 is the authoritative combustion flag. Do **not** infer combustion from
fan speed: the fan runs at 300–360 Hz during purge with combustion off.

Bytes 0, 2, 3, 5, 7, 8 have never varied.

---

## 7. `\N` and `\P`

**`\P`** — all zeros in every capture, matching the app's "No Active Code".
Treat any non-zero content as an active fault code. `\P` is as long as `\L`, so
poll it sparingly (every ~10 minutes).

**`\N`** — 88 chars. Serial number is 10 ASCII digits at offset 10. Captured
frames **differ from each other** in a field that looks like a page/record
index, suggesting `\N` backs the app's *Diagnostic Code History* screen. Reading
past fault records by writing `\N` with an index is plausible but unconfirmed.

---

## 8. Control — not implemented, and why

Everything shipped is read-only. The write path itself is proven: our query
frames, fragmentation and checksums reproduce the app's writes byte-for-byte, so
a **correct** command will be accepted.

What is missing is *which bytes encode which command*. `\K` is almost certainly
the command register. Guessing byte positions means writing unknown commands to
a gas appliance, which is not worth it.

**To finish it safely**, one capture is enough:

1. Start a PacketLogger iOS trace (see §9).
2. In the Rinnai Central app: press the **recirculation** control.
3. Separately, change the **setpoint** by a known amount (e.g. 120 → 125).
4. Export as **BTSnoop** and diff those `\K` writes against the idle `\K` write.

The bytes that differ are the command encoding. With two or three examples, a
recirculation `switch` and a setpoint `number` entity become straightforward.

---

## 9. Re-capturing traffic (if ever needed)

- **PacketLogger** ships in **Additional Tools for Xcode 15** (~30 MB, not Xcode
  itself). Version 15 runs on macOS Ventura 13.5+.
- Install Apple's **Bluetooth logging profile**: download it on the Mac,
  **AirDrop** it to the iPhone, then **reboot the phone with USB disconnected**.
  Downloading it directly on the phone produces an empty capture.
- Confirm the **pulse icon** at the top of the iPhone screen before doing
  anything — no icon means nothing is being logged.
- File → New iOS Trace, reproduce actions with pauses between them, export as
  **BTSnoop**.
- Parse: BTSnoop → HCI ACL → L2CAP CID `0x0004` (ATT) → opcode `0x1B`
  (notifications) and `0x52` (write commands).

Much of the time you do **not** need a capture: enable the **Raw Diagnostic
Frame L** and **Raw Status Frame K** sensors in Home Assistant and read the hex
straight from the entity history. Copy it **as text** — OCR from a screenshot
mangles long hex strings.
