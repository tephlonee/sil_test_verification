"""Sensor fault reactions. Faults are injected in the plant's sensors, not in the ECU."""

import numpy as np
import pytest

from rig.assertions import eventually, holds_from
from rig.harness import Scenario, SensorFaultEvent
from rig.trace import MODES


@pytest.mark.req("REQ-008")
def test_temperature_sensor_fault(run, spec, cal, measured):
    a = spec["REQ-008"]["acceptance"]
    t_fault, t_recover = 2.0, 4.0
    tr = run(Scenario("temp_sensor_dropout_then_recover", 6.0, speed_req=[(0.0, 600.0)],
                      sensor_faults=[SensorFaultEvent(t_fault, "temp", "dropout"),
                                     SensorFaultEvent(t_recover, "temp", "none")]))

    delay = eventually(tr, tr.mode == MODES["LIMP_HOME"], a["max_delay_s"],
                       "mode == LIMP_HOME", after=t_fault)
    measured("delay to LIMP_HOME", delay * 1000, "ms", f"≤ {a['max_delay_s'] * 1000:.0f}")

    limp_limit = cal["limp_frac"] * cal["torque_max"]
    t_lim = eventually(tr, tr.torque_limit <= limp_limit + 1e-3, a["limit_time_s"],
                       f"torque_limit <= {limp_limit:.0f} N·m", after=t_fault)
    measured("time to limp torque limit", t_lim, "s", f"≤ {a['limit_time_s']}")
    holds_from(tr, tr.mode == MODES["LIMP_HOME"], t_fault + delay,
               "LIMP_HOME latched after sensor recovery")


@pytest.mark.req("REQ-009")
def test_speed_sensor_fault(run, spec, measured):
    a = spec["REQ-009"]["acceptance"]
    t_fault = 1.0
    tr = run(Scenario("speed_sensor_dropout_at_full_torque", 3.0,
                      speed_req=[(0.0, 0.0), (0.2, 1000.0)],
                      sensor_faults=[SensorFaultEvent(t_fault, "speed", "dropout")]))

    delay = eventually(tr, tr.mode == MODES["SAFE"], a["max_delay_s"], "mode == SAFE",
                       after=t_fault)
    measured("delay to SAFE", delay * 1000, "ms", f"≤ {a['max_delay_s'] * 1000:.0f}")
    t_zero = eventually(tr, np.abs(tr.torque_cmd) < 1e-3, a["torque_zero_s"],
                        "torque_cmd == 0", after=t_fault)
    measured("time to zero torque", t_zero, "s", f"≤ {a['torque_zero_s']}")
    holds_from(tr, tr.mode == MODES["SAFE"], t_fault + delay, "SAFE latched")


@pytest.mark.req("REQ-015")
@pytest.mark.xfail(strict=True, reason="Known gap: stuck-sensor detection not implemented "
                                       "in motor_ctrl.c v0.1.0")
def test_stuck_temperature_sensor(run, spec, measured):
    a = spec["REQ-015"]["acceptance"]
    t_stuck = 5.0
    tr = run(Scenario("temp_sensor_stuck_under_load", t_stuck + a["max_delay_s"] + 2.0,
                      speed_req=[(0.0, 650.0)], initial_temps=(100.0, 90.0),
                      sensor_faults=[SensorFaultEvent(t_stuck, "temp", "stuck")]))

    measured("true winding rise while stuck",
             float(tr.temp[-1] - tr.at("temp", t_stuck)), "K", "")
    eventually(tr, tr.mode == MODES["LIMP_HOME"], a["max_delay_s"], "mode == LIMP_HOME",
               after=t_stuck)
