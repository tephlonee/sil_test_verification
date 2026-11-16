"""
assertions.py - A small assertion language over recorded traces.

Each function maps onto a typical requirement phrase, checks it, and either
returns the measured value (for the evidence log) or raises AssertionError
with a message that says what was expected and what actually happened.

    settles(tr, "speed", target=300, band=0.02, within=2.0, after=0.0)   "within ±2 % within 2 s"
    overshoot(tr, "speed", target=300, after=0.0)                         "overshoot of at most ..."
    always(tr, tr.torque_cmd <= tr.torque_limit, what="...")              "shall never exceed"
    eventually(tr, tr.mode == SAFE, within=0.1, after=3.0, what="...")    "within 100 ms"
    holds_from(tr, mask, t0, what="...")                                  "and remain ... (latched)"
"""

from __future__ import annotations

import numpy as np

from rig.trace import Trace


def _fmt(t: float | None) -> str:
    return "never" if t is None else f"{t:.3f} s"


def settles(tr: Trace, signal: str, target: float, band: float, within: float,
            after: float = 0.0) -> float:
    """Signal enters and stays within target*(1±band) no later than `after + within`.
    Returns the settling time measured from `after`."""
    x = tr.data[signal]
    win = tr.window(after)
    outside = np.abs(x - target) > abs(target) * band
    idx_out = np.flatnonzero(outside & win)
    t_settle = 0.0 if idx_out.size == 0 else float(tr.t[min(idx_out[-1] + 1, len(tr) - 1)]) - after
    if idx_out.size and idx_out[-1] == len(tr) - 1:
        raise AssertionError(f"{signal} never settled within ±{band:.0%} of {target} "
                             f"(final value {x[-1]:.2f})")
    if t_settle > within + 1e-9:
        raise AssertionError(f"{signal} settled within ±{band:.0%} of {target} after "
                             f"{t_settle:.3f} s, required ≤ {within} s")
    return t_settle


def overshoot(tr: Trace, signal: str, target: float, after: float = 0.0,
              max_frac: float | None = None) -> float:
    """Peak excursion beyond target, as a fraction of target."""
    x = tr.data[signal][tr.window(after)]
    over = (x.max() - target) / abs(target) if target > 0 else (target - x.min()) / abs(target)
    over = max(float(over), 0.0)
    if max_frac is not None and over > max_frac + 1e-9:
        raise AssertionError(f"{signal} overshoot {over:.1%}, allowed ≤ {max_frac:.0%}")
    return over


def always(tr: Trace, mask: np.ndarray, what: str, t0: float = 0.0,
           t1: float = float("inf")) -> None:
    """Condition holds at every tick in [t0, t1)."""
    bad = np.flatnonzero(~mask & tr.window(t0, t1))
    if bad.size:
        i = bad[0]
        raise AssertionError(f"'{what}' violated at t={tr.t[i]:.3f} s "
                             f"({bad.size} tick(s) in total)")


def never(tr: Trace, mask: np.ndarray, what: str, t0: float = 0.0,
          t1: float = float("inf")) -> None:
    always(tr, ~mask, f"never: {what}", t0, t1)


def eventually(tr: Trace, mask: np.ndarray, within: float, what: str,
               after: float = 0.0) -> float:
    """Condition becomes true no later than `after + within`. Returns the delay from `after`."""
    t_hit = tr.first_time(mask, after)
    if t_hit is None or t_hit - after > within + 1e-9:
        raise AssertionError(f"'{what}' expected within {within * 1000:.0f} ms of "
                             f"t={after:.3f} s, first true at {_fmt(t_hit)}")
    return t_hit - after


def holds_from(tr: Trace, mask: np.ndarray, t0: float, what: str) -> None:
    """Condition holds from t0 until the end of the run (e.g. a latched state)."""
    always(tr, mask, what, t0=t0)
