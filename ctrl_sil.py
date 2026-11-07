"""
ctrl_sil.py - Python shim for the C controller (software-in-the-loop).

Mirrors the C structs with ctypes and checks at load time that the layouts
match, so a header change can never silently corrupt the data exchange.
"""

from __future__ import annotations

import ctypes as C
from pathlib import Path

DEFAULT_LIB = Path(__file__).parent / "controller" / "build" / "libmotorctrl.so"

MODE_NAMES = {0: "INIT", 1: "NORMAL", 2: "DERATED", 3: "LIMP_HOME", 4: "SAFE"}


class MCInputs(C.Structure):
    _fields_ = [
        ("speed_req", C.c_float),
        ("speed_meas", C.c_float),
        ("temp_meas", C.c_float),
        ("speed_valid", C.c_uint8),
        ("temp_valid", C.c_uint8),
    ]


class MCOutputs(C.Structure):
    _fields_ = [
        ("torque_cmd", C.c_float),
        ("torque_limit", C.c_float),
        ("faults", C.c_uint16),
        ("mode", C.c_uint8),
        ("derate_active", C.c_uint8),
    ]


class MCCal(C.Structure):
    _fields_ = [(name, C.c_float) for name in (
        "dt", "kp", "ki", "integ_limit", "torque_max",
        "derate_start", "derate_end", "derate_min_frac", "derate_hyst",
        "temp_critical", "limp_frac",
        "limit_slew_down", "limit_slew_up", "safe_ramp",
        "temp_min_valid", "temp_max_valid", "temp_jump", "temp_filt_tau",
        "speed_max_valid", "speed_jump", "speed_filt_tau",
    )] + [("fault_debounce_ticks", C.c_uint16)]


class MotorController:
    def __init__(self, lib_path: Path | str = DEFAULT_LIB) -> None:
        self._lib = C.CDLL(str(lib_path))
        self._lib.sil_init.argtypes = [C.POINTER(MCCal)]
        self._lib.sil_init.restype = C.c_uint16
        self._lib.sil_step.argtypes = [C.POINTER(MCInputs), C.POINTER(MCOutputs)]
        self._lib.sil_default_cal.argtypes = [C.POINTER(MCCal)]
        self._lib.sil_version.restype = C.c_char_p
        self._check_layout()
        self._in, self._out = MCInputs(), MCOutputs()

    def _check_layout(self) -> None:
        sizes = [C.c_uint32() for _ in range(3)]
        self._lib.sil_struct_sizes(*[C.byref(s) for s in sizes])
        for name, c_size, py in zip(("Inputs", "Outputs", "Cal"), sizes,
                                    (MCInputs, MCOutputs, MCCal)):
            if c_size.value != C.sizeof(py):
                raise RuntimeError(f"Struct mismatch MC_{name}: C={c_size.value} B, Python={C.sizeof(py)} B")

    @property
    def version(self) -> str:
        return self._lib.sil_version().decode()

    def default_cal(self) -> MCCal:
        cal = MCCal()
        self._lib.sil_default_cal(C.byref(cal))
        return cal

    def init(self, cal: MCCal | None = None) -> int:
        """Returns 0 if the calibration was accepted, else the CAL_INVALID fault bit."""
        return self._lib.sil_init(C.byref(cal) if cal is not None else None)

    def step(self, speed_req: float, speed_meas: float, speed_valid: bool,
             temp_meas: float, temp_valid: bool) -> MCOutputs:
        self._in.speed_req, self._in.speed_meas = speed_req, speed_meas
        self._in.temp_meas = temp_meas
        self._in.speed_valid, self._in.temp_valid = int(speed_valid), int(temp_valid)
        self._lib.sil_step(C.byref(self._in), C.byref(self._out))
        return self._out
