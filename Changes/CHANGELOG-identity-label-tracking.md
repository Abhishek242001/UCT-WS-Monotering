# Changelog: employee identity now labels the person's own box, follows their track

## What changed

Previously, EVERY detected person's box was always drawn with a generic
`"person {confidence}"` label, regardless of identity — the actual
recognition result was drawn separately, anchored to the workstation's
own static rectangle, never to the moving person. This was the direct
cause of the originally-reported "bounding box should show the employee
name, not just 'person'" gap.

- `app/vision/frame_annotate.py` — a person's own box now shows their
  employee ID + name once identified (`EMP-001 - Abhishek`), keyed by
  their persistent `track_id` so it's tied to the actual tracked person,
  not a fixed desk rectangle. Distinguishes three states with different
  colors: MATCH (green), MISMATCH (orange, explicitly flagged — wrong
  person at this desk), UNKNOWN (gray — identification was attempted and
  came back inconclusive) vs. the original green "person {confidence}"
  for a track never yet identified at all. These read as different
  situations to someone watching the feed and shouldn't look the same.
- `app/vision/stream_worker.py` / `app/vision/video_export.py` — both
  build and maintain a `track_id -> identity` map, updated every occupied
  frame (see the real bug below for why every frame, not only when
  `_identify()` runs).

## A real, previously-invisible bug this surfaced — and a third copy of the crop bug

**Bug 1 (caught by the integration test, not assumed away):** the first
version of this wiring only updated the track-identity map inside the
`if status_changed or heartbeat_due:` block, right when `_identify()`
itself ran. Directly reproducing the exact test video frame-by-frame
showed why that's wrong: Ultralytics' ByteTrack does not confirm a
track's ID (`box.id`) until that track's **second** seen frame — it's
`None` on the exact frame a person first appears. Since `_identify()`
fires precisely on that same first-appearance frame (the VACANT→ACTIVE
transition), the very first identification for a newly-arrived person
was silently never attached to any track — lost to a one-frame timing
gap, not a logic error as such. Fixed by decoupling "when do we compute
a fresh identity" (still rare/event-driven, unchanged) from "which
current track do we attach the known identity to" (now re-evaluated
every occupied frame, using whichever track_id is current) — so the
identity reaches the display the moment the ID confirms, one frame
later, instead of being lost.

**Bug 2 (found while making this fix, fixed alongside it, not left for
later):** `video_export.py` — the "download annotated video" feature —
had its own separate copy of `_identify()`, and it had never received
the crop-before-identify fix applied to `stream_worker.py` in an earlier
round of work. It was still calling `face_embedder.extract_embedding()`
on the full, uncropped frame — the same low-recognition-accuracy bug,
a third time, in a third place. Fixed the same way: crop to the
occupying person's box first (`face_crop.py`). Also switched this
module's detector call to `detect_and_track_people()` (it was using the
plain, non-tracking `detect_people()`), so the downloaded video gets the
same track-based identity labels as the live view — otherwise this
module's own stated design goal ("live and downloaded output match")
would have quietly broken the moment the live view changed.

## Verification

New test file `tests/real-app-smoke/test_identity_label_tracking.py`
(7 tests):

- Pure drawing-logic tests: no identity yet → generic label; MATCH →
  "id - name"; MATCH with no name available → bare id; MISMATCH →
  explicitly flagged; UNKNOWN → visually distinguished from "never
  checked"; a person with no track_id (e.g. from a non-tracking
  detection call) never accidentally matches a stale map entry.
- `test_stream_worker_records_identity_by_track_id` — the real
  `StreamWorker`, real enrollment, real identify() call: this is the
  test that caught Bug 1 above (failed first, with an honestly-reported
  empty dict, not a false pass) and now passes after the fix, confirming
  the actual object holds the correct employee_id/name/similarity keyed
  by the real track_id.

Full regression, all passing:
- `tests/real-app-smoke`: **66/66** (59 previous + 7 new).
- `backend/tests/test_video_pipeline.py`: **17/17** (exercises the
  now-also-fixed `video_export.py` path directly).
- `tests/design-acceptance-suite`: **454 passed, 22 skipped** (unaffected).

## Scope note

- The per-track dicts (`_last_identity_by_track`, `_last_position`,
  `_last_activity`, `_last_employee_name`) grow for as long as a stream
  runs, with no cleanup of stale track_ids for people who've long since
  left frame. Harmless for the short-lived runs tested so far (video
  analysis, short live sessions), but a real memory-growth concern for a
  camera stream running continuously for days/weeks. Not addressed in
  this step — flagging it now rather than after it's forgotten.
- Frontend JS hasn't needed any changes for this — the labels are drawn
  server-side, directly into the JPEG pushed over the existing "frame"
  WebSocket message, not as separate DOM overlay elements.
