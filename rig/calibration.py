"""
calibration.py - Load and validate calibration files.

Two files, two owners:
  calibration/limits.json   software team: which parameters exist, units, allowed ranges, rules
  calibration/*.json        calibration engineer: the chosen values

Validation runs twice on purpose (defence in depth):
  1. here, in CI, before anything is packaged         -> tools/check_calibration.py
  2. in the C code at start-up, MC_CheckCal()          -> rejects and forces SAFE mode
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CAL = ROOT / "calibration" / "default.json"
DEFAULT_LIMITS = ROOT / "calibration" / "limits.json"


@dataclass(frozen=True)
class Calibration:
    name: str
    version: str
    values: dict
    sha256: str
    path: str


def sha256_file(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path: Path | str = DEFAULT_CAL) -> Calibration:
    path = Path(path)
    doc = json.loads(path.read_text())
    meta = doc.get("meta", {})
    return Calibration(name=meta.get("name", path.stem), version=meta.get("version", "0"),
                       values=dict(doc["values"]), sha256=sha256_file(path), path=str(path))


def load_limits(path: Path | str = DEFAULT_LIMITS) -> dict:
    return json.loads(Path(path).read_text())


def validate(values: dict, limits: dict | None = None) -> list[str]:
    """Returns a list of human-readable problems; empty means valid."""
    limits = limits or load_limits()
    params = limits["parameters"]
    errors = []

    for key in sorted(set(params) - set(values)):
        errors.append(f"{key}: missing")
    for key in sorted(set(values) - set(params)):
        errors.append(f"{key}: unknown parameter (not in limits.json)")

    for key, spec in params.items():
        if key not in values:
            continue
        v = values[key]
        if spec["type"] == "int" and (not isinstance(v, int) or isinstance(v, bool)):
            errors.append(f"{key}: must be an integer, got {v!r}")
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
            errors.append(f"{key}: must be a number, got {v!r}")
            continue
        if not spec["min"] <= v <= spec["max"]:
            errors.append(f"{key} = {v} {spec['unit']}: outside [{spec['min']}, {spec['max']}]")

    if not errors:   # cross-parameter rules only make sense on individually valid values
        for rule in limits.get("rules", []):
            if not eval(rule["expr"], {"__builtins__": {}}, dict(values)):  # noqa: S307 - trusted, repo-owned file
                errors.append(f"rule violated: {rule['expr']} ({rule['why']})")
    return errors


def to_struct(values: dict):
    """Convert a values dict into the ctypes MC_Cal struct for the C controller."""
    from rig.ctrl_sil import MCCal
    cal = MCCal()
    names = {f[0] for f in MCCal._fields_}
    missing = names - set(values)
    if missing:
        raise KeyError(f"calibration missing fields required by MC_Cal: {sorted(missing)}")
    for name in names:
        setattr(cal, name, values[name])
    return cal
