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

## Known gap

REQ-015 (stuck temperature sensor detection) is specified and tested but not
yet implemented; its test is marked as an expected failure and shows as
*KNOWN GAP* in the traceability matrix.
