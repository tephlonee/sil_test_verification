"""Unit tests for the LLM test drafter. No model needed: the Ollama call is always mocked."""

from __future__ import annotations

import io
import json
import textwrap
from pathlib import Path

import pytest

from tools import draft_test, review_draft
from tools.drafter_gates import (GateError, all_passed, gate_acceptance, gate_forbidden,
                                 gate_imports, gate_parse, gate_pytest, gate_req_marker, run_gates)

SPEC = draft_test.load_spec()
REQ = "REQ-014"                                # real requirement, so the pytest gate can run
ACC = SPEC[REQ]["acceptance"]                  # {stop_speed_rad_s: 10, stop_time_s: 2.0}


def src(code: str) -> str:
    return textwrap.dedent(code).lstrip()


GOOD = src('''
    import numpy as np
    import pytest

    from rig.assertions import eventually
    from rig.can_bus import BusFault
    from rig.harness import Scenario


    @pytest.mark.req("REQ-014")
    def test_request_loss_stops_motor(run, spec, measured):
        a = spec["REQ-014"]["acceptance"]
        t0 = 2.0
        tr = run(Scenario("loss", 4.5, speed_req=[(0.0, 300.0)],
                          bus_faults=[BusFault("VCU_Request", t0, 4.5, "drop")]))
        t_stop = eventually(tr, np.abs(tr.speed) < a["stop_speed_rad_s"], a["stop_time_s"],
                            "stopped", after=t0)
        measured("time to stop", t_stop, "s", f"<= {a['stop_time_s']}")
''')

# Runs, but the check fails on an AssertionError: a verdict, so the pytest gate accepts it
FAILS_ON_ASSERTION = src('''
    import pytest

    from rig.assertions import eventually
    from rig.harness import Scenario


    @pytest.mark.req("REQ-014")
    def test_unreachable(run, spec):
        a = spec["REQ-014"]["acceptance"]
        tr = run(Scenario("short", 0.5, speed_req=[(0.0, 100.0)]))
        eventually(tr, tr.speed > a["stop_speed_rad_s"] * 1000, a["stop_time_s"], "unreachable")
''')


def tree(code):
    return gate_parse(code)


# --------------------------------------------------------------------------- static gates


def test_good_sample_passes_static_gates():
    results = run_gates(GOOD, REQ, ACC, run_pytest=False)
    assert all_passed(results, run_pytest=False), results


def test_parse_gate_rejects_syntax_error():
    with pytest.raises(GateError, match="SyntaxError"):
        gate_parse("def test_x(:\n    pass\n")


@pytest.mark.parametrize("line", [
    "import os",
    "import subprocess",
    "import rig.harness",
    "from pathlib import Path",
    "from rig import calibration",
    "from rig.harness import subprocess",        # not a test-facing name of rig.harness
    "from rig.trace import json",
    "from numpy import load",
    "from . import conftest",
])
def test_import_gate_rejects(line):
    with pytest.raises(GateError):
        gate_imports(tree(line + "\n"))


def test_import_gate_accepts_allowed_imports():
    gate_imports(tree(src('''
        from __future__ import annotations
        import numpy as np
        import pytest
        from pytest import approx
        from rig.assertions import always, eventually, holds_from, never, overshoot, settles
        from rig.harness import Rig, Scenario, SensorFaultEvent
        from rig.can_bus import BusFault
        from rig.trace import MODES, SIGNALS
    ''')))


@pytest.mark.parametrize("code", [
    "open('x').read()",
    "eval('1')",
    "exec('x = 1')",
    "__import__('os')",
    "getattr(tr, 'save')('x')",
    "tr.save('evidence')",
    "tr.plot('x.png')",
    "np.save('x.npy', tr.speed)",
    "np.loadtxt('x.csv')",
    "Path('x').write_text('y')",
    "os.system('ls')",
    "().__class__.__bases__",
    "__builtins__",
])
def test_forbidden_gate_rejects(code):
    with pytest.raises(GateError):
        gate_forbidden(tree(code + "\n"))


@pytest.mark.parametrize("code,match", [
    ("def helper():\n    pass\n", "no test function"),
    ("def test_x():\n    pass\n", 'missing @pytest.mark.req\\("REQ-014"\\)'),
    ('@pytest.mark.req("REQ-001")\ndef test_x():\n    pass\n', "missing"),
    ('@pytest.mark.req("REQ-014", "REQ-001")\ndef test_x():\n    pass\n', "other requirements"),
    ('@pytest.mark.req("REQ-014")\n@pytest.mark.xfail\ndef test_x():\n    pass\n', "xfail"),
    ('@pytest.mark.req("REQ-014")\n@pytest.mark.skip(reason="x")\ndef test_x():\n    pass\n', "skip"),
    ('@pytest.mark.req(REQ)\ndef test_x():\n    pass\n', "string literal"),
])
def test_req_marker_gate_rejects(code, match):
    with pytest.raises(GateError, match=match):
        gate_req_marker(tree(code), REQ)


def test_req_marker_gate_allows_parametrize():
    gate_req_marker(tree(src('''
        @pytest.mark.req("REQ-014")
        @pytest.mark.parametrize("kind", ["drop", "corrupt"])
        def test_x(kind):
            pass
    ''')), REQ)


@pytest.mark.parametrize("code,match", [
    # thresholds not read from the spec at all
    ("def test_x(spec):\n    assert 1\n", "must be read from"),
    # reads another requirement
    ('def test_x(spec):\n    a = spec["REQ-014"]["acceptance"]\n    b = spec["REQ-001"]\n',
     "other requirements"),
    # acceptance number hard-coded in an assertion call
    ('def test_x(spec, tr):\n    a = spec["REQ-014"]["acceptance"]\n'
     '    eventually(tr, tr.speed < a["stop_speed_rad_s"], 2.0, "x")\n', "literal 2.0"),
    # ... in a comparison
    ('def test_x(spec, tr):\n    a = spec["REQ-014"]["acceptance"]\n'
     '    always(tr, tr.speed < 10, "x")\n', "literal 10"),
    # ... in a bare assert
    ('def test_x(spec, t):\n    a = spec["REQ-014"]["acceptance"]\n    assert t <= 2.0\n', "literal 2.0"),
    # ... negated
    ('def test_x(spec, tr):\n    a = spec["REQ-014"]["acceptance"]\n'
     '    always(tr, tr.speed > -10, "x")\n', "literal 10"),
])
def test_acceptance_gate_rejects(code, match):
    with pytest.raises(GateError, match=match):
        gate_acceptance(tree(code), REQ, ACC)


def test_acceptance_gate_allows_literals_in_scenario_setup():
    # 2.0 equals stop_time_s, but here it is stimulus timing, not a threshold
    gate_acceptance(tree(src('''
        def test_x(spec, run):
            a = spec["REQ-014"]["acceptance"]
            t0 = 2.0
            tr = run(Scenario("s", 4.0, speed_req=[(0.0, 300.0), (2.0, 10.0)]))
            always(tr, tr.speed >= 0, "x")
    ''')), REQ, ACC)


def test_gates_stop_at_first_failure():
    results = run_gates("import os\n" + GOOD, REQ, ACC)
    assert [r.name for r in results] == ["parse", "imports"]
    assert not results[-1].passed and not all_passed(results)


# --------------------------------------------------------------------------- pytest gate


def test_pytest_gate_passing_test():
    r = gate_pytest(GOOD)
    assert r.passed and r.extra["outcome"] == "passed", r.detail


def test_pytest_gate_allows_assertion_failure():
    r = gate_pytest(FAILS_ON_ASSERTION)
    assert r.passed and r.extra["outcome"] == "failed", r.detail
    assert "AssertionError" in r.detail


@pytest.mark.parametrize("code,match", [
    # non-assertion exception in the test body: a broken test, not a verdict
    (FAILS_ON_ASSERTION.replace('tr.speed > a["stop_speed_rad_s"] * 1000', "tr.no_such_signal > 0"),
     "AttributeError"),
    # unknown fixture: setup error
    (GOOD.replace("(run, spec, measured)", "(run, spec, measured, no_such_fixture)"), "setup"),
    # import-time crash: collection error
    ("x = 1 / 0\n" + GOOD, "collection error"),
    # nothing to run
    ("import pytest\n", "no tests collected"),
])
def test_pytest_gate_rejects_broken_tests(code, match):
    r = gate_pytest(code)
    assert not r.passed
    assert match in r.detail


def test_pytest_gate_never_touches_real_reports():
    real = draft_test.ROOT / "reports" / "req_results.json"
    before = real.stat().st_mtime_ns if real.exists() else None
    gate_pytest(GOOD)
    assert (real.stat().st_mtime_ns if real.exists() else None) == before


def test_pytest_ignores_drafts(request):
    assert not [i for i in request.session.items if "drafts" in Path(str(i.fspath)).parts]


# --------------------------------------------------------------------------- drafting loop (mocked LLM)


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt, model, url, **kw):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def fenced(code: str) -> str:
    return f"<think>planning...</think>\nHere is the file:\n```python\n{code}```\n"


def test_extract_code_takes_fenced_block_and_drops_thinking():
    assert draft_test.extract_code(fenced("x = 1\n")) == "x = 1\n"
    assert draft_test.extract_code("x = 2") == "x = 2\n"


def test_prompt_contains_requirement_and_apis():
    req = SPEC["REQ-016"]
    p = draft_test.build_prompt(req, draft_test.pick_example(req, SPEC))
    for needle in (req["text"].split()[0], "invalid_request_rad_s", "speed_max_valid",
                   'spec["REQ-016"]["acceptance"]', "def settles(", "holds_from",
                   "class Scenario", "class SensorFaultEvent", "class BusFault", "FAULT_MODES",
                   '"torque_limit"', "MODES", "test_req_can.py", "@pytest.mark.req"):
        assert needle in p, needle


def test_example_follows_requirement_category():
    assert draft_test.pick_example(SPEC["REQ-017"], SPEC).name == "test_req_calibration.py"


def test_draft_retries_with_gate_error_then_saves(tmp_path, monkeypatch):
    llm = FakeLLM(fenced("import os\n" + GOOD), fenced(GOOD))
    monkeypatch.setattr(draft_test, "ollama_chat", llm)
    log = tmp_path / "log.jsonl"
    path = draft_test.draft(REQ, model="fake:1b", url="http://x", retries=1, spec=SPEC,
                            drafts_dir=tmp_path / "drafts", log=log, out=lambda *a: None)

    assert path == tmp_path / "drafts" / "test_req_014_draft.py"
    assert len(llm.prompts) == 2
    assert "previous answer was rejected" in llm.prompts[1] and "import os" in llm.prompts[1]

    text = path.read_text()
    assert text.startswith("# DRAFT - requires human review before moving to tests/")
    for needle in ("requirement:   REQ-014", "model:         fake:1b", "generated_utc:",
                   "prompt_sha256:", "attempts:      2", "pytest=pass", "1 passed"):
        assert needle in text, needle
    assert GOOD in text

    (entry,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert entry["req"] == REQ and entry["model"] == "fake:1b" and entry["gates_passed"] is True
    assert entry["review_status"] == "pending" and entry["file"].endswith("test_req_014_draft.py")
    assert all(entry["gates"].values()) and entry["timestamp"]


def test_draft_stops_after_retries_and_reports(tmp_path, monkeypatch):
    llm = FakeLLM(fenced("import os\n"), fenced("import subprocess\n"))
    monkeypatch.setattr(draft_test, "ollama_chat", llm)
    log = tmp_path / "log.jsonl"
    msgs = []
    path = draft_test.draft(REQ, model="fake:1b", url="http://x", retries=1, spec=SPEC,
                            drafts_dir=tmp_path / "drafts", log=log, out=msgs.append)
    assert path is None and len(llm.prompts) == 2
    assert not (tmp_path / "drafts").exists()
    assert any("gates failed after 2 attempt(s)" in m for m in msgs)
    (entry,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert entry["gates_passed"] is False and entry["review_status"] == "gate_failed"
    assert entry["gates"]["imports"] is False and entry["gates"]["pytest"] is None


def test_ollama_chat_request(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen.update(url=req.full_url, body=json.loads(req.data), timeout=timeout)
        chunks = [{"message": {"content": "```python\n"}, "done": False},
                  {"message": {"content": "x = 1\n```"}, "done": False},
                  {"message": {"content": ""}, "done": True}]
        return Resp("".join(json.dumps(c) + "\n" for c in chunks).encode())

    monkeypatch.setattr(draft_test.urllib.request, "urlopen", fake_urlopen)
    out = draft_test.ollama_chat("PROMPT", "qwen3:8b", "http://host:11434/", timeout=5)
    assert out == "```python\nx = 1\n```"
    assert seen["url"] == "http://host:11434/api/chat"
    assert seen["body"]["model"] == "qwen3:8b" and seen["body"]["stream"] is True
    assert seen["body"]["messages"][-1]["content"] == "PROMPT"


def test_untested_requirements(tmp_path):
    tests_dir = tmp_path / "tests"
    (tests_dir / "drafts").mkdir(parents=True)
    (tests_dir / "test_a.py").write_text('@pytest.mark.req("REQ-001")\ndef test_a(): pass\n')
    (tests_dir / "drafts" / "test_req_002_draft.py").write_text("")
    spec = {f"REQ-00{i}": {"id": f"REQ-00{i}", "status": s}
            for i, s in enumerate(["active", "active", "active", "open", "retired"], start=1)}
    assert draft_test.untested_requirements(spec, tests_dir, tests_dir / "drafts") == ["REQ-003", "REQ-004"]


# --------------------------------------------------------------------------- review


@pytest.fixture
def review_env(tmp_path):
    drafts, tests_dir, log, spec = (tmp_path / "drafts", tmp_path / "tests",
                                    tmp_path / "log.jsonl", tmp_path / "requirements.yaml")
    drafts.mkdir()
    tests_dir.mkdir()
    spec.write_text("requirements:\n  - id: REQ-020\n    status: open\n"
                    "  - id: REQ-021\n    status: open  # keep\n")
    text = "# DRAFT - requires human review before moving to tests/\n# requirement:   REQ-020\nx = 1\n"
    (drafts / "test_req_020_draft.py").write_text(text)
    review_draft.write_log([{"req": "REQ-020", "review_status": "pending",
                             "draft_sha256": review_draft.hashlib.sha256(text.encode()).hexdigest()}], log)
    return dict(drafts_dir=drafts, tests_dir=tests_dir, log=log, spec_path=spec)


def test_accept_moves_draft_logs_and_activates(review_env):
    dst = review_draft.accept("REQ-020", reviewer="rev", out=lambda *a: None, **review_env)
    assert dst == review_env["tests_dir"] / "test_req_020.py"
    assert not (review_env["drafts_dir"] / "test_req_020_draft.py").exists()
    assert dst.read_text().startswith("# Reviewed: accepted by rev on ")
    (entry,) = review_draft.read_log(review_env["log"])
    assert entry["review_status"] == "accepted" and entry["reviewer"] == "rev"
    spec = review_env["spec_path"].read_text()
    assert "id: REQ-020\n    status: active\n" in spec
    assert "id: REQ-021\n    status: open" in spec                 # other requirements untouched


def test_accept_detects_edits(review_env):
    p = review_env["drafts_dir"] / "test_req_020_draft.py"
    p.write_text(p.read_text() + "y = 2\n")
    review_draft.accept("REQ-020", reviewer="rev", out=lambda *a: None, **review_env)
    assert review_draft.read_log(review_env["log"])[0]["review_status"] == "edited"


def test_accept_edited_flag(review_env):
    review_draft.accept("REQ-020", edited=True, reviewer="rev", out=lambda *a: None, **review_env)
    assert review_draft.read_log(review_env["log"])[0]["review_status"] == "edited"


def test_reject_deletes_and_logs(review_env):
    review_draft.reject("REQ-020", reason="wrong transport", reviewer="rev", out=lambda *a: None,
                        drafts_dir=review_env["drafts_dir"], log=review_env["log"])
    assert not (review_env["drafts_dir"] / "test_req_020_draft.py").exists()
    (entry,) = review_draft.read_log(review_env["log"])
    assert entry["review_status"] == "rejected" and entry["reason"] == "wrong transport"
    assert "status: open" in review_env["spec_path"].read_text()


def test_acceptance_rate():
    entries = [{"review_status": s} for s in
               ("accepted", "edited", "rejected", "rejected", "pending", "gate_failed")]
    rate, counts = review_draft.acceptance_rate(entries)
    assert rate == 0.5 and counts["pending"] == 1 and counts["gate_failed"] == 1
    assert review_draft.acceptance_rate([])[0] is None
