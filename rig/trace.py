"""
trace.py - Recorded signals of one SIL run, plus the metadata that makes it evidence.

Every run records every signal at every 10 ms tick, together with the exact
firmware version, calibration hash, DBC hash and scenario that produced it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

MODE_NAMES = {0: "INIT", 1: "NORMAL", 2: "DERATED", 3: "LIMP_HOME", 4: "SAFE"}
MODES = {v: k for k, v in MODE_NAMES.items()}

SIGNALS = (
    "t",              # s
    "speed_req",      # rad/s, request sent by the harness
    "speed",          # rad/s, true rotor speed (plant)
    "speed_ecu",      # rad/s, speed the ECU received over CAN
    "speed_valid",    # 0/1, validity as seen by the ECU
    "temp",           # degC, true winding temperature (plant)
    "temp_ecu",       # degC, temperature the ECU received over CAN
    "temp_valid",     # 0/1, validity as seen by the ECU
    "torque_cmd",     # N*m, ECU output
    "torque_limit",   # N*m, ECU output
    "torque",         # N*m, torque actually produced (plant)
    "load",           # N*m, external load torque
    "mode",           # MC_Mode
    "derate",         # 0/1
    "faults",         # MC_FAULT_* bits
    "rx_errors",      # cumulative frames the ECU discarded (bad checksum)
    "rx_stale",       # cumulative frames the ECU discarded (frozen alive counter)
)


@dataclass
class Trace:
    data: dict[str, np.ndarray]
    meta: dict = field(default_factory=dict)

    def __getattr__(self, name: str) -> np.ndarray:
        try:
            return self.__dict__["data"][name]
        except KeyError:
            raise AttributeError(name) from None

    def __len__(self) -> int:
        return len(self.data["t"])

    def window(self, t0: float = 0.0, t1: float = float("inf")) -> np.ndarray:
        """Boolean mask for t0 <= t < t1."""
        return (self.t >= t0 - 1e-9) & (self.t < t1 - 1e-9)

    def at(self, signal: str, t: float) -> float:
        i = int(np.searchsorted(self.t, t - 1e-9))
        return float(self.data[signal][min(i, len(self) - 1)])

    def first_time(self, mask: np.ndarray, after: float = 0.0) -> float | None:
        idx = np.flatnonzero(mask & (self.t >= after - 1e-9))
        return float(self.t[idx[0]]) if idx.size else None

    def mode_name(self, i: int = -1) -> str:
        return MODE_NAMES[int(self.mode[i])]

    # ---- Persistence -------------------------------------------------------

    def save(self, stem: Path | str) -> None:
        stem = Path(stem)
        stem.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(stem.with_suffix(".npz"), **self.data)
        stem.with_suffix(".json").write_text(json.dumps(self.meta, indent=2))

    @classmethod
    def load(cls, stem: Path | str) -> "Trace":
        stem = Path(stem)
        with np.load(stem.with_suffix(".npz")) as z:
            data = {k: z[k] for k in z.files}
        return cls(data, json.loads(stem.with_suffix(".json").read_text()))

    def plot(self, path: Path | str, title: str = "") -> None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(4, 1, figsize=(9, 8.5), sharex=True)
        ax[0].plot(self.t, self.speed_req, "--", label="Request")
        ax[0].plot(self.t, self.speed, label="Actual")
        ax[0].set_ylabel("rad/s"); ax[0].legend(loc="best"); ax[0].set_title("Speed")
        ax[1].plot(self.t, self.torque_cmd, label="Torque command")
        ax[1].plot(self.t, self.torque_limit, label="Torque limit")
        ax[1].set_ylabel("N·m"); ax[1].legend(loc="best"); ax[1].set_title("Torque")
        temp_ecu = np.where(self.temp_valid > 0, self.temp_ecu, np.nan)
        ax[2].plot(self.t, self.temp, label="Winding (true)")
        ax[2].plot(self.t, temp_ecu, label="Winding (ECU)", alpha=0.7)
        ax[2].set_ylabel("°C"); ax[2].legend(loc="best"); ax[2].set_title("Temperature")
        ax[3].step(self.t, self.mode, where="post")
        ax[3].set_yticks(list(MODE_NAMES)); ax[3].set_yticklabels(MODE_NAMES.values())
        ax[3].set_title("ECU mode"); ax[3].set_xlabel("Time [s]")
        fig.suptitle(title or self.meta.get("scenario", ""))
        fig.tight_layout()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=90)
        plt.close(fig)
