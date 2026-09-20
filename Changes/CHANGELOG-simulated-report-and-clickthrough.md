# Changelog: closing the two flagged follow-ups from item 11

Both of these were explicitly flagged as scoped-out follow-ups in item
11's changelog, not silently left — closing them out now.

## What changed

**`/attendance/simulated/report`** (`app/routers/attendance.py`) — the
last piece of simulated/real endpoint parity. `today` and `segments`
already had simulated counterparts from item 8; `report` (the
date-range, per-day view) didn't. Same query shape as the real one,
against `SimulatedAttendance` instead.

**The actual click-through** — previously, a completed Video Analysis
run's history row just told you the org/date to go look up manually.
Now it's a real link:

- `frontend/dashboard.html` / `js/tabs/attendance.js` — the "Employee
  stats" card gets a **Real / Simulated** data-source toggle. Both
  `loadEmployeeStats()` and `loadDailyTimeline()` now call the real or
  simulated endpoint pair based on it (`/attendance/report` vs.
  `/attendance/simulated/report`, `/attendance/segments` vs.
  `/attendance/simulated/segments`) — same UI, same code paths, just
  pointed at a different pair of tables.
- `js/tabs/videoanalysis.js` — each **completed** run in the history
  table gets a "View" button (deliberately not shown for a still-running
  run, which has no data yet). Clicking it switches to the Attendance
  tab, sets the toggle to Simulated, and pre-fills the date range and
  timeline date to that run's own date — genuinely one click from "here's
  a past run" to "here's what it found," not just a pointer to go build
  the URL yourself. Employee ID is deliberately left blank for the admin
  to fill in, since a run isn't tied to one specific person — it may
  have identified several.

## Verification

Backend: new test file `tests/real-app-smoke/test_simulated_report.py`
(2 tests) — confirms simulated and real data never leak into each
other's report (a simulated detection's date doesn't show up in the real
report and vice versa, checked in both directions), and that the
date-range filter works correctly against the simulated table.

Frontend (no JS test framework in this repo, same approach as every
frontend step so far): syntax checks, an ID-collision check for the new
toggle, HTML well-formedness, and — the real verification — **executing
the actual functions** against fixtures:
- Confirmed `loadEmployeeStats()` and `loadDailyTimeline()` call the
  correct endpoint (checked the literal request URL) for both toggle
  positions.
- Confirmed the history table's View button appears only for a
  `completed` run, not a `running` one.
- Confirmed `viewRunInAttendance()` actually sets every field it's
  supposed to (tab, toggle, both date fields, timeline date) and shows
  the right guidance message — not assumed from reading the function.

Full regression, all passing:
- `tests/real-app-smoke`: **104/104** (102 previous + 2 new).
- `backend/tests/test_video_pipeline.py`: **17/17** (unaffected).
- `tests/design-acceptance-suite`: **454 passed, 22 skipped** (unaffected).

## What's still genuinely open

- The identity-hysteresis refinement from item 13 — flagged as "build
  only if real footage still shows flicker after the occupancy fix,"
  and no real-footage feedback has come in yet, so it stays parked
  rather than built speculatively.
- ADAR calibration itself — still blocked on real measured
  (distance, similarity) data from your actual camera, which only you
  can collect.
- The whole chain (detection through attendance through this UI) is
  still verified only against synthetic video and direct DB checks —
  testing it end-to-end on real footage remains the natural next step
  before treating any of this as production-ready.
