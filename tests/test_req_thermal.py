"""Thermal protection requirements. Hot starts replace minutes of simulated warm-up."""

import numpy as np
import pytest

from rig.assertions import always, eventually, holds_from
from rig.harness import Scenario
from rig.trace import MODES

FAULT_OVERTEMP = 0x0100


def derate_curve(cal: dict, temp: np.ndarray) -> np.ndarray:
    span = cal["derate_end"] - cal["derate_start"]
    frac = 1.0 - (temp - cal["derate_start"]) / span * (1.0 - cal["derate_min_frac"])
    return cal["torque_max"] * np.clip(frac, cal["derate_min_frac"], 1.0)


@pytest.mark.req("REQ-004")
def test_derating_onset(run, spec, cal, measured):
    a = spec["REQ-004"]["acceptance"]
    tr = run(Scenario("heat_through_derate_start", 30.0, speed_req=[(0.0, 650.0)],
                      initial_temps=(cal["derate_start"] - 3.0, 100.0)))

    t_cross = tr.first_time(tr.temp_ecu >= cal["derate_start"])
    assert t_cross is not None, "scenario never reached derate_start - test is invalid"
    delay = eventually(tr, tr.mode == MODES["DERATED"], a["max_delay_s"],
                       "mode == DERATED", after=t_cross)
    measured("delay to DERATED", delay * 1000, "ms", f"≤ {a['max_delay_s'] * 1000:.0f}")


@pytest.mark.req("REQ-005")
def test_derating_curve(run, spec, cal, measured):
    a = spec["REQ-005"]["acceptance"]
    tr = run(Scenario("sustained_heavy_load_hot", 60.0, speed_req=[(0.0, 650.0)],
                      initial_temps=(cal["derate_start"] - 3.0, 100.0)))

    t_der = tr.first_time(tr.mode == MODES["DERATED"])
    assert t_der is not None, "scenario never entered DERATED - test is invalid"
    tol = a["tolerance_frac_of_max"] * cal["torque_max"]
    excess = tr.torque_limit - derate_curve(cal, tr.temp_ecu)
    in_derate = (tr.mode == MODES["DERATED"]) & (tr.t >= t_der + a["settle_s"])
    measured("max limit above curve", float(excess[in_derate].max()), "N·m", f"≤ {tol:.2f}")
    always(tr, ~in_derate | (excess <= tol), "torque_limit <= derating curve + tolerance")


@pytest.mark.req("REQ-006")
def test_derating_hysteresis(run, spec, cal, measured):
    a = spec["REQ-006"]["acceptance"]
    exit_temp = cal["derate_start"] - cal["derate_hyst"]
    tr = run(Scenario("cool_down_from_derated", 240.0, speed_req=[(0.0, 150.0)],
                      initial_temps=(cal["derate_start"] + 6.0, 110.0)))

    derated = tr.mode == MODES["DERATED"]
    exits = np.flatnonzero(derated[:-1] & ~derated[1:]) + 1
    assert exits.size >= 1, "never left DERATED - scenario did not cool enough"
    measured("mode exits", exits.size, "", f"≤ {a['max_exits']}")
    measured("temperature at exit", float(tr.temp_ecu[exits[0]]), "°C", f"< {exit_temp}")
    assert exits.size <= a["max_exits"], f"left DERATED {exits.size} times (oscillation)"
    for i in exits:
        assert tr.temp_ecu[i] < exit_temp, (
            f"left DERATED at {tr.temp_ecu[i]:.2f} °C (t={tr.t[i]:.2f} s), must be < {exit_temp} °C")


@pytest.mark.req("REQ-007")
def test_winding_temperature_protection(run, spec, cal, measured):
    a = spec["REQ-007"]["acceptance"]
    tr = run(Scenario("sustained_max_demand_hot", a["duration_s"],
                      speed_req=[(0.0, a["speed_req_rad_s"])],
                      initial_temps=(a["initial_winding_c"], a["initial_housing_c"])))

    measured("peak winding temperature", float(tr.temp.max()), "°C", f"< {cal['derate_end']}")
    always(tr, tr.temp < cal["derate_end"], "true winding temperature < derate_end")


@pytest.mark.req("REQ-010")
def test_overtemperature_shutdown(run, spec, cal, measured):
    a = spec["REQ-010"]["acceptance"]
    tr = run(Scenario("start_above_critical", 2.0, speed_req=[(0.0, 300.0)],
                      initial_temps=(cal["temp_critical"] + 5.0, 150.0)))

    t_safe = eventually(tr, tr.mode == MODES["SAFE"], a["max_delay_s"], "mode == SAFE")
    measured("delay to SAFE", t_safe * 1000, "ms", f"≤ {a['max_delay_s'] * 1000:.0f}")
    t_zero = eventually(tr, np.abs(tr.torque_cmd) < 1e-3, a["torque_zero_s"], "torque_cmd == 0",
                        after=0.0)
    measured("time to zero torque", t_zero, "s", f"≤ {a['torque_zero_s']}")
    holds_from(tr, (tr.faults.astype(int) & FAULT_OVERTEMP) != 0, t_safe, "OVERTEMP fault latched")
