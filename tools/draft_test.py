"""
draft_test.py - Draft a requirement-linked SIL test with a local LLM (Ollama).

The LLM only *drafts*. Every draft must pass the deterministic gates in
tools/drafter_gates.py, is saved to tests/drafts/ (which pytest and CI ignore),
and only becomes part of the verification after a human accepts it with
tools/review_draft.py. No LLM output is ever part of a pass/fail decision.

Usage:
    python -m tools.draft_test --req REQ-016 [--model qwen3:8b] [--retries 1]
    python -m tools.draft_test --all-untested

Configuration (CLI flag > environment variable > default):
    --url    DRAFTER_OLLAMA_URL   http://localhost:11434
    --model  DRAFTER_MODEL        qwen3:8b
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from tools.drafter_gates import GATE_ORDER, GateResult, all_passed, run_gates

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec" / "requirements.yaml"
TESTS = ROOT / "tests"
DRAFTS = TESTS / "drafts"
LOG = ROOT / "reports" / "drafter_log.jsonl"
CAL = ROOT / "calibration" / "default.json"
DEFAULT_EXAMPLE = TESTS / "test_req_can.py"

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:8b"
DRAFT_BANNER = "DRAFT - requires human review before moving to tests/"

SYSTEM_PROMPT = (
    "You write pytest tests for a software-in-the-loop (SIL) verification rig of a motor "
    "controller ECU. You answer with exactly one complete Python file in a single "
    "```python code block and nothing else."
)


# --------------------------------------------------------------------------- #
# Spec and test discovery
# --------------------------------------------------------------------------- #


def load_spec(path: Path = SPEC) -> dict[str, dict]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {r["id"]: r for r in doc["requirements"]}


def req_slug(req_id: str) -> str:
    """REQ-016 -> req_016"""
    return re.sub(r"[^a-z0-9]+", "_", req_id.lower()).strip("_")


def draft_path(req_id: str, drafts_dir: Path = DRAFTS) -> Path:
    return drafts_dir / f"test_{req_slug(req_id)}_draft.py"


def linked_requirements(tests_dir: Path = TESTS) -> dict[str, list[Path]]:
    """Requirement id -> test files that link it with @pytest.mark.req (drafts excluded)."""
    links: dict[str, list[Path]] = {}
    for path in sorted(tests_dir.rglob("test_*.py")):
        if "drafts" in path.relative_to(tests_dir).parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "req"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        links.setdefault(arg.value, []).append(path)
    return links


def untested_requirements(spec: dict[str, dict], tests_dir: Path = TESTS,
                          drafts_dir: Path = DRAFTS) -> list[str]:
    """Active or open requirements with no linked test and no pending draft."""
    linked = linked_requirements(tests_dir)
    return [rid for rid, r in spec.items()
            if r.get("status", "active") in ("active", "open")
            and rid not in linked and not draft_path(rid, drafts_dir).exists()]


def pick_example(req: dict, spec: dict[str, dict], tests_dir: Path = TESTS) -> Path:
    """An existing test file covering the same category, else test_req_can.py."""
    for rid, files in linked_requirements(tests_dir).items():
        if rid in spec and spec[rid].get("category") == req.get("category"):
            return files[0]
    return DEFAULT_EXAMPLE


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def _source_segments(path: Path, names: set[str]) -> list[str]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out = []
    for node in tree.body:
        name = getattr(node, "name", None)
        if name is None and isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
            name = getattr(tgt, "id", None)
        if name in names:
            out.append(ast.get_source_segment(src, node))
    return out


def _api_summary(path: Path, names: set[str] | None = None) -> str:
    """Module docstring plus 'def signature: docstring' of each public function."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    parts = [ast.get_docstring(tree) or ""]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            if names and node.name not in names:
                continue
            sig = src.splitlines()[node.lineno - 1:node.body[0].lineno - 1]
            sig = " ".join(line.strip() for line in sig if not line.strip().startswith(('"""', "'''")))
            doc = ast.get_docstring(node)
            parts.append(sig + (f'\n    """{doc}"""' if doc else ""))
    return "\n\n".join(p for p in parts if p)


def _trace_methods() -> str:
    src = (ROOT / "rig" / "trace.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Trace")
    lines = []
    for fn in cls.body:
        if isinstance(fn, ast.FunctionDef) and fn.name in ("window", "at", "first_time", "mode_name"):
            doc = ast.get_docstring(fn)
            lines.append(src.splitlines()[fn.lineno - 1].strip() + (f"  # {doc}" if doc else ""))
    return "\n".join(lines)


def build_prompt(req: dict, example: Path) -> str:
    rig = ROOT / "rig"
    cal_values = json.loads(CAL.read_text(encoding="utf-8"))["values"]
    req_yaml = yaml.safe_dump({k: req[k] for k in ("id", "title", "text", "acceptance", "notes")
                               if k in req}, sort_keys=False, allow_unicode=True, width=100)
    scenario_api = "\n\n".join(
        _source_segments(rig / "harness.py", {"SensorFaultEvent", "Scenario"})
        + _source_segments(rig / "can_bus.py", {"FAULT_KINDS"})
        + [s.split("def active")[0].rstrip() for s in _source_segments(rig / "can_bus.py", {"BusFault"})]
        + _source_segments(rig / "plant.py", {"FAULT_MODES"}))
    rig_init = next(l.strip() for l in (rig / "harness.py").read_text(encoding="utf-8").splitlines()
                    if "def __init__(self, calibration" in l)
    harness_doc = ast.get_docstring(ast.parse((rig / "harness.py").read_text(encoding="utf-8")))
    signals = "\n\n".join(_source_segments(rig / "trace.py", {"MODE_NAMES", "MODES", "SIGNALS"}))
    rid = req["id"]

    return f"""Write a pytest file that verifies requirement {rid} on the SIL rig.

## Requirement
```yaml
{req_yaml.strip()}
```

## Rules (an automatic checker rejects the file if any is broken)
1. Return ONE complete Python file in a single ```python block. No explanations.
2. Import only from: pytest, numpy (as np), rig.assertions, rig.harness (Rig, Scenario,
   SensorFaultEvent, DT), rig.can_bus (BusFault), rig.trace (MODES, MODE_NAMES, SIGNALS).
3. No file, network, subprocess or os access; no open/eval/exec/getattr; do not call
   Trace.save/plot/load - the `run` fixture already stores the evidence.
4. Every test function is decorated with @pytest.mark.req("{rid}"). No skip/xfail marks.
5. Read every threshold from the requirement: a = spec["{rid}"]["acceptance"], then use
   a["<key>"]. Never write an acceptance number as a literal in an assert, a comparison
   or an assertion-function call. Scenario timing may use named variables (t0 = 2.0).
6. Check with the assertion functions below (they raise AssertionError with a clear
   message) and record each measured value with measured(label, value, unit, limit).
7. The test must run without errors: use only the fixtures, signals and APIs listed here.

## Fixtures (tests/conftest.py)
- spec: dict, requirement id -> requirement; spec["{rid}"]["acceptance"] is the dict above
- cal: dict of calibration values: {json.dumps(cal_values)}
- run(scenario, on=None) -> Trace: runs a Scenario on the default CAN rig, or on the Rig passed
  as `on`, and saves the evidence
- measured(label: str, value: float, unit: str = "", limit: str = "") records a measured value

## Rig
{harness_doc}

{rig_init}
  calibration: None (default calibration) or a dict of calibration values, e.g. {{**cal, "kp": 500.0}}
  transport: "can" (default) or "direct"

## Scenario and fault injection API (rig/harness.py, rig/can_bus.py, rig/plant.py)
```python
{scenario_api}
```

## Trace signals (rig/trace.py) - access as tr.<signal>, numpy arrays sampled every 10 ms
```python
{signals}
```
Trace methods:
```python
{_trace_methods()}
```

## Assertion functions (rig/assertions.py)
```python
{_api_summary(rig / "assertions.py")}
```

## Example of an existing test file in this project ({example.relative_to(ROOT).as_posix()})
```python
{example.read_text(encoding="utf-8").strip()}
```

Now write the test file for {rid}.
"""


def retry_prompt(base_prompt: str, previous_code: str, failure: GateResult) -> str:
    return (f"{base_prompt}\n\n## Your previous answer was rejected\n"
            f"```python\n{previous_code.strip()}\n```\n"
            f"The `{failure.name}` check failed: {failure.detail}\n"
            f"Fix this and return the complete corrected file.\n")


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #


def ollama_chat(prompt: str, model: str, url: str, timeout: float = 900.0,
                num_ctx: int = 16384) -> str:
    """One chat call to Ollama, streamed (a long silent request can be dropped by the
    WSL -> Windows localhost forwarding). Tests replace this function with a fake."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
        "stream": True,
        "options": {"temperature": 0.2, "num_ctx": num_ctx},
    }).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    parts = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise RuntimeError(f"Ollama error: {chunk['error']}")
                parts.append(chunk.get("message", {}).get("content", ""))
                if chunk.get("done"):
                    break
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ollama returned HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise RuntimeError(f"cannot reach Ollama at {url}: {e}") from None
    return "".join(parts)


def extract_code(text: str) -> str:
    """The Python file from an LLM answer: drops <think> blocks, takes the fenced code."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    blocks = re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```", text, flags=re.S)
    code = max(blocks, key=len) if blocks else text
    return code.strip() + "\n"


# --------------------------------------------------------------------------- #
# Drafting loop
# --------------------------------------------------------------------------- #


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _gate_summary(results: list[GateResult]) -> dict[str, bool | None]:
    done = {r.name: r.passed for r in results}
    return {name: done.get(name) for name in GATE_ORDER}


def append_log(entry: dict, log: Path = LOG) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def render_draft(code: str, req_id: str, model: str, stamp: str, prompt_sha: str,
                 attempts: int, results: list[GateResult]) -> str:
    gates = " ".join(str(r).split(" (")[0] for r in results)
    pytest_detail = next((r.detail for r in results if r.name == "pytest"), "")
    header = [
        f"# {DRAFT_BANNER}",
        f"# requirement:   {req_id}",
        f"# model:         {model}",
        f"# generated_utc: {stamp}",
        f"# prompt_sha256: {prompt_sha}",
        f"# attempts:      {attempts}",
        f"# gates:         {gates}",
        f"# pytest result: {pytest_detail}",
        "# The gates check that this file is a well-formed, safe, linked test. They do NOT",
        "# check that it verifies the requirement correctly - that is the reviewer's job.",
    ]
    return "\n".join(header) + "\n\n" + code


def draft(req_id: str, *, model: str, url: str, retries: int = 1,
          spec: dict[str, dict] | None = None, drafts_dir: Path = DRAFTS, log: Path = LOG,
          pytest_timeout: float = 300.0, llm_timeout: float = 900.0, num_ctx: int = 16384,
          force: bool = False, out=print) -> Path | None:
    """Draft, gate and save a test for one requirement. Returns the draft path or None."""
    spec = spec if spec is not None else load_spec()
    if req_id not in spec:
        raise SystemExit(f"{req_id} is not in {SPEC.relative_to(ROOT)}")
    req = spec[req_id]
    target = draft_path(req_id, drafts_dir)
    if target.exists() and not force:
        out(f"{req_id}: {target} already exists - review it first, or pass --force")
        return None

    example = pick_example(req, spec)
    base = build_prompt(req, example)
    prompt, code, results = base, "", []
    attempts = 0
    for attempt in range(1, retries + 2):
        attempts = attempt
        out(f"{req_id}: attempt {attempt} - asking {model} (example: {example.name}) ...")
        code = extract_code(ollama_chat(prompt, model, url, timeout=llm_timeout, num_ctx=num_ctx))
        results = run_gates(code, req_id, req.get("acceptance") or {}, pytest_timeout=pytest_timeout)
        for r in results:
            out(f"    {r}")
        if all_passed(results):
            break
        if attempt <= retries:
            prompt = retry_prompt(base, code, results[-1])

    stamp = _now()
    prompt_sha = _sha256(prompt)
    passed = all_passed(results)
    entry = {"req": req_id, "model": model, "gates_passed": passed,
             "gates": _gate_summary(results), "attempts": attempts,
             "prompt_sha256": prompt_sha, "timestamp": stamp}
    if not passed:
        failed = results[-1]
        out(f"{req_id}: gates failed after {attempts} attempt(s) - stopped. "
            f"Last failure: {failed.name}: {failed.detail}")
        append_log({**entry, "file": None, "review_status": "gate_failed",
                    "failure": f"{failed.name}: {failed.detail}"[:500]}, log)
        return None

    text = render_draft(code, req_id, model, stamp, prompt_sha, attempts, results)
    drafts_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    pytest_result = next(r for r in results if r.name == "pytest")
    rel = target.relative_to(ROOT).as_posix() if target.is_relative_to(ROOT) else str(target)
    append_log({**entry, "file": rel, "draft_sha256": _sha256(text),
                "pytest_outcome": pytest_result.extra.get("outcome"),
                "review_status": "pending"}, log)
    out(f"{req_id}: draft saved to {rel} (pytest: {pytest_result.detail})")
    out(f"    review it, then: python -m tools.review_draft --accept {req_id}   (or --reject)")
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--req", help="requirement id, e.g. REQ-016")
    who.add_argument("--all-untested", action="store_true",
                     help="draft for every active or open requirement with no linked test "
                          "and no pending draft")
    ap.add_argument("--model", default=os.environ.get("DRAFTER_MODEL", DEFAULT_MODEL))
    ap.add_argument("--url", default=os.environ.get("DRAFTER_OLLAMA_URL", DEFAULT_URL))
    ap.add_argument("--retries", type=int, default=1,
                    help="extra attempts after a gate failure, with the gate error in the prompt")
    ap.add_argument("--num-ctx", type=int, default=16384, help="Ollama context window (tokens)")
    ap.add_argument("--llm-timeout", type=float, default=900.0, help="seconds per LLM call")
    ap.add_argument("--pytest-timeout", type=float, default=300.0, help="seconds for the pytest gate")
    ap.add_argument("--force", action="store_true", help="overwrite an existing draft")
    args = ap.parse_args(argv)

    spec = load_spec()
    ids = [args.req] if args.req else untested_requirements(spec)
    if not ids:
        print("Every active or open requirement already has a linked test or a pending draft.")
        return 0
    ok = True
    for rid in ids:
        try:
            ok &= draft(rid, model=args.model, url=args.url, retries=args.retries, spec=spec,
                        pytest_timeout=args.pytest_timeout, llm_timeout=args.llm_timeout,
                        num_ctx=args.num_ctx, force=args.force) is not None
        except RuntimeError as e:
            print(f"{rid}: {e}")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
