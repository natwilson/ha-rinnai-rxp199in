"""Derived values for low-flow "silent lockouts" and false starts. No Home Assistant imports.

A condensing tankless heater cannot turn its burner down below a minimum firing
rate (15,000 BTU/h on the RXP199iN). When a draw needs less heat than that --
low flow with warm inlet water, typically in summer -- the heat exchanger
overheats, the burner cuts out, and water keeps flowing unheated with no fault
code until the tap is closed. These helpers estimate how close a draw is to
that floor and detect the cut-out itself.
"""
from __future__ import annotations

from dataclasses import dataclass

BTU_PER_GPM_F = 500          # 8.33 lb/gal x 60 min/h x 1 BTU/(lb.°F)
DEFAULT_MIN_FIRING_RATE = 15_000  # BTU/h, RXP199iN data sheet
LOCKOUT_MIN_FLOW = 0.4       # gal/min; well above the activation flow
LOCKOUT_AFTER = 60.0         # seconds of flow without combustion
FALSE_START_MAX_RISE = 4     # °F; a heat exchanger rise this big means it lit
RESIDUAL_HEAT = 15           # °F above inlet: still hot from an earlier burn


def heat_demand(flow: float | None, setpoint: float | None, inlet: float | None) -> int | None:
    """Heat needed to raise the current flow from inlet to setpoint, in BTU/h."""
    if flow is None or setpoint is None or inlet is None:
        return None
    return max(0, round(BTU_PER_GPM_F * flow * (setpoint - inlet)))


def firing_margin(demand: int | None, min_rate: float) -> float | None:
    """Percent the demand sits above the minimum firing rate (negative = below).

    Only meaningful during a draw, so None when nothing is flowing.
    """
    if not demand or min_rate <= 0:
        return None
    return round((demand / min_rate - 1) * 100, 1)


class LockoutDetector:
    """Flags water flowing with the burner off for longer than ignition takes.

    Normal draws light within seconds, so requiring LOCKOUT_AFTER seconds (two
    or more polls at the default interval) avoids flagging ignition. Post-purge
    has zero flow and recirculation keeps the burner lit, so neither trips it.
    """

    def __init__(self, after: float = LOCKOUT_AFTER) -> None:
        self.after = after
        self.since: float | None = None

    def update(self, flow: float | None, combustion: bool | None,
               fault_active: bool | None, now: float) -> bool:
        unheated = (
            flow is not None and flow >= LOCKOUT_MIN_FLOW
            and combustion is False and not fault_active
        )
        if not unheated:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return now - self.since >= self.after


@dataclass
class _FanRun:
    start: float
    hex_start: float | None
    inlet: float | None
    hex_max: float | None
    lit: bool
    max_fan: int
    max_flow: float


class FalseStartDetector:
    """Flags fan runs that end without the burner ever lighting.

    A brief flow blip wakes the heater: the fan spins up for ignition, the
    flow is gone before the burner lights, and the fan stops again without the
    long post-combustion purge. Observed on an RXP199iN as runs of 30-90 s
    with no temperature change.

    A run counts as lit if combustion or the recirculation pump was seen, or
    the heat exchanger warmed. A run first seen while the exchanger is still
    hot is the purge after a burn the polls missed, so it is not counted.
    Needs polls every few seconds while the fan runs to be reliable.
    """

    def __init__(self) -> None:
        self._run: _FanRun | None = None

    def update(self, fan: int | None, combustion: bool | None, pump: bool | None,
               hex_temp: float | None, inlet: float | None, flow: float | None,
               now: float) -> dict | None:
        """Feed one poll. Returns details when a false start has just ended."""
        if fan is None:
            return None
        lit = bool(combustion) or bool(pump)
        run = self._run
        if fan > 0:
            if run is None:
                self._run = _FanRun(now, hex_temp, inlet, hex_temp, lit, fan, flow or 0.0)
                return None
            run.lit |= lit
            run.max_fan = max(run.max_fan, fan)
            run.max_flow = max(run.max_flow, flow or 0.0)
            if hex_temp is not None:
                run.hex_max = hex_temp if run.hex_max is None else max(run.hex_max, hex_temp)
            return None
        if run is None:
            return None
        self._run = None
        if run.lit or run.hex_start is None or run.hex_max is None:
            return None
        if run.hex_max - run.hex_start >= FALSE_START_MAX_RISE:
            return None
        if run.inlet is not None and run.hex_start - run.inlet >= RESIDUAL_HEAT:
            return None
        return {
            "duration": round(now - run.start),
            "max_fan_frequency": run.max_fan,
            "max_flow": run.max_flow,
        }
