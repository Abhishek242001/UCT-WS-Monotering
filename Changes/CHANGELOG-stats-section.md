# Changelog: item 10 — the stats section

## What changed

Backend: new `GET /attendance/live_now?org_id=X` endpoint —
`app/routers/attendance.py`. Every existing attendance endpoint needed
an `employee_id` up front; there was no way to see an org's whole
current presence at a glance. Returns every employee whose today record
is still `PRESENT` or `ON_BREAK` (genuinely here now — not
`SIGNED_OUT`, not simply never seen today), with name, status, and last
seen location. Always reads real attendance, never `SimulatedAttendance`
— "who's here" is inherently a live-monitoring question.

Frontend: the Attendance tab, previously a bare-bones two-tool demo
harness, now has three real sections above those original tools (kept,
since they're still useful for testing without a live pipeline):

1. **Who's here now** — a tile grid from the new endpoint.
2. **Employee stats** — search by employee ID + date range, using the
   existing `/attendance/report` endpoint (no backend change needed
   there): a per-day table plus an average-hours-per-day figure over the
   range, computed client-side.
3. **Daily timeline** — pick one date (or click a row above), fetch
   `/attendance/segments`, and render a horizontal bar: desk time
   (indigo) vs. time in the room but not at a desk (amber, the `ROOM`
   sentinel from an earlier step) vs. away/untracked (shown as empty
   space, not a rendered block). Also computes a desk-utilization
   percentage.

## Two things worth being precise about, not glossing over

**"Utilization" is labeled exactly for what it measures.** It's desk
time as a share of *tracked* time (desk + room), not as a share of the
scheduled workday — the latter would need shift-schedule data
cross-referenced in, which this view doesn't attempt. Labeled "Desk
share of tracked time" specifically so it can't be misread as a claim
it doesn't make.

**Weekly/period average excludes in-progress days.** An employee still
`PRESENT` today has no `net_present_seconds` yet (it's only computed on
sign-out or the away-timeout sweep) — averaging in a zero or partial
value for that day would understate every completed day's real hours.
The average is computed only over days with a completed record, and the
count shown makes that explicit ("Average over N completed day(s)").

## A real bug caught by actually executing the code, not just reading it

First version of the timeline used `new Date()` (real wall-clock time)
as the assumed end of any still-open segment, unconditionally. Running
it against a fixture for a **past** date immediately showed the problem:
a past date's open segment (itself a data anomaly, since
`close_stale_sessions` should normally close it) was getting measured
against *today's* clock time-of-day, which is meaningless and can even
go negative if today's current time-of-day happens to be earlier than
the segment's recorded start. Fixed: "assume still running until now" is
only used when the viewed date **is** today; for any other date, an open
segment falls back to the latest known end time among that day's other
segments — an honest "no better information available" answer instead
of a fabricated one. Verified in both directions after the fix, with
realistic fixture timing (checked the sandbox's actual current UTC time
before writing the "today" test case, rather than guessing).

## Verification

Backend: new test file `tests/real-app-smoke/test_attendance_live_now.py`
(4 tests) — present employee appears, signed-out employee doesn't,
never-seen-today employee doesn't, and org-scoping holds in both
directions.

Frontend (this repo has no JS test framework — confirmed, not assumed;
same approach as the previous frontend step):
- `node --check js/tabs/attendance.js` — syntax valid.
- ID-collision check on every new element.
- HTML well-formedness check on `dashboard.html`.
- **Executed the real functions** against realistic fixtures: `loadLiveNow`
  with two employees; `loadEmployeeStats` with a mix of completed and
  in-progress days (confirmed the average genuinely excludes the
  in-progress one); `loadDailyTimeline` with a desk segment, a room
  segment, and an open final segment, for both a past date and today —
  this is what caught the bug above.

Full regression, all passing:
- `tests/real-app-smoke`: **98/98** (94 previous + 4 new).
- `backend/tests/test_video_pipeline.py`: **17/17** (unaffected).
- `tests/design-acceptance-suite`: **454 passed, 22 skipped** (unaffected).

## Scope note

- No auto-refresh on "who's here now" — manual button, matching the
  desk status grid's own established pattern from the previous frontend
  step.
- The timeline doesn't yet cross-reference the activity classifier
  (SITTING/STANDING/WALKING) — "room time" here is exactly what
  `AttendanceSegment` already records (the `ROOM` sentinel), not a
  finer-grained activity breakdown.
