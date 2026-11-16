"""CAN interface: DBC encoding, and the ECU's reaction to bus faults."""

import numpy as np
import pytest

from rig.assertions import always, eventually
from rig.can_bus import BusFault, CanCodec
from rig.harness import Scenario
from rig.trace import MODES

# ---------------------------------------------------------------- DBC sanity (no requirement)


def test_dbc_round_trip_quantisation():
    codec = CanCodec()
    data = codec.encode("INV_Status", {"MotorSpeed": 312.347, "MotorTemp": 121.26,
                                       "SpeedValid": 1, "TempValid": 0}, counter=3)
    name, sig, ok = codec.decode(codec.frame_id("INV_Status"), data)
    assert ok and name == "INV_Status"
    assert sig["MotorSpeed"] == pytest.approx(312.347, abs=0.05)   # 0.1 rad/s resolution
    assert sig["MotorTemp"] == pytest.approx(121.26, abs=0.05)     # 0.1 degC resolution
    assert (sig["SpeedValid"], sig["TempValid"], sig["AliveCounter"]) == (1, 0, 3)


def test_dbc_checksum_detects_bit_flip():
    codec = CanCodec()
    data = bytearray(codec.encode("ECU_TorqueCmd", {"TorqueRequest": 100.0, "TorqueLimit": 250.0}, 1))
    data[0] ^= 0x01
    assert codec.decode(codec.frame_id("ECU_TorqueCmd"), bytes(data))[2] is False


def test_dbc_saturates_out_of_range_values():
    codec = CanCodec()
    data = codec.encode("VCU_Request", {"SpeedRequest": 5000.0}, 1)
    assert codec.decode(codec.frame_id("VCU_Request"), data)[1]["SpeedRequest"] == 1200.0

# ---------------------------------------------------------------- Requirements


@pytest.mark.req("REQ-011")
@pytest.mark.parametrize("kind", ["drop", "freeze_counter"])
def test_inverter_message_loss_forces_safe(run, spec, measured, kind):
    a = spec["REQ-011"]["acceptance"]
    t0 = 2.0
    tr = run(Scenario(f"INV_Status_{kind}", 4.0, speed_req=[(0.0, 300.0)],
                      bus_faults=[BusFault("INV_Status", t0, 3.5, kind)]))
    delay = eventually(tr, tr.mode == MODES["SAFE"], a["max_delay_s"], "mode == SAFE", after=t0)
    measured("delay to SAFE", delay * 1000, "ms", f"≤ {a['max_delay_s'] * 1000:.0f}")


@pytest.mark.req("REQ-011")
def test_short_inverter_gap_is_tolerated(run, spec, measured):
    a = spec["REQ-011"]["acceptance"]
    t0 = 2.0
    tr = run(Scenario("INV_Status_30ms_gap", 4.0, speed_req=[(0.0, 300.0)],
                      bus_faults=[BusFault("INV_Status", t0, t0 + a["tolerated_gap_s"], "drop")]))
    measured("final mode", tr.mode[-1], "", "NORMAL (1)")
    always(tr, tr.mode == MODES["NORMAL"], "mode == NORMAL", t0=1.0)
    always(tr, tr.faults == 0, "no faults set")


@pytest.mark.req("REQ-012")
def test_corrupted_frames_discarded(run, spec, measured):
    n = spec["REQ-012"]["acceptance"]["corrupted_frames"]
    faults = [BusFault("INV_Status", 1.0 + i, 1.0 + i + 0.005, "corrupt") for i in range(n)]
    tr = run(Scenario("isolated_INV_Status_corruption", 6.0, speed_req=[(0.0, 300.0)],
                      bus_faults=faults))
    measured("frames discarded", tr.rx_errors[-1], "", f"= {n}")
    assert int(tr.rx_errors[-1]) == n, f"ECU discarded {int(tr.rx_errors[-1])} frames, expected {n}"
    always(tr, tr.mode == MODES["NORMAL"], "mode == NORMAL", t0=0.5)
    always(tr, tr.faults == 0, "no faults set")


@pytest.mark.req("REQ-014")
def test_speed_request_loss_stops_motor(run, spec, measured):
    a = spec["REQ-014"]["acceptance"]
    t0 = 2.0
    tr = run(Scenario("VCU_Request_lost", 5.0, speed_req=[(0.0, 300.0)],
                      bus_faults=[BusFault("VCU_Request", t0, 5.0, "drop")]))
    t_stop = eventually(tr, np.abs(tr.speed) < a["stop_speed_rad_s"], a["stop_time_s"],
                        f"|speed| < {a['stop_speed_rad_s']} rad/s", after=t0)
    measured("time to stop", t_stop, "s", f"≤ {a['stop_time_s']}")
