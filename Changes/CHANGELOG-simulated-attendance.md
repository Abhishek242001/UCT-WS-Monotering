# Changelog: item 8 — Video Analysis gets its own attendance tables

## What changed

`app/models.py` — two new tables, `SimulatedAttendance` and
`SimulatedAttendanceSegment`, structurally identical to `EmployeeAttendance`/
`AttendanceSegment` but genuinely separate tables.

`app/routers/attendance.py` — the core recording logic
(`_record_detection_impl`, `_close_open_segment_and_open_new`) is now
parameterized over which pair of tables to write to. Two thin wrappers:
`record_detection_core` (real, unchanged behavior) and the new
`record_simulated_detection_core` (writes to the simulated tables). Two
new read endpoints mirroring the real ones: `/attendance/simulated/today`,
`/attendance/simulated/segments`.

`app/vision/stream_worker.py` — a confirmed MATCH now always writes
attendance somewhere: the real tables when `self.is_live`, the simulated
ones otherwise. This is a real behavior change from the previous round
of work, where `is_live=False` meant "write nothing" — now it means
"write to the separate simulated tables instead." Applies to both the
desk-identification path and the room-presence path (item 7) equally.

## Why a separate table, not a column on the existing tables — a design decision I revised mid-build, and why

My first plan was a `source` column (`LIVE`/`SIMULATED`) on the existing
`EmployeeAttendance`/`AttendanceSegment` tables. Checking the actual
schema before writing anything surfaced two real problems with that:

1. **This project has no migration framework at all** —
  `app/database.py` only ever calls `Base.metadata.create_all()`, which
  creates tables that don't exist yet but never alters an existing one.
  Your real, already-running database already has an
  `employee_attendance` table. Adding a column to it would silently do
  nothing to your actual file, and the first query referencing that
  column would fail with "no such column" — a real, deployment-breaking
  problem, not a cosmetic one.
2. `EmployeeAttendance` has a real `UniqueConstraint("org_id",
  "employee_id", "date")`. A shared table would need that constraint
  widened to include the new column — SQLite doesn't support altering a
  constraint on an existing table without recreating it, which is a
  materially riskier operation against data that may already be real.

A brand-new table sidesteps both problems entirely: `create_all()`
creates it cleanly regardless of what already exists, with its own
constraint, and zero risk to your existing real data. It's also a more
literal reading of "should not use the same database" than a
discriminator column would have been.

## Verification

New test file `tests/real-app-smoke/test_simulated_attendance.py` (3 tests):

- A Video Analysis run: confirmed to write **nothing** to the real table
  (still 404) and to write correctly to the simulated one.
- A live stream: confirmed to write to the real table, and confirmed to
  write **nothing** to the simulated one — the same separation checked
  in both directions, not just one.
- The segment open/close-on-location-change behavior (item 6) applies
  identically on the simulated path, since it's the same shared helper
  function, just parameterized.

Full regression, all passing:
- `tests/real-app-smoke`: **88/88** (85 previous + 3 new). This includes
  all of item 6 and item 7's existing tests, unmodified — they check the
  REAL endpoints specifically, which are unaffected by this change (a
  Video Analysis run still never touches them, just via a different
  mechanism than before: writing elsewhere instead of writing nothing).
- `backend/tests/test_video_pipeline.py`: **17/17** (unaffected).
- `tests/design-acceptance-suite`: **454 passed, 22 skipped** (unaffected).

## On ADAR calibration (item 14)

Not attempted, and won't be without real data. "ADAR-faithful
calibration" means fitting a real per-deployment SNR decay curve from
actual measured (distance, similarity) pairs on your camera. Inventing
those numbers would be fabricating experimental results and presenting
them as calibrated — directly against your own stated research standard.
What's genuinely buildable now, if wanted: a small script/endpoint that
captures real similarity readings at known, labeled distances (an
enrolled person standing at a few measured distances from the camera)
and fits a curve from whatever real samples you feed it — the tool, not
a fake result. Separate task from what shipped here; not started.

## Scope note

- `/attendance/simulated/report` (the report-view counterpart to
  `/attendance/report`) doesn't exist yet — only `today` and `segments`
  got simulated counterparts, matching what's most immediately useful
  for "a glimpse of the software." Easy to add if wanted.
- `close_stale_sessions` still only sweeps real attendance — a Video
  Analysis run is short and finishes on its own; there's no long-running
  "stale simulated session" in the same sense a real all-day live stream
  has.
