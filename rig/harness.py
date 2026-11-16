"""
harness.py - Deterministic lockstep SIL runner.

One 10 ms tick, in order:
  1. Harness (VCU)  sends VCU_Request          ─┐
  2. Inverter       sends INV_Status            │  CAN bus (virtual)
  3. ECU            receives, MC_Step(), sends ECU_TorqueCmd / ECU_Status
  4. Inverter       receives torque command    ─┘
  5. Plant          integrates 10 ms with that torque
  6. Recorder       stores every signal

transport="can"    every signal is encoded/decoded through can/motor.dbc (default)
transport="direct" values handed straight to the C code (fast, no quantisation)
"""

from __future__ import annotations

import bisect
import datetime as dt
import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from rig import calibration as calmod
from rig.can_bus import DEFAULT_DBC, BusFault, CanCodec, EcuNode, InverterNode, open_virtual_bus
from rig.ctrl_sil import DEFAULT_LIB, MotorController
from rig.plant import MotorPlant, PlantInputs, PlantParams, SensorFault
from rig.trace import SIGNALS, Trace

DT = 0.01


@dataclass(frozen=True)
class SensorFaultEvent:
    t: float
    sensor: str               # "speed" | "temp"
    mode: str                 # see rig.plant.FAULT_MODES; "none" clears
    value: float = 0.0


@dataclass
class Scenario:
    """Piecewise-constant profiles: [(t0, v0), (t1, v1), ...] means v0 from t0, v1 from t1, ..."""
    name: str
    duration: float
    speed_req: list[tuple[float, float]]
    load: list[tuple[float, float]] = field(default_factory=lambda: [(0.0, 0.0)])
    ambient: float = 25.0
    initial_temps: tuple[float, float] | None = None
    sensor_faults: list[SensorFaultEvent] = field(default_factory=list)
    bus_faults: list[BusFault] = field(default_factory=list)
    seed: int = 0


def _profile(points: list[tuple[float, float]]):
    times = [p[0] for p in points]
    values = [p[1] for p in points]

    def at(t: float) -> float:
        i = bisect.bisect_right(times, t + 1e-9) - 1
        return values[max(i, 0)]
    return at


def git_commit() -> str:
    for var in ("GIT_COMMIT", "GITHUB_SHA"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True, cwd=Path(__file__).parent).stdout.strip()
    except Exception:   # noqa: BLE001 - not a git checkout, or git missing
        return "unknown"


class Rig:
    def __init__(self, calibration: calmod.Calibration | dict | str | Path | None = None,
                 transport: str = "can", lib: Path | str = DEFAULT_LIB,
                 dbc: Path | str = DEFAULT_DBC, plant_params: PlantParams | None = None) -> None:
        if transport not in ("can", "direct"):
            raise ValueError("transport must be 'can' or 'direct'")
        if isinstance(calibration, dict):
            self.cal = calmod.Calibration("inline", "0", dict(calibration), "inline", "inline")
        elif isinstance(calibration, calmod.Calibration):
            self.cal = calibration
        else:
            self.cal = calmod.load(calibration or calmod.DEFAULT_CAL)
        self.transport = transport
        self.ctrl = MotorController(lib)
        self.dbc = Path(dbc)
        self.codec = CanCodec(self.dbc) if transport == "can" else None
        self.plant_params = plant_params or PlantParams()
        self.cal_status = None

    def run(self, sc: Scenario) -> Trace:
        self.cal_status = self.ctrl.init(calmod.to_struct(self.cal.values))
        plant = MotorPlant(self.plant_params, seed=sc.seed)
        meas = plant.reset(ambient_temp=sc.ambient, seed=sc.seed, initial_temps=sc.initial_temps)
        speed_at, load_at = _profile(sc.speed_req), _profile(sc.load)
        pending = sorted(sc.sensor_faults, key=lambda e: e.t)

        ecu = inv = None
        if self.transport == "can":
            channel = open_virtual_bus()
            ecu = EcuNode(channel, self.codec, self.ctrl, faults=sc.bus_faults)
            inv = InverterNode(channel, self.codec, faults=sc.bus_faults)

        n = round(sc.duration / DT)
        rec = {k: np.zeros(n) for k in SIGNALS}
        try:
            for k in range(n):
                t = k * DT
                while pending and pending[0].t <= t + 1e-9:
                    e = pending.pop(0)
                    plant.set_fault(e.sensor, SensorFault(e.mode, e.value))
                req, load = speed_at(t), load_at(t)

                if self.transport == "can":
                    inv.send("VCU_Request", {"SpeedRequest": req}, t)
                    inv.publish(meas, t)
                    out = ecu.step(k, t)
                    used = ecu.inputs_used
                    torque_cmd = inv.torque_command(k)
                    rx_err, rx_stale = ecu.rx_errors, ecu.rx_stale
                else:
                    out = self.ctrl.step(req, meas.speed_meas, meas.speed_valid,
                                         meas.temp_meas, meas.temp_valid)
                    used = dict(speed_req=req, speed=meas.speed_meas, temp=meas.temp_meas,
                                speed_valid=meas.speed_valid, temp_valid=meas.temp_valid)
                    torque_cmd = out.torque_cmd
                    rx_err = rx_stale = 0

                row = dict(t=t, speed_req=req, speed=meas.omega, speed_ecu=used["speed"],
                           speed_valid=used["speed_valid"], temp=meas.theta_winding,
                           temp_ecu=used["temp"], temp_valid=used["temp_valid"],
                           torque_cmd=out.torque_cmd, torque_limit=out.torque_limit,
                           torque=meas.torque_actual, load=load, mode=out.mode,
                           derate=out.derate_active, faults=out.faults,
                           rx_errors=rx_err, rx_stale=rx_stale)
                for key, val in row.items():
                    rec[key][k] = val

                meas = plant.step(PlantInputs(torque_cmd=torque_cmd, load_torque=load,
                                              ambient_temp=sc.ambient), DT)
        finally:
            if ecu:
                ecu.close()
                inv.close()

        return Trace(rec, self._meta(sc))

    def _meta(self, sc: Scenario) -> dict:
        return {
            "scenario": sc.name,
            "duration_s": sc.duration,
            "seed": sc.seed,
            "transport": self.transport,
            "firmware_version": self.ctrl.version,
            "calibration": {"name": self.cal.name, "version": self.cal.version,
                            "sha256": self.cal.sha256},
            "calibration_status": int(self.cal_status or 0),
            "dbc_sha256": calmod.sha256_file(self.dbc) if self.transport == "can" else None,
            "git_commit": git_commit(),
            "recorded_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "python": platform.python_version(),
        }
