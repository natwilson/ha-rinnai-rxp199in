r"""Rinnai RX/RXP BLE maintenance-protocol codec.

Reverse-engineered from a Rinnai Central BLE capture (RXP199iN). See
PROTOCOL.md for the full reference. This module has no Home Assistant
dependencies and can be reused anywhere.

App-layer frame:  <STX>"03"\<REG><ASCII-hex data><ETX><2-char hex checksum><CR>
GATT fragmentation: first fragment = [0x01][total_count][body...],
                    subsequent      = [seq][body...]   (seq is 1-based; chunk=18B)
"""
from __future__ import annotations

import logging
import re
from math import ceil

STX = 0x02
ETX = 0x03
CR = 0x0D
ADDR = "03"
CHUNK = 18  # fragment body size used by the app, both directions

SVC_UUID = "c0850001-eca2-441e-9f57-cfab262fe9c0"
WRITE_UUID = "c0850003-eca2-441e-9f57-cfab262fe9c0"   # write-without-response
NOTIFY_UUID = "c0850002-eca2-441e-9f57-cfab262fe9c0"  # notify

_LOGGER = logging.getLogger(__name__)
_FRAME_RE = re.compile(rb"\x02(..)\\(.)([0-9A-Fa-f]*)\x03([0-9A-Fa-f]{2})\x0d", re.S)


def checksum(body: bytes) -> int:
    """Low byte of the sum of every byte from the address through ETX inclusive."""
    return sum(body) & 0xFF


def build_frame(reg: str, data: str = "") -> bytes:
    body = f"{ADDR}\\{reg}{data}".encode("ascii") + bytes([ETX])
    return bytes([STX]) + body + f"{checksum(body):02X}".encode("ascii") + bytes([CR])


def build_write(reg: str, data: str = "") -> list[bytes]:
    """Return the ordered characteristic writes for a query/command."""
    frame = build_frame(reg, data)
    n = max(1, ceil(len(frame) / CHUNK))
    out: list[bytes] = []
    for i in range(n):
        header = bytes([0x01, n]) if i == 0 else bytes([i + 1])
        out.append(header + frame[i * CHUNK:(i + 1) * CHUNK])
    return out


def fragment(frame: bytes) -> list[bytes]:
    """Split a raw frame the way the heater does (used by tests and tools)."""
    n = max(1, ceil(len(frame) / CHUNK))
    return [
        (bytes([0x01, n]) if i == 0 else bytes([i + 1])) + frame[i * CHUNK:(i + 1) * CHUNK]
        for i in range(n)
    ]


def parse_frame(raw: bytes) -> tuple[str, str] | None:
    """Return (register, data) for a complete frame with a valid checksum."""
    start = raw.find(bytes([STX]))
    if start < 0:
        return None
    m = _FRAME_RE.match(raw, start)
    if not m:
        return None
    body = raw[start + 1:m.start(4)]  # address .. ETX inclusive
    if checksum(body) != int(m.group(4), 16):
        _LOGGER.debug("Dropping \\%s frame with bad checksum", m.group(2))
        return None
    return m.group(2).decode("ascii"), m.group(3).decode("ascii")


class Reassembler:
    """Rebuild app-layer frames from notification fragments.

    Fragments are slotted by their sequence number rather than appended in
    arrival order, so a duplicated fragment simply overwrites its own slot
    instead of corrupting the frame. A frame is emitted once every slot from
    1 to the advertised count is filled and the checksum verifies.
    """

    def __init__(self) -> None:
        self._count = 0
        self._parts: dict[int, bytes] = {}

    def reset(self) -> None:
        self._count = 0
        self._parts.clear()

    @property
    def buffered(self) -> str:
        """Human-readable reassembly state, for failure diagnostics."""
        if not self._count:
            return "nothing received"
        have = sorted(self._parts)
        return f"{len(have)}/{self._count} fragments (have {have})"

    def feed(self, chunk: bytes) -> list[tuple[str, str]]:
        if len(chunk) < 2:
            return []
        seq = chunk[0]
        if seq == 0x01:
            # First fragment always starts a new message.
            self._count = max(1, chunk[1])
            self._parts = {1: bytes(chunk[2:])}
        elif self._count and 1 < seq <= self._count:
            self._parts[seq] = bytes(chunk[1:])
        else:
            return []  # stray fragment with no message in progress
        if len(self._parts) < self._count:
            return []
        raw = b"".join(self._parts[i] for i in range(1, self._count + 1))
        self.reset()
        frame = parse_frame(raw)
        return [frame] if frame else []


# ---- Register parsers -------------------------------------------------------

def _u16(raw: bytes, i: int) -> int:
    return int.from_bytes(raw[i:i + 2], "big")


L_LENGTH = 49  # bytes


def parse_L(data: str) -> dict:
    r"""\L live diagnostics. Words 0-7 are even-aligned; byte 17 onward is odd-aligned."""
    try:
        raw = bytes.fromhex(data)
    except ValueError:
        return {}
    if len(raw) < L_LENGTH:
        return {}
    return {
        "fan_frequency": _u16(raw, 0),               # Hz
        "fan_current": _u16(raw, 2) * 10,            # mA
        "water_flow_rate": round(_u16(raw, 4) * 0.1, 1),  # gal/min
        "outgoing_temp": _u16(raw, 6),               # °F
        "hex_outlet_temp": _u16(raw, 8),
        "inlet_temp": _u16(raw, 10),
        "exhaust_temp": _u16(raw, 12),
        "freeze_protection_temp": _u16(raw, 14),
        "bypass_position": _u16(raw, 19),            # degrees
        "internal_pump_installed": bool(_u16(raw, 21)),  # capability flag
        # Counters have 100-unit hardware resolution.
        "combustion_hours": _u16(raw, 23) * 100,
        "combustion_cycles": _u16(raw, 25) * 100,
        "pump_hours": _u16(raw, 27) * 100,
        "pump_cycles": _u16(raw, 29) * 100,
        "water_through_heater": _u16(raw, 33),       # gal, includes recirculation
        "total_heat_energy": _u16(raw, 37) * 100,    # kcal
        "lime_detecting_temp": _u16(raw, 41),        # °F
        # Not decoded yet; exposed so changes can be spotted (see PROTOCOL.md §5).
        "unknown_l16": raw[16],
        "unknown_l39": _u16(raw, 39),
        "raw_l": data,
    }


def parse_K(data: str) -> dict:
    r"""\K status. Byte 6 is the authoritative combustion flag."""
    try:
        b = bytes.fromhex(data)
    except ValueError:
        return {}
    if len(b) < 7:
        return {}
    return {
        "operating_mode": b[1],          # 1 idle, 2 recirculation running
        "pump_running": b[1] == 2,
        "setpoint_temp": b[4],           # °F
        "combustion": bool(b[6]),        # 0 during post-combustion purge too
        "raw_k": data,
    }


def parse_P(data: str) -> dict:
    r"""\P diagnostic code. All zeros means no active code."""
    active = bool(data.strip("0"))
    return {
        "diagnostic_code_active": active,
        "diagnostic_code": data if active else "none",
    }


def parse_N(data: str) -> dict:
    r"""\N unit info. The serial is 10 ASCII digits at offset 10."""
    serial = data[10:20]
    return {"serial": serial} if len(serial) == 10 and serial.isdigit() else {}
