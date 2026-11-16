"""Speed control requirements, verified through the CAN interface."""

import numpy as np
import pytest

from rig.assertions import always, overshoot, settles
from rig.harness import Scenario, SensorFaultEvent


@pytest.mark.req("REQ-001")
def test_speed_step_response(run, spec, measured):
    a = spec["REQ-001"]["acceptance"]
    target = a["target_rad_s"]
    tr = run(Scenario("step_0_to_300", 5.0, speed_req=[(0.0, 0.0), (1.0, target)]))

    t_settle = settles(tr, "speed", target, a["band_frac"], a["settle_time_s"], after=1.0)
    measured("settling time", t_settle, "s", f"≤ {a['settle_time_s']}")
    over = overshoot(tr, "speed", target, after=1.0, max_frac=a["max_overshoot_frac"])
    measured("overshoot", over * 100, "%", f"≤ {a['max_overshoot_frac'] * 100:.0f}")


@pytest.mark.req("REQ-002")
def test_load_disturbance_rejection(run, spec, measured):
    a = spec["REQ-002"]["acceptance"]
    target, t_step = a["target_rad_s"], 3.0
    tr = run(Scenario("load_step_60Nm", 6.0, speed_req=[(0.0, target)],
                      load=[(0.0, 0.0), (t_step, a["load_step_nm"])]))

    dip = 1.0 - tr.speed[tr.window(t_step)].min() / target
    measured("speed dip", dip * 100, "%", f"≤ {a['max_dip_frac'] * 100:.0f}")
    assert dip <= a["max_dip_frac"], f"speed dipped {dip:.1%}, allowed {a['max_dip_frac']:.0%}"
    t_rec = settles(tr, "speed", target, a["band_frac"], a["recover_time_s"], after=t_step)
    measured("recovery time", t_rec, "s", f"≤ {a['recover_time_s']}")


# REQ-003 must hold in every mode, so it is checked across one scenario per mode
TORQUE_LIMIT_SCENARIOS = [
    Scenario("normal_full_demand", 4.0, speed_req=[(0.0, 0.0), (0.5, 1000.0)]),
    Scenario("derated_hot_start", 20.0, speed_req=[(0.0, 650.0)], initial_temps=(125.0, 110.0)),
    Scenario("limp_home", 4.0, speed_req=[(0.0, 600.0)],
             sensor_faults=[SensorFaultEvent(1.0, "temp", "dropout")]),
    Scenario("safe_from_full_torque", 3.0, speed_req=[(0.0, 0.0), (0.2, 1000.0)],
             sensor_faults=[SensorFaultEvent(0.6, "speed", "dropout")]),
]


@pytest.mark.req("REQ-003")
@pytest.mark.parametrize("scenario", TORQUE_LIMIT_SCENARIOS, ids=lambda s: s.name)
def test_torque_command_within_limit(run, spec, measured, scenario):
    tol = spec["REQ-003"]["acceptance"]["tolerance_nm"]
    tr = run(scenario)
    margin = float(np.max(np.abs(tr.torque_cmd) - tr.torque_limit))
    measured("max (|command| - limit)", margin, "N·m", f"≤ {tol}")
    always(tr, np.abs(tr.torque_cmd) <= tr.torque_limit + tol, "|torque_cmd| <= torque_limit")
