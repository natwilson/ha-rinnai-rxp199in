"""Tests for the standalone codec. Run with: python -m unittest discover tests"""
import importlib.util
import pathlib
import unittest

# Load the codec directly so the tests need no Home Assistant install.
_PATH = pathlib.Path(__file__).parents[1] / "custom_components/rinnai_ble/rinnai_protocol.py"
_spec = importlib.util.spec_from_file_location("rinnai_protocol", _PATH)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

# Worked example from PROTOCOL.md section 5.
L_EXAMPLE = (
    "0000000000000060" "0063004C0055" "0050" "08" "0000" "0023" "0001" "0004"
    "0049" "0000" "0003" "0000" "5B29" "0000" "75B2" "088C" "0064" "01A7" "01A7" "0000"
)
K_EXAMPLE = "020101017802000000"


def reply(reg, data):
    return R.fragment(R.build_frame(reg, data))


class BuildTests(unittest.TestCase):
    def test_bare_query_matches_app(self):
        self.assertEqual(R.build_write("L"), [bytes.fromhex("010102 30335C4C 03 3045 0D")])

    def test_k_query_matches_app(self):
        frags = R.build_write("K", "0" * 18)
        self.assertEqual(len(frags), 2)
        self.assertEqual(frags[0][:2], b"\x01\x02")
        self.assertEqual(frags[1], bytes.fromhex("02 3030303030 03 3644 0D"))

    def test_i_handshake_checksum_matches_app(self):
        frags = R.build_write("I", "0" * 26)
        self.assertTrue(frags[-1].endswith(bytes.fromhex("03 4542 0D")))


class ReassemblerTests(unittest.TestCase):
    def feed_all(self, frags):
        r, out = R.Reassembler(), []
        for f in frags:
            out += r.feed(f)
        return out

    def test_roundtrip_long_frame(self):
        frags = reply("L", L_EXAMPLE)
        self.assertGreater(len(frags), 5)
        self.assertEqual(self.feed_all(frags), [("L", L_EXAMPLE)])

    def test_duplicated_middle_fragment_is_harmless(self):
        frags = reply("L", L_EXAMPLE)
        frags.insert(3, frags[2])
        self.assertEqual(self.feed_all(frags), [("L", L_EXAMPLE)])

    def test_out_of_order_fragments(self):
        frags = reply("L", L_EXAMPLE)
        frags[2], frags[3] = frags[3], frags[2]
        self.assertEqual(self.feed_all(frags), [("L", L_EXAMPLE)])

    def test_missing_fragment_emits_nothing(self):
        frags = reply("L", L_EXAMPLE)
        del frags[2]
        r = R.Reassembler()
        for f in frags:
            self.assertEqual(r.feed(f), [])
        self.assertIn("fragments", r.buffered)

    def test_new_message_discards_partial(self):
        partial = reply("L", L_EXAMPLE)[:3]
        self.assertEqual(self.feed_all(partial + reply("K", K_EXAMPLE)), [("K", K_EXAMPLE)])

    def test_corrupt_checksum_dropped(self):
        frame = bytearray(R.build_frame("K", K_EXAMPLE))
        frame[-2] ^= 0x01
        self.assertEqual(self.feed_all(R.fragment(bytes(frame))), [])

    def test_stray_fragment_ignored(self):
        self.assertEqual(R.Reassembler().feed(b"\x03abc"), [])


class ParseTests(unittest.TestCase):
    def test_parse_L_worked_example(self):
        d = R.parse_L(L_EXAMPLE)
        self.assertEqual(
            {k: d[k] for k in (
                "fan_frequency", "water_flow_rate", "outgoing_temp", "hex_outlet_temp",
                "inlet_temp", "exhaust_temp", "freeze_protection_temp", "bypass_position",
                "combustion_hours", "combustion_cycles", "pump_hours", "pump_cycles",
                "total_heat_energy", "lime_detecting_temp")},
            {
                "fan_frequency": 0, "water_flow_rate": 0.0, "outgoing_temp": 96,
                "hex_outlet_temp": 99, "inlet_temp": 76, "exhaust_temp": 85,
                "freeze_protection_temp": 80, "bypass_position": 35,
                "combustion_hours": 400, "combustion_cycles": 7300, "pump_hours": 0,
                "pump_cycles": 300, "total_heat_energy": 3_013_000, "lime_detecting_temp": 100,
            },
        )
        self.assertTrue(d["internal_pump_installed"])

    def test_parse_L_short_frame_rejected(self):
        self.assertEqual(R.parse_L(L_EXAMPLE[:60]), {})

    def test_parse_K(self):
        self.assertEqual(
            {k: v for k, v in R.parse_K(K_EXAMPLE).items() if k != "raw_k"},
            {"operating_mode": 1, "pump_running": False, "setpoint_temp": 120, "combustion": False},
        )
        firing = K_EXAMPLE[:12] + "01" + K_EXAMPLE[14:]
        self.assertTrue(R.parse_K(firing)["combustion"])

    def test_parse_P(self):
        self.assertFalse(R.parse_P("0" * 98)["diagnostic_code_active"])
        self.assertEqual(R.parse_P("0" * 96 + "11")["diagnostic_code"], "0" * 96 + "11")

    def test_parse_N_serial(self):
        self.assertEqual(R.parse_N("0" * 10 + "1234567890" + "0" * 68), {"serial": "1234567890"})
        self.assertEqual(R.parse_N("0" * 88), {"serial": "0000000000"})


if __name__ == "__main__":
    unittest.main()
