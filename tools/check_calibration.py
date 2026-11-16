"""
check_calibration.py - CI gate: validate calibration files against calibration/limits.json.

Usage:  python -m tools.check_calibration [calibration/default.json ...]
Exit code 1 if any file is invalid.
"""

import sys
from pathlib import Path

from rig.calibration import DEFAULT_CAL, load, load_limits, validate


def main(paths: list[str]) -> int:
    limits = load_limits()
    failed = 0
    for path in paths or [str(DEFAULT_CAL)]:
        cal = load(path)
        errors = validate(cal.values, limits)
        status = "OK" if not errors else "INVALID"
        print(f"{status:8} {Path(path).name}  (name={cal.name}, version={cal.version}, sha256={cal.sha256[:12]})")
        for e in errors:
            print(f"         - {e}")
        if not errors:
            for key, spec in limits["parameters"].items():
                print(f"         {key:22} {cal.values[key]:>10} {spec['unit']:14} [{spec['min']}, {spec['max']}]")
        failed += bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
