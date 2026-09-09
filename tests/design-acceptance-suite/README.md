# Test Suite — Employee Identity-Aware Workstation Monitoring & Attendance System

This is the acceptance-test checklist for the project, built ahead of the
full production implementation so every design decision documented in
`100-key-points.md` and the main project documentation has a corresponding,
runnable (or explicitly pending) test.

## Quick start

```bash
./setup.sh                 # install deps into .venv and run the full suite
./setup.sh --coverage      # same, plus an HTML coverage report
./setup.sh --no-run        # just provision the environment
```

Or manually:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -v
```

## Current status

```
476 tests collected
454 passed
 22 skipped (deliberately — see "Two kinds of tests" below)
 99% statement coverage on src/logic.py and src/mock_app.py
```

## Structure

```
src/
  logic.py       # Pure business-logic functions: distance/SNR-gate math,
                  # the attendance state machine, activity classification.
                  # No web framework, no database — fully unit-testable.
  mock_app.py    # In-memory FastAPI implementation of all 35 documented
                  # API endpoints, used for contract/integration tests.
  schema.sql     # SQLite mirror of the 17-table PostgreSQL schema, used to
                  # verify real constraint behaviour (UNIQUE, FK, CHECK)
                  # quickly without a live Postgres instance.

tests/
  conftest.py                              # shared fixtures
  test_logic_face_recognition.py           # distance/SNR-gate/matching math
  test_logic_attendance_state_machine.py   # sign-in/out state machine
  test_logic_activity_detection.py         # 5 activity classes + UNKNOWN
  test_api_streams.py                      # 8.1
  test_api_workstations.py                 # 8.2
  test_api_employees.py                    # 8.3
  test_api_dataset_calibration.py          # 8.4
  test_api_attendance.py                   # 8.5
  test_api_admin_auth.py                   # 8.6
  test_api_shifts_breaks_calendar.py       # 8.7
  test_api_reports_and_health.py           # 8.8 + 8.9
  test_database_constraints.py             # all 17 tables, real constraints
  test_attendance_scenarios.py             # one test per Section 6.2 scenario
  test_security.py                         # auth hardening + hardening checklist
  test_performance_optimization.py         # FPS budget math + hardware checklist
```

## Two kinds of tests — read this before "fixing" a skip

1. **Tests that run against the mock today and must keep passing.** These
   cover pure algorithms (`src/logic.py`) and the API contract (`src/mock_app.py`).
   They do **not** prove the real computer-vision pipeline works — they prove
   the *logic and API shape* the real pipeline must implement is correct and
   internally consistent.

2. **Tests marked `@pytest.mark.skip(reason=...)`.** These encode
   requirements from the documentation (bot/rate-limit hardening, liveness
   anti-spoofing, TensorRT/OpenVINO FPS targets, encryption-at-rest, RBAC
   beyond a single admin role) that the mock intentionally does not
   implement, because they require real hardware, a real deployment
   environment, or real trained models. **As each real component is built,
   move its test out of `mock_app.py`-only scope and un-skip it** — that is
   the intended workflow for keeping this suite meaningful all the way to
   the end of the project, not just at the start.

Do not delete a skipped test to make the suite "look" fully green — the
`reason=` string is the outstanding work item.

## Extending the suite

- New API endpoint → add it to `src/mock_app.py` first (in-memory, simple),
  then write its contract tests the same way the existing `test_api_*.py`
  files do.
- New business rule (e.g. a new attendance scenario) → add a pure function
  to `src/logic.py`, unit test it directly, and add one traceability test in
  `test_attendance_scenarios.py` if it corresponds to a documented scenario.
- New schema table/column → update `src/schema.sql` and add constraint
  tests to `test_database_constraints.py` mirroring the real PostgreSQL
  constraints documented in Section 7 of the project documentation.
- Real hardware becomes available (GPU for TensorRT, Intel CPU target for
  OpenVINO, a real trained model) → un-skip the relevant tests in
  `test_performance_optimization.py` and implement them for real against
  actual inference calls, replacing the arithmetic-only versions.

## Relationship to production

`src/mock_app.py` and `src/schema.sql` are **test scaffolding, not the
production implementation.** Production uses real PostgreSQL (+ `pgvector`
for face embeddings), the real YOLO/ADAR computer-vision pipeline, and a
real session/cookie/CORS configuration appropriate to the deployment
environment (see Section 10 of the project documentation). Keep this
distinction explicit in code review — a passing mock test is necessary but
not sufficient evidence that the real feature works.
