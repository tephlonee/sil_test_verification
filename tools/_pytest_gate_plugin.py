"""
pytest plugin used only by the drafter's pytest gate (tools/drafter_gates.py).

Records, per test, whether it passed, failed on an AssertionError (a verdict on the
firmware - allowed) or errored any other way (a broken test - rejected), and writes
the record to $DRAFTER_GATE_OUT.
"""

from __future__ import annotations

import json
import os

import pytest

_rec: dict = {"collect_errors": [], "tests": []}


def _first_line(text: str) -> str:
    text = str(text).strip()
    return (text.splitlines()[0] if text else "")[:300]


def pytest_collectreport(report):
    if report.failed:
        _rec["collect_errors"].append(f"{report.nodeid or '<module>'}: "
                                      + "\n".join(str(report.longrepr).splitlines()[-6:])[:800])


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.passed and call.when != "call":
        return
    if rep.skipped:
        _rec["tests"].append({"nodeid": item.nodeid, "when": call.when, "kind": "error",
                              "message": "test was skipped"})
        return
    if rep.passed:
        _rec["tests"].append({"nodeid": item.nodeid, "when": "call", "kind": "passed", "message": ""})
        return
    exc = call.excinfo
    is_assertion = call.when == "call" and exc is not None and exc.errisinstance(AssertionError)
    _rec["tests"].append({
        "nodeid": item.nodeid,
        "when": call.when,
        "kind": "failed" if is_assertion else "error",
        "message": f"{exc.typename}: {_first_line(exc.value)}" if exc else "unknown error",
    })


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("DRAFTER_GATE_OUT")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_rec, f, indent=2)
