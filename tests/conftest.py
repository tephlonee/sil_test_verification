"""
Shared fixtures and the requirement-traceability plumbing.

  @pytest.mark.req("REQ-001")   links a test to a requirement in spec/requirements.yaml
  run(scenario)                 runs a SIL scenario and saves its trace + plot as evidence
  measured(label, value, ...)   records a measured value next to its limit for the report

After the session, reports/req_results.json holds every linked test's outcome,
measured values and evidence files - tools/trace_report.py turns it into the
traceability matrix.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

import pytest
import yaml

from rig import calibration
from rig.harness import Rig, git_commit

ROOT = Path(__file__).resolve().parent.parent
# SIL_REPORTS_DIR lets a tool (e.g. the test drafter's pytest gate) run tests without
# overwriting the real evidence in reports/.
REPORTS = Path(os.environ.get("SIL_REPORTS_DIR") or ROOT / "reports")
EVIDENCE = REPORTS / "evidence"
SPEC = ROOT / "spec" / "requirements.yaml"

_results: list[dict] = []
_run_meta: dict = {}


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")[:80]


# ---------------------------------------------------------------- Fixtures


@pytest.fixture(scope="session")
def spec() -> dict:
    doc = yaml.safe_load(SPEC.read_text())
    return {r["id"]: r for r in doc["requirements"]}


@pytest.fixture(scope="session")
def cal() -> dict:
    return calibration.load().values


@pytest.fixture(scope="session")
def rig() -> Rig:
    r = Rig()
    _run_meta.update(firmware_version=r.ctrl.version,
                     calibration={"name": r.cal.name, "version": r.cal.version, "sha256": r.cal.sha256},
                     dbc_sha256=calibration.sha256_file(r.dbc))
    return r


@pytest.fixture
def run(rig, request):
    """Run a scenario on the rig (or on another rig passed in) and keep the evidence."""
    def _run(scenario, on: Rig | None = None):
        tr = (on or rig).run(scenario)
        stem = EVIDENCE / f"{_slug(request.node.name)}__{_slug(scenario.name)}"
        tr.save(stem)
        png = stem.with_suffix(".png")
        tr.plot(png, title=f"{request.node.name} - {scenario.name}")
        request.node.user_properties.append(("evidence", str(png.relative_to(REPORTS))))
        return tr
    return _run


@pytest.fixture
def measured(request):
    def _record(label: str, value: float, unit: str = "", limit: str = "") -> None:
        request.node.user_properties.append(
            ("measured", {"label": label, "value": round(float(value), 4), "unit": unit, "limit": limit}))
    return _record


# ---------------------------------------------------------------- Result collection


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    reqs = [rid for m in item.iter_markers("req") for rid in m.args]
    if not reqs:
        return
    final = rep.when == "call" or (rep.when == "setup" and not rep.passed)
    if not final:
        return

    if hasattr(rep, "wasxfail"):
        status = "known_gap" if rep.skipped else "xpassed"
    else:
        status = rep.outcome     # passed | failed | skipped

    message = ""
    if call.excinfo is not None:
        message = str(call.excinfo.value).strip().splitlines()[0][:300] if str(call.excinfo.value) else call.excinfo.typename

    props = item.user_properties
    _results.append({
        "test": item.nodeid,
        "requirements": reqs,
        "status": status,
        "duration_s": round(rep.duration, 3),
        "message": message,
        "measured": [v for k, v in props if k == "measured"],
        "evidence": [v for k, v in props if k == "evidence"],
    })


def pytest_sessionfinish(session, exitstatus):
    if not _results:
        return
    REPORTS.mkdir(exist_ok=True)
    doc = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "git_commit": git_commit(),
        **_run_meta,
        "results": _results,
    }
    (REPORTS / "req_results.json").write_text(json.dumps(doc, indent=2))
