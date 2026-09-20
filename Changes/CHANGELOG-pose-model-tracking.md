# Changelog: person detector swapped to YOLOv8n-pose + tracking

## What changed

`backend/app/vision/yolo_detector.py`'s detector switched from plain
`yolov8n.pt` (bounding boxes only) to `yolov8n-pose.pt` (bounding boxes +
17 COCO body keypoints), with persistent multi-object tracking
(Ultralytics' ByteTrack) added for continuing video sources.

This serves three consumers from one detection call per frame, instead of
running a separate detector alongside a separate pose model:

1. **Occupancy** — unchanged. Still just the bounding box; ROI-overlap
   logic (`boxes_overlap`) untouched.
2. **Persistent track IDs** — the data-layer foundation for a recognized
   employee's name staying attached to their box as they move, instead of
   needing a fresh identification call every frame. `PersonDetection` now
   carries `track_id` when produced via the new tracking path.
3. **Body keypoints** — feeds `app/logic.py`'s `classify_activity()`
   (SITTING/STANDING/WALKING/etc), which existed fully unit-tested but had
   no real keypoint source anywhere in the codebase until now.
   `PersonDetection` now carries a `keypoints` dict (17 COCO joint names →
   normalized x, y, confidence).

`docs/100-key-points.md` point 89 explicitly rejected a pose model as a
*replacement* for plain detection **for identification purposes**. That
rejection is honored, not overridden: the keypoints are never used for
face detection, alignment, or identification. Identification still crops
to the plain bounding box (`face_crop.py`, wired in the previous change)
and lets InsightFace run its own detector + alignment on that crop,
completely unchanged. Keypoints are only ever consumed by
`classify_activity()` — a separate, decoupled feature, per the same
docs' own design (points 91–92).

## New files / dependencies

- `backend/yolov8n-pose.pt` — bundled model weight (6.5MB), mirroring how
  `yolov8n.pt` is already checked in, so tests don't depend on network
  access to run.
- `backend/requirements.txt` — added `lap>=0.5.12`, a real dependency of
  Ultralytics' ByteTrack tracker that was previously not pinned (it
  silently auto-installs at runtime on first `.track()` call otherwise —
  not something to leave implicit).

## Architecture decision: one model instance per StreamWorker, not shared

Ultralytics' tracker keeps mutable state (assigned track IDs, motion
history) on the model object itself. The existing codebase's single
process-wide model singleton (`yolo_detector.get_model()`) is fine for
stateless single-image calls, but sharing it across `.track()` calls from
multiple concurrent or sequential video sources would either collide
track-ID spaces between unrelated streams, or hit real thread-safety
issues from multiple worker threads mutating the same tracker state at
once.

Verified directly (not assumed): a fresh model instance starts its own
independent track-ID sequence rather than continuing another instance's
state (see `test_separate_model_instances_do_not_share_track_id_state`).
`StreamWorker.run()` now calls `yolo_detector.new_model_instance()` for
its own exclusive use, once per stream, only after the source is
confirmed open (so a bad source URL doesn't pay a model-load cost for
nothing). The existing shared singleton (`get_model()`) is untouched and
still used by the stateless single-image path (`workstations.py`'s
`simulate_detection`).

## Verification

New test file `tests/real-app-smoke/test_pose_tracking.py`:

- `test_detect_people_still_works_unchanged_for_single_images` — the
  plain single-image path still returns the same bbox fields as before,
  plus keypoints now populated with valid normalized coordinates and
  confidences.
- `test_track_ids_persist_across_frames_on_the_same_model_instance` — the
  same people across two calls on one model instance keep the same track
  IDs (the actual point of the tracking addition).
- `test_separate_model_instances_do_not_share_track_id_state` — a fresh
  model instance starts its own ID sequence rather than continuing a
  prior instance's, verified directly (not assumed) — the concrete
  justification for per-StreamWorker model isolation above.
- `test_stream_worker_gets_its_own_private_model_instance` — checks the
  actual wiring: `StreamWorker.run()` calls `new_model_instance()`, not
  the shared singleton.

Full regression run, all passing:
- `tests/real-app-smoke`: **51/51 passed** (47 previous + 4 new).
- `backend/tests/test_video_pipeline.py`: **17/17 passed** (exercises
  `video_export.py`, which also calls `yolo_detector.detect_people()` —
  confirmed unaffected by the pose-model swap).
- `tests/design-acceptance-suite`: **454 passed, 22 skipped** (unaffected
  — this suite doesn't import `backend/app` at all).

Also required updating `tests/real-app-smoke/conftest.py`'s
`YOLO_MODEL_PATH` default from the bundled `yolov8n.pt` to the new bundled
`yolov8n-pose.pt` — without this, tests would have kept running against
the old detection-only weight and silently never exercised the pose path
at all (`r.keypoints` would just be `None` throughout, no error raised).

## Scope note — what this does NOT yet do

This is the detector/data layer only. Still separate, not-yet-built work:

- Nothing yet reads `track_id` to make an employee's name-label follow
  their box in the frontend, or persists it anywhere.
- Nothing yet calls `classify_activity()` with the new keypoints — the
  function exists and is tested, but has no caller yet.
- Recognition cadence (60s baseline / faster low-confidence retry) is
  unchanged from before this round of work.
