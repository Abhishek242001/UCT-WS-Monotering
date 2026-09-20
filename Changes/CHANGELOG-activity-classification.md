# Changelog: classify_activity() wired to real keypoints

## What changed

`app/logic.py`'s `classify_activity()` existed fully unit-tested but had
no real keypoint source anywhere in the codebase — nothing called it.
This wires it up:

- `app/logic.py` — two new pure functions, decoupled from the vision
  layer (take/return plain dicts and tuples, same as `classify_activity()`
  itself): `combine_side_pair()` (picks the higher-confidence side of a
  COCO left/right joint pair) and `activity_inputs_from_coco_keypoints()`
  (converts the 17 COCO keypoints `yolo_detector.PersonDetection` carries
  into the joint-level `keypoint_confidence` dict + `torso_angle_deg`
  `classify_activity()` expects).
- `app/vision/stream_worker.py` — `_process_frame` now calls
  `_classify_and_publish_activity()` every frame, for **every tracked
  person in frame, not just workstation occupants** (per
  `docs/100-key-points.md` points 91–92: this is deliberately decoupled
  from occupancy/identity, and its real value is characterizing people
  who are *not* at any desk just as much as those who are). `is_moving`
  is derived from a tracked person's hip position (or bounding-box center
  when the hip isn't confidently located) shifting more than a threshold
  speed between frames. Publishing is debounced per track_id — only on a
  genuine change, mirroring the same debounce pattern already used for
  VACANT events, not once per frame.

## A real bug caught by the tests themselves, not shipped

Initial implementation had `combine_side_pair()` documented and consumed
as returning `(confidence, x, y)`, but it actually returned
`(x, y, confidence)` — matching the storage order `PersonDetection.keypoints`
itself already uses. The new unit tests caught this immediately (asserted
confidence values came back as x-coordinates instead). Fixed by making
every caller consistently use `(x, y, confidence)`, matching the
underlying data's own convention, rather than introducing a second,
different order. Documenting this because it's exactly the kind of bug
that "looks plausible" without being run — this is why every function was
tested before being wired into the live pipeline, not after.

## Known, deliberately unaddressed limitation

`activity_inputs_from_coco_keypoints()` does not compute a `seated_hint`.
No validated geometric heuristic for it exists yet — a guess based on
hip/knee angle would be unvalidated against real footage, and per point
93, knee/ankle keypoints are usually desk-occluded for a seated employee
anyway, leaving little to validate a heuristic against even if one were
written. Until this is added and validated against real footage, an
upright, stationary person will classify as STANDING rather than SITTING,
even when actually seated. This is flagged as a known gap, not silently
shipped as if it were handled.

## Verification

New test file `tests/real-app-smoke/test_activity_detection.py` (8 tests):

- Pure-logic unit tests for `combine_side_pair()` (higher-confidence side
  wins, one side missing, both absent) and
  `activity_inputs_from_coco_keypoints()` (vertical torso → ~0°; a
  leaning torso → correctly signed, symmetric nonzero angle; missing
  keypoints → 0° without crashing).
- `test_classify_activity_on_real_photo_is_unknown` — grounded in
  directly-observed real keypoint confidences on the bundled test photo
  (knee/ankle ~0.00–0.04, well below the 0.5 gate), confirming the full
  real pipeline correctly returns UNKNOWN — the documented expectation
  (point 93), not a surprising failure.
- `test_stream_worker_publishes_activity_events_debounced_per_track` — the
  real `StreamWorker`, through the actual `/videos/analyze` → WebSocket
  pipeline, publishes an "activity" message per tracked person (matching
  the UNKNOWN result above) exactly once per track_id despite the same
  photo repeating across 3 frames — proving the debounce works, not just
  that classification runs.

Full regression, all passing:
- `tests/real-app-smoke`: **59/59** (51 previous + 8 new).
- `backend/tests/test_video_pipeline.py`: **17/17**.
- `tests/design-acceptance-suite`: **454 passed, 22 skipped** (unaffected
  — imports its own separate copy of `logic.py`, not touched).

## Scope note — what this does NOT yet do

- Nothing yet persists activity classifications to a database table —
  they're published live over the WebSocket only, not written anywhere
  durable. Room-vs-desk time tracking using this signal is separate,
  still-pending work.
- Nothing yet displays activity on the frontend.
- The `seated_hint` gap above.
