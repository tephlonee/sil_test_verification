"""Calibration validity: the CI checker and the ECU must both reject a bad calibration."""

import numpy as np
import pytest

from rig import calibration
from rig.assertions import always
from rig.harness import Rig, Scenario
from rig.trace import MODES

INVALID_CALIBRATIONS = {
    "derate_end_below_start": {"derate_end": 110.0},
    "gain_out_of_range": {"kp": 500.0},
    "critical_below_derate_end": {"temp_critical": 140.0},
}


def test_default_calibration_is_valid():
    cal = calibration.load()
    assert calibration.validate(cal.values) == []


@pytest.mark.req("REQ-013")
@pytest.mark.parametrize("change", INVALID_CALIBRATIONS.values(), ids=INVALID_CALIBRATIONS.keys())
def test_invalid_calibration_rejected(run, spec, cal, measured, change):
    fault_bit = spec["REQ-013"]["acceptance"]["fault_bit"]
    bad = {**cal, **change}

    errors = calibration.validate(bad)
    assert errors, "CI calibration check accepted an invalid calibration"
    measured("CI check findings", len(errors), "", "≥ 1")

    tr = run(Scenario("start_with_invalid_calibration", 1.0, speed_req=[(0.0, 300.0)]),
             on=Rig(calibration=bad))
    always(tr, tr.mode == MODES["SAFE"], "mode == SAFE")
    always(tr, (tr.faults.astype(int) & fault_bit) != 0, "CAL_INVALID fault set")
    always(tr, np.abs(tr.torque_cmd) == 0.0, "torque_cmd == 0")
