# Changelog: real attendance recording wired into the live pipeline

## What changed

The attendance state machine, sign-in/out tracking, and per-location
segments already existed, fully built and tested — `record_detection`,
`EmployeeAttendance`, `AttendanceSegment` — but nothing in the live
video pipeline ever called any of it. This wires it up.

- `app/routers/attendance.py` — `record_detection`'s logic extracted
  into `record_detection_core(db, org_id, employee_id, cam_id, location,
  timestamp)`, a plain function callable directly with a db session. The
  HTTP endpoint is now a thin wrapper around it (unchanged behavior,
  confirmed by the full existing attendance test suite still passing
  unmodified).
- `app/vision/stream_worker.py` — on every confirmed MATCH (never
  MISMATCH or UNKNOWN — attendance tracks real presence, not every
  inconclusive glance), calls `record_detection_core` directly, with the
  workstation name as `location`. This is what "room" resolves to in
  practice: since you confirmed "in the room" means anywhere in the
  camera's frame, and a camera frame can hold multiple desks, each
  `AttendanceSegment` records time at a specific desk (`location` =
  workstation name); there's no separate broader "in the room but not at
  any desk" segment yet — see Scope Note below.

## A real, previously-existing gap fixed: AttendanceSegment never closed

Every call to `record_detection` — regardless of whether the location
had actually changed — opened a brand-new `AttendanceSegment` row with
only `start_time`. `end_time`/`duration_seconds` were never set
anywhere in the codebase. For someone checked once a minute at the same
desk, that meant a new open-ended micro-segment every minute, and no
segment's real duration was ever computable.

Fixed: a new segment only opens on a genuine location change (or the
first detection of the day). When the location does change, the
previously-open segment is closed with a real, computed
`duration_seconds`. `close_stale_sessions` (the away-timeout sweep) now
also closes an employee's final open segment when marking them
SIGNED_OUT, using their `last_seen_at` as the effective end time — so a
segment doesn't stay open forever just because the person left without
triggering a location-change detection.

## The most important part of this step: is_live gating

`app/routers/streams.py`'s `start_worker()` is the literal shared code
path both a live RTSP camera (`/streams/start`) and Video Analysis
(`/videos/{id}/analyze`) use — confirmed directly in the code before
touching anything, not assumed. Without a way to distinguish them,
wiring `record_detection_core` into the pipeline unconditionally would
mean **analyzing an uploaded demo video writes fabricated sign-in times
and fabricated desk-time into the exact same tables real attendance
reporting reads from.**

Added `StreamWorker.is_live` (default **False** — the safe failure mode:
forgetting to set it loses attendance data rather than corrupting it),
threaded explicitly through both real call sites: `/streams/start` →
`True`, `/videos/.../analyze` → `False`. `record_detection_core` is only
called when `self.is_live` is `True`.

This is the actual first piece of "Live vs. Video Analysis should use
separate data" (a backlog item from earlier in this round of work) —
not the full separate-database design yet, but the specific, concrete
part of it that would otherwise have caused real data corruption the
moment this step shipped.

## Verification

New test file `tests/real-app-smoke/test_attendance_wiring.py` (7 tests):

- Segment behavior: repeated detections at the same location stay one
  open segment (not fragmented); a location change closes the old
  segment with the correct computed duration and opens a new one;
  `close_stale_sessions` closes the final open segment too.
- **The critical pair**: the exact same recognition scenario (same
  photo, same enrollment) run twice — once with `is_live=True`
  (confirms a real attendance record gets written), once with
  `is_live=False` (confirms recognition still succeeds — checked
  directly against `_last_identity_by_track` — but NO attendance record
  is written). Proves the gate works on the actual failure mode that
  matters, not just that the flag exists.
- Both router wiring points checked directly against the actual
  constructed `StreamWorker` instance's `.is_live` value, not just that
  the endpoints return 201.

Full regression, all passing:
- `tests/real-app-smoke`: **79/79** (72 previous + 7 new).
- `backend/tests/test_video_pipeline.py`: **17/17** (unaffected).
- `tests/design-acceptance-suite`: **454 passed, 22 skipped**
  (unaffected — this suite's own separate mock `logic.py`/`mock_app.py`
  weren't touched).

## Scope note — what this does NOT yet do

- No separate "in room, not at any desk" segment type exists yet — only
  per-workstation segments. A person walking between desks or in a
  group discussion away from any ROI currently isn't captured by
  attendance segments at all (though they would be by the activity
  classifier from an earlier step, which is NOT yet cross-referenced
  with attendance data).
- Live and Video Analysis still share the SAME `EmployeeAttendance` /
  `AttendanceSegment` tables — `is_live=False` prevents Video Analysis
  from writing to them at all, which is the safe interim answer, but
  it's not the same as Video Analysis having its own separate, working
  simulated-attendance view. That's still the larger, not-yet-designed
  "separate database" backlog item.
- `WorkstationDailyAnalytics` / `WorkstationIdentityDailyAnalytics`
  rollup tables (mentioned in earlier planning) are still untouched —
  this step wires the per-detection/per-segment layer, not daily
  rollups or the dashboard/stats frontend that would read them.
