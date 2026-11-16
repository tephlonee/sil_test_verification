"""
package_release.py - Build a versioned, self-describing release package.

    dist/motor-ctrl-<version>+<commit>.zip
      firmware/libmotorctrl.so      the software (SIL build; a target build later)
      calibration/default.json      the calibration it was verified with
      can/motor.dbc                 the interface it was verified against
      reports/traceability.html     the verification evidence summary
      manifest.json                 versions, SHA-256 of every file, verification summary

Refuses to package an invalid calibration. Run after the tests and trace report.
Usage:  python -m tools.package_release
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from rig import calibration
from rig.harness import git_commit

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
HEADER = ROOT / "controller" / "include" / "motor_ctrl.h"

CONTENTS = {
    "firmware/libmotorctrl.so": ROOT / "controller" / "build" / "libmotorctrl.so",
    "calibration/default.json": calibration.DEFAULT_CAL,
    "can/motor.dbc": ROOT / "can" / "motor.dbc",
    "reports/traceability.html": ROOT / "reports" / "traceability.html",
}


def sw_version() -> str:
    m = re.search(r'#define\s+MC_SW_VERSION\s+"([^"]+)"', HEADER.read_text())
    if not m:
        raise SystemExit("MC_SW_VERSION not found in motor_ctrl.h")
    return m.group(1)


def junit_summary(path: Path) -> dict | None:
    if not path.exists():
        return None
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    total = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for s in suites:
        for k in total:
            total[k] += int(s.get(k, 0))
    total["passed"] = total["tests"] - total["failures"] - total["errors"] - total["skipped"]
    return total


def requirement_summary() -> dict | None:
    import yaml
    from tools.trace_report import RESULTS, SPEC, build
    if not RESULTS.exists():
        return None
    rows, problems = build(yaml.safe_load(SPEC.read_text()), json.loads(RESULTS.read_text()))
    by = lambda v: [r["id"] for r in rows if r["verdict"] == v]   # noqa: E731
    return {"total": len(rows), "verified": by("VERIFIED"), "known_gaps": by("KNOWN GAP"),
            "failed": by("FAILED"), "not_tested": by("NOT TESTED"), "gate_passed": not problems}


def main() -> int:
    missing = [str(src) for src in CONTENTS.values() if not src.exists()]
    if missing:
        print("Cannot package, missing:\n  " + "\n  ".join(missing))
        return 1

    cal = calibration.load()
    errors = calibration.validate(cal.values)
    if errors:
        print("Refusing to package an invalid calibration:\n  " + "\n  ".join(errors))
        return 1

    version, commit = sw_version(), git_commit()
    short = commit[:8] if commit != "unknown" else "local"
    name = f"motor-ctrl-{version}+{short}"

    manifest = {
        "package": "motor-ctrl",
        "sw_version": version,
        "git_commit": commit,
        "build_id": os.environ.get("BUILD_NUMBER") or os.environ.get("GITHUB_RUN_NUMBER") or "local",
        "built_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "calibration": {"name": cal.name, "version": cal.version},
        "files": {arc: {"sha256": calibration.sha256_file(src), "bytes": src.stat().st_size}
                  for arc, src in CONTENTS.items()},
        "verification": {
            "tests": junit_summary(ROOT / "reports" / "tests.xml"),
            "requirements": requirement_summary(),
        },
    }

    DIST.mkdir(exist_ok=True)
    out = DIST / f"{name}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, src in CONTENTS.items():
            z.write(src, f"{name}/{arc}")
        z.writestr(f"{name}/manifest.json", json.dumps(manifest, indent=2))

    print(f"Packaged {out.relative_to(ROOT)}")
    print(json.dumps({k: manifest[k] for k in ("sw_version", "git_commit", "calibration")}, indent=2))
    req = manifest["verification"]["requirements"]
    if req:
        print(f"Requirements verified: {len(req['verified'])}/{req['total']}, "
              f"known gaps: {req['known_gaps']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
