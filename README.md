# Rinnai RXP199iN water heater for Home Assistant (local Bluetooth)

A Home Assistant integration that reads a **Rinnai SENSEI RX/RXP** tankless
water heater directly over its built-in Bluetooth. No cloud, no Wi-Fi module, no
Rinnai account. Developed and tested against an **RXP199iN**; other RX/RXP
models with the same Bluetooth module will likely work.

The protocol was reverse-engineered from scratch and is documented in
**[PROTOCOL.md](PROTOCOL.md)**. Read-only for now (see PROTOCOL.md §8 for what
control would take).

## What you get

| Type | Entities |
|---|---|
| Sensors | Outgoing, Inlet, HEX Outlet, Exhaust and Lime Detecting temperatures, Water Flow Rate, Temperature Rise, Water Through Heater (lifetime gallons, includes recirculation) |
| Diagnostic sensors | Setpoint, Freeze Protection temperature, Fan Frequency/Current, Bypass Position, Combustion and Pump Cycles/Hours, Total Heat Energy, Diagnostic Code, raw `\K`/`\L` frames and two not-yet-decoded `\L` fields (disabled by default) |
| Binary sensors | Combustion, Recirculation Pump, Diagnostic Code Active, Internal Pump Installed |

## Requirements

- Home Assistant 2025.2 or newer.
- A Bluetooth adapter on the Home Assistant host within range of the heater.
  **A local USB adapter is strongly preferred**; bonding through an ESPHome
  proxy is unreliable for this device.
- Ideally an adapter **dedicated** to the heater, or at least one that is not
  also holding connections to other devices. The heater keeps one connection
  open permanently, and older CSR8510 dongles handle only one connection
  attempt at a time, so other Bluetooth devices queue behind it. A Realtek
  RTL8761B-based adapter is a good choice.
- Physical access to the heater once, to press its Bluetooth button.

## Install

**HACS (recommended):** HACS → ⋮ → Custom repositories → add this repository's
URL as an *Integration*, install **Rinnai RXP199iN Water Heater (BLE)**, then
restart Home Assistant.

**Manual:** copy `custom_components/rinnai_ble/` into
`<config>/custom_components/` and restart Home Assistant (a reload is not
enough for Python changes).

Upgrading from a pre-1.0 copy: replace the folder and restart. Entity IDs and
history are kept.

## Bonding (the step everyone gets stuck on)

The heater's data characteristics need an encrypted, bonded link, and it only
accepts a bond while in pairing mode. On the Home Assistant host, with the
integration disabled and the Rinnai phone app closed:

```bash
bluetoothctl
remove AA:BB:CC:DD:EE:FF     # your heater's address; clears a stale record
power on
agent on
default-agent
scan on                      # wait for "Rinnai RX  <serial>"
# --- press the Bluetooth button on the front of the heater now (LED blinks) ---
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
info AA:BB:CC:DD:EE:FF       # want Paired: yes and Bonded: yes
quit
```

- Without the button press, pairing fails with `AuthenticationFailed` on any
  adapter. There is no passkey.
- **Bonds are per adapter.** Change dongles and you must bond again.
- If the heater was re-paired with the phone or lost power, it may have
  forgotten the bond. Symptoms: connections drop immediately with
  `NotConnected`, or the integration reports it "refused an encrypted
  connection". Bond again.

Then add the integration (it is usually auto-discovered).

## Options

Settings → Devices & Services → Rinnai → **Configure**:

- **Poll interval:** default 30 s. Go lower (10 s) only briefly to catch a
  short event.
- **Bluetooth adapter:** *Automatic* uses the best-placed local adapter. If you
  have several, pin the one holding the bond. A pinned adapter is strict: the
  integration waits for it rather than roaming to another radio.

## How it stays connected

- **One persistent connection**, opened with the `\I` session handshake the
  Rinnai app sends.
- **A missed poll is not an outage.** Values are kept and entities stay
  available until no valid data has arrived for 5 minutes.
- **Stalls are fixed in place first.** If `\L` stops answering, the session
  handshake is re-sent on the same link; the link is only rebuilt after three
  failed polls in a row.
- **Reconnects back off** (30 s doubling to 5 min) so a busy adapter isn't
  hammered.
- **Corrupt data is never shown.** Fragments are reassembled by sequence number
  (duplicates are harmless) and every frame's checksum is verified.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `refused an encrypted connection`, `Insufficient encryption` | Not bonded with this adapter. See *Bonding* |
| Setup retries with `NotConnected` | The heater drops the link right after connecting: usually a lost bond, sometimes the phone app holding the connection. Close the app, then bond again |
| `heater not currently visible` | No adapter can see it. Check range, close `bluetoothctl` and the phone app (the heater stops advertising while anything is connected) |
| `\L has not answered for 3 polls; reconnecting` now and then | Normal recovery. If constant, suspect RF: use a USB extension cable away from USB 3 ports |
| Other Bluetooth devices got slower | Shared adapter. See *Requirements* |

For more detail, add to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.rinnai_ble: debug
```

**Only one central at a time:** the heater accepts a single connection, so the
Rinnai Central app and Home Assistant compete. Keep the app closed.

## Development

`custom_components/rinnai_ble/rinnai_protocol.py` has no Home Assistant
dependencies and can be reused in any Python project, or as the reference for an
ESP32/ESPHome implementation.

```bash
pip install -r requirements_test.txt
pytest
```

The codec tests alone need only the standard library:
`python -m unittest tests.test_protocol`.

To decode more fields without a packet capture, enable the **Raw Diagnostic
Frame L** / **Raw Status Frame K** sensors, put the unit in the state of
interest, and copy the hex from the entity history **as text**. PROTOCOL.md
lists what is still unmapped.

## Disclaimer

Not affiliated with or endorsed by Rinnai. Read-only by design: it only sends the
same status queries the Rinnai app sends, never control commands. Use at your own risk.

## License

MIT
