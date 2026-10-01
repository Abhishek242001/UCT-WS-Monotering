# Changelog — RTSP-camera employee enrollment

Session: 2026-09-28. Scope: add a way to enroll an employee's face gallery
directly from a live RTSP camera (or a server-side video file, for demos),
capturing multiple views (front/left/right/top) at multiple distances,
instead of only uploading photos one at a time or bundled into a zip.

## Context — what was already there

The face-gallery/multi-distance machinery already existed, built for the
zip-upload enrollment flow (`backend/app/routers/dataset_calibration.py`):

- `depth_XXXm/<view>.jpg` on-disk dataset structure.
- `_employee_zip_uploads` — a per-employee registry of `{org_id, zip_path,
  declared_distances}` that `/calibration/run_all` re-reads to fit the
  distance-decay curve (`near_k`, `near_sigma0`).
- An org-wide "locked distances" convention: whichever employee is
  enrolled first for an org fixes the distance set every later employee's
  zip is checked against.
- `POST /employees/enroll_from_zip` writes only the *reference* (closest
  declared) distance's embedding into `EmployeeFaceGallery`; the other
  distances are kept on disk purely to feed calibration.

There was no way to populate any of this from a live camera — enrollment
meant uploading pre-taken photos.

## What was added

**Backend**

- `app/vision/enrollment_session.py` (new) — a minimal per-connection
  session wrapping `app/vision/capture.py`'s `open_frame_source()` (the
  same RTSP hardening — reconnect, timeouts, newest-frame reads — used by
  the real stream worker). Deliberately NOT built on `StreamWorker`: that
  class can write real attendance records when live, which enrollment must
  never do. Frames are pulled on demand, nothing is pushed on a timer.
  Idle sessions (15 min unused) are swept.
- `app/routers/enrollment_rtsp.py` (new):
  - `POST /enrollment/rtsp/start` / `POST /enrollment/rtsp/stop` — open
    and close a session against an admin-supplied source (RTSP URL or
    server-side file path), org-scoped.
  - `WS /ws/enrollment/rtsp/{session_id}` — pushes raw preview frames only
    (no detection — kept cheap so it can run continuously while the admin
    positions the employee), same auth pattern as `routers/streams_ws.py`
    (session token as a query param, since browsers can't set the
    Authorization header on a WebSocket handshake).
  - `POST /enrollment/rtsp/capture` — the real work, run once per
    view×distance the admin clicks "Capture" for: grabs the session's
    newest frame, runs real YOLO person detection, crops to the largest
    detected person (`face_crop.crop_person_region`), extracts a face
    embedding (`face_embedder.extract_embedding`), writes the crop into
    the SAME `depth_XXXm/<view>.jpg` staging structure the zip flow uses,
    re-zips it into the same `EMP-ZIP-<employee_id>.zip` path
    `enroll_from_zip` produces, and registers it in the same
    `_employee_zip_uploads` dict — so `/calibration/run_all` picks up
    RTSP-captured employees with zero changes of its own. Only the
    reference (closest declared) distance's embedding updates
    `EmployeeFaceGallery`, matching `enroll_from_zip`'s own behavior.
    Enforces the same org-wide locked-distances rule as the zip flow.
- `app/main.py` — registers the new router.

No existing endpoint, table, or calibration/matching logic was changed.

**Frontend** (`frontend/dashboard.html`, `frontend/js/tabs/employees.js`)

- New "Enroll from a live RTSP camera" card on the Employees tab: connect
  to a source, live `<img>` preview over the new WebSocket (mirrors the
  Live Stream tab's own preview pattern), then a view/distance picker and
  a "Capture" button that shows the captured crop and a running checklist.
- The distance-set UI (`renderDistanceField`) was generalized to render
  into both the existing zip card and the new RTSP card from one shared
  `lockedDistances` value, since both flows share one org-wide distance
  set on the backend.

## Not done / known limits

- No RTSP camera was available to test against in this session (same
  caveat as the rest of the app's RTSP support) — exercised against the
  bundled `sample_data/demo-camera-feed.mp4` as a stand-in; OpenCV treats
  both sources identically at the `VideoCapture`/`open_frame_source`
  level, but a real camera has its own failure modes a file can't fully
  exercise.
- The live preview shows raw frames only, no bounding box — the admin
  positions the employee visually; detection only runs at capture time.
