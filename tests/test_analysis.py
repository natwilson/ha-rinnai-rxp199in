"""Tests for the low-flow lockout helpers. Run with: python -m unittest tests.test_analysis"""
import importlib.util
import pathlib
import sys
import unittest

_PATH = pathlib.Path(__file__).parents[1] / "custom_components/rinnai_ble/analysis.py"
_spec = importlib.util.spec_from_file_location("analysis", _PATH)
A = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = A  # dataclasses look the module up
_spec.loader.exec_module(A)


class HeatDemandTests(unittest.TestCase):
    def test_summer_low_flow_is_below_floor(self):
        # 0.65 gal/min at a 125 °F setpoint with 81 °F inlet water.
        self.assertEqual(A.heat_demand(0.65, 125, 81), 14300)
        self.assertLess(A.firing_margin(14300, 15000), 0)

    def test_winter_same_draw_has_margin(self):
        demand = A.heat_demand(0.65, 125, 68)
        self.assertEqual(demand, 18525)
        self.assertAlmostEqual(A.firing_margin(demand, 15000), 23.5)

    def test_missing_inputs_and_no_flow(self):
        self.assertIsNone(A.heat_demand(None, 125, 70))
        self.assertEqual(A.heat_demand(0.0, 125, 70), 0)
        self.assertIsNone(A.firing_margin(0, 15000))

    def test_inlet_above_setpoint_never_negative(self):
        self.assertEqual(A.heat_demand(1.0, 100, 110), 0)


class LockoutDetectorTests(unittest.TestCase):
    def test_flags_only_after_sustained_unheated_flow(self):
        d = A.LockoutDetector(after=60)
        self.assertFalse(d.update(0.7, False, False, now=0))
        self.assertFalse(d.update(0.7, False, False, now=31))
        self.assertTrue(d.update(0.7, False, False, now=62))

    def test_ignition_within_window_is_not_a_lockout(self):
        d = A.LockoutDetector(after=60)
        d.update(0.7, False, False, now=0)
        self.assertFalse(d.update(0.7, True, False, now=31))
        self.assertFalse(d.update(0.7, False, False, now=62))  # timer restarted

    def test_clears_when_flow_stops(self):
        d = A.LockoutDetector(after=60)
        for t in (0, 31, 62):
            d.update(0.7, False, False, now=t)
        self.assertFalse(d.update(0.0, False, False, now=93))

    def test_ignores_purge_trickle_and_faults(self):
        d = A.LockoutDetector(after=60)
        self.assertFalse(d.update(0.0, False, False, now=0))     # post-purge
        self.assertFalse(d.update(0.2, False, False, now=100))   # below threshold
        d2 = A.LockoutDetector(after=60)
        for t in (0, 31, 62):
            last = d2.update(0.7, False, True, now=t)            # real fault code
        self.assertFalse(last)


if __name__ == "__main__":
    unittest.main()


class FalseStartDetectorTests(unittest.TestCase):
    # update(fan, combustion, pump, hex, inlet, flow, now)

    def test_fan_run_without_flame_or_heat_is_a_false_start(self):
        d = A.FalseStartDetector()
        self.assertIsNone(d.update(0, False, False, 75, 69, 0.0, now=0))
        self.assertIsNone(d.update(359, False, False, 75, 69, 0.2, now=5))
        self.assertIsNone(d.update(359, False, False, 76, 69, 0.0, now=10))
        self.assertEqual(
            d.update(0, False, False, 75, 69, 0.0, now=35),
            {"duration": 30, "max_fan_frequency": 359, "max_flow": 0.2},
        )
        self.assertIsNone(d.update(0, False, False, 75, 69, 0.0, now=40))  # reported once

    def test_burn_is_not_a_false_start(self):
        d = A.FalseStartDetector()
        d.update(359, False, False, 75, 69, 1.5, now=0)
        d.update(359, True, False, 90, 69, 1.5, now=5)
        d.update(300, False, False, 100, 69, 0.0, now=60)
        self.assertIsNone(d.update(0, False, False, 95, 69, 0.0, now=240))

    def test_heat_rise_counts_as_lit_even_if_flag_missed(self):
        d = A.FalseStartDetector()
        d.update(359, False, False, 75, 69, 2.8, now=0)
        d.update(300, False, False, 101, 69, 0.0, now=30)
        self.assertIsNone(d.update(0, False, False, 90, 69, 0.0, now=200))

    def test_purge_first_seen_while_hot_is_not_counted(self):
        d = A.FalseStartDetector()
        d.update(300, False, False, 93, 69, 0.0, now=0)
        self.assertIsNone(d.update(0, False, False, 88, 69, 0.0, now=150))

    def test_recirculation_is_not_a_false_start(self):
        d = A.FalseStartDetector()
        d.update(359, False, True, 75, 69, 1.0, now=0)
        self.assertIsNone(d.update(0, False, False, 76, 69, 0.0, now=60))

    def test_missing_fan_reading_is_ignored(self):
        d = A.FalseStartDetector()
        d.update(359, False, False, 75, 69, 0.0, now=0)
        self.assertIsNone(d.update(None, None, None, None, None, None, now=5))
        self.assertIsNotNone(d.update(0, False, False, 75, 69, 0.0, now=30))
