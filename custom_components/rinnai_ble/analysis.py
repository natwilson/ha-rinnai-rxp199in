"""Derived values for spotting low-flow "silent lockouts". No Home Assistant imports.

A condensing tankless heater cannot turn its burner down below a minimum firing
rate (15,000 BTU/h on the RXP199iN). When a draw needs less heat than that --
low flow with warm inlet water, typically in summer -- the heat exchanger
overheats, the burner cuts out, and water keeps flowing unheated with no fault
code until the tap is closed. These helpers estimate how close a draw is to
that floor and detect the cut-out itself.
"""
from __future__ import annotations

BTU_PER_GPM_F = 500          # 8.33 lb/gal x 60 min/h x 1 BTU/(lb.°F)
DEFAULT_MIN_FIRING_RATE = 15_000  # BTU/h, RXP199iN data sheet
LOCKOUT_MIN_FLOW = 0.4       # gal/min; well above the activation flow
LOCKOUT_AFTER = 60.0         # seconds of flow without combustion


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
