"""
drafter_gates.py - Deterministic gates for LLM-drafted tests. No LLM in here.

A draft from tools/draft_test.py must pass every gate, in this order, before it is
saved for human review:

  parse        the code parses with ast
  imports      only pytest, numpy, rig.assertions, rig.harness, rig.can_bus, rig.trace
               (and only the public test-facing names of the rig modules)
  forbidden    no file, network, subprocess, eval/exec, os, dunder or reflection access
  req_marker   every test carries @pytest.mark.req("<requested id>"), no other marks
               besides parametrize (no skip/xfail that would hide a result)
  acceptance   thresholds come from spec["<id>"]["acceptance"]; acceptance values
               may not appear as literals in asserts, comparisons or assertion calls
  pytest       the file runs under pytest: passing or failing on an AssertionError is
               fine (that is a verdict on the firmware); collection errors, fixture
               errors, other exceptions and timeouts are not (that is a broken test)

The gates decide whether a draft is worth a reviewer's time. They never decide
whether a requirement is met - that is the accepted test's job, in CI.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "tests" / "drafts" / "_staging"
GATE_PLUGIN = "tools._pytest_gate_plugin"

GATE_ORDER = ("parse", "imports", "forbidden", "req_marker", "acceptance", "pytest")

# `import x [as y]` allowed for these
MODULE_IMPORTS = {"pytest", "numpy"}
# `from x import a, b` allowed for these modules and names
FROM_IMPORTS: dict[str, set[str]] = {
    "__future__": {"annotations"},
    "pytest": {"approx", "mark", "param", "raises"},
    "rig.assertions": {"settles", "overshoot", "always", "never", "eventually", "holds_from"},
    "rig.harness": {"Rig", "Scenario", "SensorFaultEvent", "DT"},
    "rig.can_bus": {"BusFault", "CanCodec", "FAULT_KINDS", "RX_TIMEOUT_TICKS"},
    "rig.trace": {"MODES", "MODE_NAMES", "SIGNALS", "Trace"},
}

FORBIDDEN_CALLS = {
    "open", "eval", "exec", "compile", "__import__", "input", "breakpoint", "globals",
    "locals", "vars", "getattr", "setattr", "delattr", "exit", "quit", "help", "memoryview",
}
FORBIDDEN_NAMES = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "Path", "importlib", "builtins",
    "urllib", "http", "requests", "ctypes", "pickle", "tempfile", "glob", "io",
}
# File I/O through numpy, Trace and pathlib-like objects, and process/network helpers
FORBIDDEN_ATTRS = {
    "save", "savez", "savez_compressed", "savetxt", "load", "loadtxt", "genfromtxt",
    "fromfile", "tofile", "memmap", "DataSource", "plot", "open", "write_text", "read_text",
    "write_bytes", "read_bytes", "system", "popen", "spawn", "ctypes", "lib",
}

ALLOWED_MARKS = {"req", "parametrize"}
ASSERTION_CALLS = {"settles", "overshoot", "always", "never", "eventually", "holds_from", "approx"}
TRIVIAL_NUMBERS = {0, 1}


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""
    extra: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.name}={'pass' if self.passed else 'FAIL'}" + (f" ({self.detail})" if self.detail else "")


class GateError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _dotted(node: ast.AST) -> str:
    """'pytest.mark.req' for an Attribute chain, '' if it is not a plain dotted name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _numbers(value) -> set[float]:
    """Every number in an acceptance value (nested dicts/lists included), bools excluded."""
    if isinstance(value, bool):
        return set()
    if isinstance(value, (int, float)):
        return {float(value)}
    if isinstance(value, dict):
        return set().union(*(_numbers(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_numbers(v) for v in value)) if value else set()
    return set()


def _test_functions(tree: ast.AST) -> list[ast.FunctionDef]:
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


def gate_parse(code: str) -> ast.Module:
    try:
        return ast.parse(code)
    except SyntaxError as e:
        raise GateError(f"SyntaxError line {e.lineno}: {e.msg}") from None


def gate_imports(tree: ast.Module) -> None:
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in MODULE_IMPORTS:
                    problems.append(f"line {node.lineno}: 'import {alias.name}' not allowed "
                                    f"(allowed: import {', '.join(sorted(MODULE_IMPORTS))}; "
                                    f"rig modules via 'from rig.x import name')")
        elif isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            allowed = FROM_IMPORTS.get(mod)
            if allowed is None:
                problems.append(f"line {node.lineno}: 'from {mod} import ...' not allowed "
                                f"(allowed modules: {', '.join(sorted(FROM_IMPORTS))})")
                continue
            for alias in node.names:
                if alias.name not in allowed:
                    problems.append(f"line {node.lineno}: '{alias.name}' cannot be imported from "
                                    f"{mod} (allowed: {', '.join(sorted(allowed))})")
    if problems:
        raise GateError("; ".join(problems))


def gate_forbidden(tree: ast.Module) -> None:
    problems = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Name):
            if node.id in FORBIDDEN_CALLS or node.id in FORBIDDEN_NAMES:
                problems.append(f"line {line}: use of '{node.id}'")
            elif node.id.startswith("__") and node.id != "__name__":
                problems.append(f"line {line}: dunder name '{node.id}'")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                problems.append(f"line {line}: dunder attribute '.{node.attr}'")
            elif node.attr in FORBIDDEN_ATTRS:
                problems.append(f"line {line}: '.{node.attr}' (file/process I/O is not allowed; "
                                f"the run fixture already saves evidence)")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"line {line}: global/nonlocal statement")
    if problems:
        raise GateError("; ".join(dict.fromkeys(problems)))


def gate_req_marker(tree: ast.Module, req_id: str) -> None:
    tests = _test_functions(tree)
    if not tests:
        raise GateError("no test function (def test_...) found")
    problems = []
    for fn in tests:
        ids = []
        for dec in fn.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            name = _dotted(target)
            if not name.startswith(("pytest.mark.", "mark.")):
                continue
            mark = name.rsplit(".", 1)[-1]
            if mark not in ALLOWED_MARKS:
                problems.append(f"{fn.name}: @{name} not allowed (only req and parametrize)")
            if mark == "req":
                if not isinstance(dec, ast.Call) or not dec.args or dec.keywords:
                    problems.append(f"{fn.name}: @pytest.mark.req needs the id as its argument")
                    continue
                for arg in dec.args:
                    if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                        problems.append(f"{fn.name}: req id must be a string literal")
                    else:
                        ids.append(arg.value)
        if req_id not in ids:
            problems.append(f'{fn.name}: missing @pytest.mark.req("{req_id}")')
        others = [i for i in ids if i != req_id]
        if others:
            problems.append(f"{fn.name}: links other requirements {others}; link only {req_id}")
    if problems:
        raise GateError("; ".join(problems))


def gate_acceptance(tree: ast.Module, req_id: str, acceptance: dict) -> None:
    reads, other_reqs = 0, set()
    for node in ast.walk(tree):
        # spec["<id>"] ...
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "spec" and isinstance(node.slice, ast.Constant)):
            if node.slice.value != req_id:
                other_reqs.add(str(node.slice.value))
        # spec["<id>"]["acceptance"]
        if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                and node.slice.value == "acceptance" and isinstance(node.value, ast.Subscript)
                and isinstance(node.value.value, ast.Name) and node.value.value.id == "spec"
                and isinstance(node.value.slice, ast.Constant) and node.value.slice.value == req_id):
            reads += 1
    if other_reqs:
        raise GateError(f"reads other requirements {sorted(other_reqs)} from spec; use only {req_id}")
    if not reads:
        raise GateError(f'thresholds must be read from spec["{req_id}"]["acceptance"] '
                        f'(e.g. a = spec["{req_id}"]["acceptance"]), via the spec fixture')

    limits = _numbers(acceptance) - TRIVIAL_NUMBERS
    parents = _parents(tree)
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool) and abs(float(node.value)) in limits):
            continue
        p = parents.get(node)
        while p is not None:
            in_assert_call = (isinstance(p, ast.Call)
                              and _dotted(p.func).rsplit(".", 1)[-1] in ASSERTION_CALLS)
            if isinstance(p, (ast.Assert, ast.Compare)) or in_assert_call:
                hits.append(f"line {node.lineno}: literal {node.value!r}")
                break
            if isinstance(p, (ast.FunctionDef, ast.Module)):
                break
            p = parents.get(p)
    if hits:
        raise GateError(
            "hard-coded acceptance number(s) in a check: " + "; ".join(hits)
            + f' - read them from spec["{req_id}"]["acceptance"] (keys: {", ".join(acceptance)});'
              " for scenario timing use a named variable such as t0")


def gate_pytest(code: str, timeout: float = 300.0) -> GateResult:
    """Run the draft in a subprocess. Evidence goes to a temp dir, never to reports/."""
    STAGING.mkdir(parents=True, exist_ok=True)
    path = STAGING / f"test_candidate_{uuid.uuid4().hex[:8]}.py"
    path.write_text(code, encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="drafter_gate_") as tmp:
        out = Path(tmp) / "gate.json"
        env = {**os.environ, "SIL_REPORTS_DIR": str(Path(tmp) / "reports"),
               "DRAFTER_GATE_OUT": str(out), "MPLBACKEND": "Agg"}
        cmd = [sys.executable, "-m", "pytest", str(path), "-q", "--no-header",
               "-p", "no:cacheprovider", "-p", GATE_PLUGIN, "-o", "addopts="]
        try:
            proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return GateResult("pytest", False, f"timed out after {timeout:.0f} s")
        finally:
            path.unlink(missing_ok=True)
            try:
                STAGING.rmdir()
            except OSError:
                pass
        if not out.exists():
            tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
            return GateResult("pytest", False, f"pytest did not run (exit {proc.returncode}): {tail}")
        rec = json.loads(out.read_text())

    if rec["collect_errors"]:
        return GateResult("pytest", False, "collection error: " + rec["collect_errors"][0])
    if not rec["tests"]:
        return GateResult("pytest", False, "no tests collected")
    broken = [t for t in rec["tests"] if t["kind"] == "error"]
    if broken:
        t = broken[0]
        return GateResult("pytest", False, f"{t['nodeid']} errored in {t['when']}: {t['message']}")
    passed = sum(t["kind"] == "passed" for t in rec["tests"])
    failed = [t for t in rec["tests"] if t["kind"] == "failed"]
    detail = f"{passed} passed, {len(failed)} failed"
    if failed:
        detail += f" - first failure: {failed[0]['message']}"
    return GateResult("pytest", True, detail,
                      extra={"outcome": "failed" if failed else "passed",
                             "passed": passed, "failed": len(failed)})


def run_gates(code: str, req_id: str, acceptance: dict, *, pytest_timeout: float = 300.0,
              run_pytest: bool = True) -> list[GateResult]:
    """Runs the gates in order and stops at the first failure. Returns the results so far."""
    results: list[GateResult] = []

    def step(name, fn):
        try:
            fn()
        except GateError as e:
            results.append(GateResult(name, False, str(e)))
            return False
        results.append(GateResult(name, True))
        return True

    tree: list[ast.Module] = []
    if not step("parse", lambda: tree.append(gate_parse(code))):
        return results
    t = tree[0]
    for name, fn in (("imports", lambda: gate_imports(t)),
                     ("forbidden", lambda: gate_forbidden(t)),
                     ("req_marker", lambda: gate_req_marker(t, req_id)),
                     ("acceptance", lambda: gate_acceptance(t, req_id, acceptance))):
        if not step(name, fn):
            return results
    if run_pytest:
        results.append(gate_pytest(code, timeout=pytest_timeout))
    return results


def all_passed(results: list[GateResult], run_pytest: bool = True) -> bool:
    expected = GATE_ORDER if run_pytest else GATE_ORDER[:-1]
    return [r.name for r in results] == list(expected) and all(r.passed for r in results)
