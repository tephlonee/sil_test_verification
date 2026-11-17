# DRAFT - requires human review before moving to tests/
# requirement:   REQ-016
# model:         qwen2.5-coder:7b
# generated_utc: 2026-11-17T23:38:51+00:00
# prompt_sha256: e9ea3c0210f016d44289b20fa8bc959e8331921a46f72428666302e5adf00354
# attempts:      1
# gates:         parse=pass imports=pass forbidden=pass req_marker=pass acceptance=pass pytest=pass
# pytest result: 0 passed, 1 failed - first failure: AssertionError: '|speed| < 10 rad/s' expected within 2000 ms of t=2.000 s, first true at never
# The gates check that this file is a well-formed, safe, linked test. They do NOT
# check that it verifies the requirement correctly - that is the reviewer's job.

import numpy as np
import pytest

from rig.assertions import always, eventually, settles
from rig.harness import Scenario
from rig.trace import MODES

@pytest.mark.req("REQ-016")
def test_implausible_speed_request(run, spec, measured):
    a = spec["REQ-016"]["acceptance"]
    t0 = 2.0
    tr = run(Scenario("implausible_speed_request", 4.0, speed_req=[(0.0, 300.0), (t0, 1500.0), (t0 + 2.0, 300.0)]))
    
    delay_to_stop = eventually(tr, np.abs(tr.speed) < a["stop_speed_rad_s"], a["stop_time_s"],
                               f"|speed| < {a['stop_speed_rad_s']} rad/s", after=t0)
    measured("time to stop", delay_to_stop, "s", f"≤ {a['stop_time_s']}")
    
    always(tr, np.abs(tr.speed) < a["stop_speed_rad_s"], "speed < 10 rad/s", after=t0)
    always(tr, tr.mode == MODES["NORMAL"], "mode == NORMAL", after=t0)
    always(tr, tr.faults == 0, "no faults set")
