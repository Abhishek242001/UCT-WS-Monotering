# Changelog: item 11 — Video Analysis history

## What was already there vs. what was actually missing

Checked before building anything: the "real-time" half of item 11 was
already fully built in an earlier round of work — live WebSocket frame
preview, progress bar, and event log, all already wired into the Video
Analysis tab. What was genuinely missing was the "historical view of
past runs" half: **no record of past analysis runs existed at all**, in
any form. Uploaded video *files* were tracked (`GET /videos`), but
nothing recorded that a video had ever been analyzed, when, with what
org/camera, or how it went — that information lived only in the
ephemeral in-memory `stream_id`, lost the moment the server restarted or
the WebSocket closed.

## What changed

- `app/models.py` — new `VideoAnalysisRun` table: org_id, cam_id,
  video_id, workstation_name, stream_id (unique), started_at,
  completed_at (null while running), frames_processed (null until
  completed).
- `app/routers/videos.py` — `/videos/{id}/analyze` now creates a
  `VideoAnalysisRun` row alongside starting the worker. New
  `GET /videos/analysis_runs?org_id=X` lists them, newest first, with
  the video's filename looked up from the existing in-memory registry
  (falls back to `"(deleted)"` for a stale row rather than failing the
  whole list).
- `app/vision/stream_worker.py` — closes out its own run's row
  (`completed_at`, `frames_processed`) when the stream finishes, matched
  by `stream_id`. A live camera stream's `stream_id` simply never
  matches any row (only `/analyze` creates one), so this is a harmless
  no-op there — confirmed directly by a test, not assumed.
- Frontend: a new "Past analysis runs" table in the Video Analysis tab —
  video, org/cam, started time, status (running/completed), frame count.
  Refreshes when the tab is opened, when a new run starts, and when a
  run completes (same pattern already used for `loadIdentityStatus()`
  elsewhere in this app).

## A real bug caught before it shipped, not after

First draft of the frontend row-rendering referenced `r.org_id` — a
field the backend response doesn't actually return per-row (every run in
the list is already scoped to the single `org_id` the request was made
with, so it's redundant there). Caught this by re-reading what I'd just
written against the actual backend response shape before running the
verification step, not after a test failure — fixed to use the
response's own top-level `org_id` instead.

## Verification

Backend: new test file `tests/real-app-smoke/test_video_analysis_history.py`
(4 tests):
- Starting an analysis creates a run record with `status: "running"`,
  correct filename, `completed_at`/`frames_processed` still null.
- **Waits for the real background thread to actually finish**
  (`worker.join()`, not just checking the HTTP response), then confirms
  the row flipped to `"completed"` with a real frame count — not
  assumed from the code, observed after real execution.
- Org-scoping holds in both directions.
- A live stream (`/streams/start`) does NOT create a run record —
  confirming the harmless-no-op behavior directly, not by inspection.

Frontend (no JS test framework in this repo — confirmed, not assumed;
same approach as the previous two frontend steps): syntax check,
ID-collision check, HTML well-formedness check, and the real function
executed against fixtures covering a completed run, a running run, and
the empty-history case.

Full regression, all passing:
- `tests/real-app-smoke`: **102/102** (98 previous + 4 new).
- `backend/tests/test_video_pipeline.py`: **17/17** (unaffected).
- `tests/design-acceptance-suite`: **454 passed, 22 skipped** (unaffected).

## Scope note

- Clicking a past run does not (yet) auto-navigate to the Attendance
  tab's simulated data for that specific run — the hint text tells the
  admin the org/date needed to look it up via
  `/attendance/simulated/segments`, but doesn't wire the click-through
  itself. That would mean either rebuilding item 10's stats UI a second
  time for simulated data, or adding a "simulated" toggle to the
  existing one — a reasonable, clearly-scoped follow-up, not silently
  left half-done.
- `/attendance/simulated/report` (mentioned as a gap in item 8's
  changelog) still doesn't exist — browsing a historical run's
  per-employee day-by-day simulated report isn't fully supported yet,
  only `today`-shaped and `segments` lookups are.
