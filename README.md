# Motor controller SIL verification rig

![CI](https://github.com/tephlonee/sil_test_verification/actions/workflows/ci.yml/badge.svg)

Software-in-the-loop verification of an embedded motor speed controller with
thermal derating. The controller is portable C99; the rig simulates the motor,
the CAN bus and the faults around it, and proves each requirement with recorded
evidence.

```
spec/requirements.yaml ──► tests (pytest) ──► SIL rig ──► traces + plots ──► traceability matrix
                                                │
               C controller ◄── CAN (DBC) ──► plant model (motor + thermal)
```

## Layout

| Path | What it is |
|---|---|
| `controller/` | Software under test: C99 controller (`src/`), SIL platform layer (`platform/`) |
| `spec/requirements.yaml` | Requirements with quantified acceptance criteria |
| `calibration/` | `limits.json` (software-owned ranges and rules), `default.json` (values) |
| `can/motor.dbc` | CAN interface: messages, signals, scaling, alive counters, checksums |
| `rig/` | Test rig: plant, CAN nodes, lockstep harness, traces, assertion language |
| `tests/` | Plant physics, CAN interface and requirement-linked SIL tests |
| `tools/` | Calibration check, traceability report, release packaging |
| `demos/` | Open-loop and closed-loop demo plots |

## Run locally (WSL / Linux)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

make -C controller                                  # 1. build the controller
python -m tools.check_calibration                   # 2. validate the calibration
python -m pytest --junitxml=reports/tests.xml       # 3. run all tests (~20 s)
python -m tools.trace_report                        # 4. reports/traceability.html
python -m tools.package_release                     # 5. dist/motor-ctrl-<version>+<commit>.zip
```

## CI

Jenkins (`Jenkinsfile`) and GitHub Actions (`.github/workflows/ci.yml`) run the
same gates: build with `-Werror` → static analysis → calibration check → SIL
tests → traceability → package.

## AI test drafting

A local LLM (Ollama) can **draft** a test for a requirement. It never takes part
in a pass/fail decision. A draft only counts once a human accepts it into `tests/`,
and from then on it is an ordinary test that CI runs.

```bash
python -m tools.draft_test --req REQ-016            # one requirement
python -m tools.draft_test --all-untested           # every active/open requirement with no test
#   --model qwen3:8b  --url http://localhost:11434  --retries 1
#   (or set DRAFTER_MODEL / DRAFTER_OLLAMA_URL)
```

The prompt holds the requirement (text, acceptance, notes), the `rig.assertions`
API, the Scenario / fault-injection API, the trace signals, and one existing test
file from the same category. The answer then goes through deterministic gates,
in order (`tools/drafter_gates.py`):

| Gate | Rejects |
|---|---|
| parse | code that does not parse |
| imports | anything but pytest, numpy, `rig.assertions/harness/can_bus/trace` |
| forbidden | file / network / subprocess / os access, eval/exec, dunders, `Trace.save/plot` |
| req_marker | a test without `@pytest.mark.req("<id>")`, links to other IDs, skip/xfail |
| acceptance | thresholds not read from `spec["<id>"]["acceptance"]`; acceptance numbers hard-coded in a check |
| pytest | collection errors, fixture errors, non-assertion exceptions, timeouts (a pass or an assertion failure is fine) |

If a gate fails, the drafter retries once with the gate error added to the prompt,
then stops and reports. A draft that passes is saved to
`tests/drafts/test_<req>_draft.py` with a provenance header (model, UTC time,
prompt SHA-256, gate results). Each attempt is logged in `reports/drafter_log.jsonl`.
pytest ignores `tests/drafts/` (`--ignore` in `pytest.ini`), so drafts never run in CI.
The pytest gate writes its evidence to a temporary directory, never to `reports/`.

**Review workflow**

```bash
python -m tools.review_draft --list                 # drafts waiting for review
# read the draft: does it really test the requirement? correct stimulus, transport, limits?
python -m tools.review_draft --accept REQ-016       # -> tests/test_req_016.py, status open -> active
python -m tools.review_draft --accept REQ-016 --edited   # you changed it first (also auto-detected)
python -m tools.review_draft --reject REQ-016 --reason "tests over CAN; request saturates"
python -m tools.review_draft --stats                # acceptance rate from the log
python -m pytest && python -m tools.trace_report    # the accepted test now verifies the requirement
```

REQ-016 and REQ-017 were added to try out this workflow. They are `status: open`
with no test, so the traceability gate reports them as NOT TESTED without
failing CI. If you made them `active` before a test was accepted, the trace gate
would fail on them. Accepting a draft switches its requirement to `active`.

## Known gap

REQ-015 (stuck temperature sensor detection) is specified and tested but not
yet implemented; its test is marked as an expected failure and shows as
*KNOWN GAP* in the traceability matrix.
