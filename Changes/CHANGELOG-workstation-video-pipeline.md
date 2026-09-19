# Changelog — Workstation ROI, Video Analysis Visibility, and Video Export

Session: 2026-09-18. Scope: multi-ROI drawing on the Workstations tab,
visibility into what the Video Analysis tab will actually use before
running, a real bug fix in the live/video detection pipeline, and two
new download features (raw clip, annotated video).

## Context — what was already there

Before touching anything, the existing Workstations/Video Analysis code
was read directly, not assumed:

- Workstation ROIs were already keyed by `org_id + cam_id + name`
  (`backend/app/routers/workstations.py`), so multiple named ROIs per
  camera were already fully supported server-side and already correctly
  consumed by `StreamWorker._load_rois` — no backend change was needed
  for multi-ROI itself.
- The live/video-analysis WebSocket push (`app/vision/event_bus.py`,
  `app/routers/streams_ws.py`) already had a lazy-queue design that
  buffers messages for a not-yet-connected subscriber, so a race between
  worker start and WebSocket connect was already handled correctly.
- `backend/app/routers/videos.py`'s `_ensure_demo_workstation` already
  auto-creates a full-frame `Demo-Desk` ROI (no employee assigned) for
  any camera ID with no saved workstation — confirmed by reading the
  code, not guessed, since this silently changes what a video analysis
  run actually tests against.

## Changes

### `frontend/index.html` — Workstations tab (multi-ROI drawing)
- Added `savedRoiBoxes[]`, tracked per currently-loaded photo. Every box
  saved via "Save workstation" is now kept visible on the canvas in
  green with its name label, the name field and draft rectangle reset
  automatically, so multiple named workstations (Desk-A, Desk-B, ...)
  can be drawn and saved on one uploaded photo in sequence, with all of
  them visible at once. Previously the drawn box vanished from view the
  moment a second box was started, even though it remained saved in the
  database.
- New photo upload resets the accumulated box list (boxes belong to the
  specific frame they were drawn on).

### `frontend/index.html` — Video Analysis tab (ROI preview)
- New `previewVideoRoi()` function (debounced 400ms), triggered by
  typing into the Org ID or Camera ID fields. Calls the same
  `GET /workstations/check` endpoint the Workstations tab uses and shows
  either the matched workstation name(s) + coordinates, or an explicit
  warning that a full-frame `Demo-Desk` auto-fallback will be used with
  no employee assigned. Previously these fields were plain text inputs
  with zero feedback before clicking "Run AI Analysis".

### `backend/app/vision/stream_worker.py` — real bug fix
`_process_frame` previously wrote **and WebSocket-published a `VACANT`
event on every single not-occupied frame**, with no de-duplication —
unlike the `ACTIVE`/identify path, which was already correctly gated to
fire only on a state transition or a slow heartbeat
(`HEARTBEAT_SECONDS`). At `poll_interval_seconds=0` (hardcoded for video
analysis), this flooded both the database and the browser's event log
for any video with sustained vacancy, which is what made the frontend
"live analysis" view look frozen/unresponsive.

Fixed by applying the exact same transition-or-heartbeat gate to the
VACANT branch that the ACTIVE branch already used. `previous_status`
now starts as `None` (not `"VACANT"`) so the very first frame observed
always emits one event, establishing initial state.

### `backend/app/vision/video_export.py` (new file)
Two batch (non-live) video functions, reusing the same
`yolo_detector` / `face_embedder` / `app.logic` modules `StreamWorker`
uses so results are consistent with what the live pipeline would
decide:

- `extract_clip(source, dest, max_seconds)` — prefers an `ffmpeg`
  stream-copy (exact quality, keeps audio, fast regardless of length);
  falls back to a frame-by-frame OpenCV re-encode (video only, no audio)
  if `ffmpeg` is unavailable or the stream-copy fails.
- `annotate_video(source, dest, db, org_id, cam_id, max_seconds,
  detect_every_n_frames)` — draws detected-person boxes, ROI boxes, and
  occupancy/identity status onto each frame; raises `ValueError` if no
  ROI exists for `org_id`/`cam_id` (matches the pipeline's own
  requirement). Runs YOLO only every `detect_every_n_frames` frames
  (default 3) and reuses the last result in between, trading detection
  freshness for speed — CPU-only YOLO inference on every frame of a
  multi-minute video is not practical (see Verification). Output is
  written as `mp4v` via OpenCV, then re-encoded to H.264 via `ffmpeg` if
  available, since `mp4v` does not play natively in most browsers.

### `backend/app/routers/videos.py` — video metadata now survives a backend restart
`_videos` (the catalog `find_by_hash`, `/videos/{id}/analyze`, `/clip`,
and `/annotate` all depend on) was a plain in-memory dict with no
persistence — pre-existing, not introduced this session, but only
discovered now because a restart (for the `MAX_VIDEO_UPLOAD_BYTES` fix,
same session) exposed it: every video uploaded before a restart became
permanently unfindable — `find_by_hash` could never match it again (a
re-upload of the identical file looked "new" instead of being flagged
as a duplicate), and its `video_id` 404'd on every other endpoint, even
though the actual file was still sitting on disk.

Fixed by writing a `{video_id}{ext}.meta.json` sidecar next to each
video at upload time (`_save_video_meta`), and rebuilding `_videos` from
those sidecars once at process start (`_load_videos_from_disk`, called
at module-import time). Videos uploaded before this fix has no sidecar
and remain unrecoverable — only future uploads survive a restart.

### `backend/app/routers/videos.py`
Three new endpoints:
- `GET /videos/{video_id}/clip?seconds=300` — downloads the first N
  seconds of the raw uploaded video (default 300 = 5 minutes, per the
  original request), cached on disk by `video_id + seconds`.
- `POST /videos/{video_id}/annotate` (form: `org_id`, `cam_id`,
  `max_seconds` default 60 capped at 600, `detect_every_n_frames`
  default 3) — synchronous, blocks until rendering finishes; returns
  `annotated_id`, `download_url`, `playable_in_browser`, and timing
  info.
- `GET /videos/annotated/{annotated_id}/download` — serves the
  rendered file. Annotated videos are tracked in an in-memory dict
  (`_annotated_videos`), same persistence model as `_videos` — lost on
  backend restart.

### `frontend/index.html` — Video Analysis tab (downloads + robustness)
- New "Downloads" card (visible once a video is uploaded/selected):
  "Download clip" (seconds field, defaults 300) and "Generate + download
  annotated video" (seconds + detect-every-N-frames fields, uses the
  same Org/Camera ID fields as Run AI Analysis).
- Progress bar fix: previously only updated
  `if (msg.total_frames)`, so any video whose container doesn't report a
  reliable frame count (confirmed common for some CCTV export formats)
  left the bar frozen at 0% indefinitely even while frames were actively
  processing. Now falls back to showing a live frame count.
- Event log capped at the most recent 300 DOM lines (older lines
  dropped from view only, not from the database) — defensive, on top of
  the backend-side VACANT-flood fix, so a long video can't freeze the
  tab via unbounded DOM growth.

## Verification

1. **Syntax**: extracted `<script>` and ran `node --check` after every
   frontend edit (3 passes). `python3 -m py_compile` on all three
   changed/new backend files. All clean.
2. **App boot + route table**: loaded the real `app.main:app`,
   confirmed all 7 `/videos/*` routes register with the expected
   methods and no path conflicts (`_IncludedRouter.original_router.routes`
   inspected directly, not assumed), and got a real `200` from
   `TestClient` against `/openapi.json`.
3. **`ultralytics` was missing from this sandbox** despite being listed
   in `requirements.txt` — installed it (`pip install ultralytics`) so
   the detection path could be exercised for real rather than mocked.
   Flagging in case the same gap exists on the deployment server.
4. **`extract_clip` — real run**: against
   `sample_data/demo-camera-feed.mp4` (90KB, 2fps, 20 frames), 5-second
   clip extracted via the `ffmpeg` path in 0.05s, output file
   non-empty (76KB).
5. **`annotate_video` — real run**: same source video, `max_seconds=5`,
   `detect_every_n_frames=3`. First call: 44.5s (includes a one-time
   ~40s YOLO model download/load, confirmed by re-running immediately
   after — second call: 2.7s for the same 10 frames). Output file
   produced, `playable_in_browser: True` (this sandbox has `ffmpeg` with
   `libx264` available — confirmed separately by testing `mp4v`, `avc1`,
   `H264`, `X264` fourcc support directly: only `mp4v` opens via
   OpenCV's `VideoWriter` here, confirming the H.264 re-encode step is
   load-bearing, not optional, for browser playback).
6. **`stream_worker.py` fix — real before/after comparison**, not just
   code review: ran the *exact* pre-fix `_process_frame` (isolated as a
   subclass override, same source video, same 20-frame limit) against
   the *actual* post-fix version. **Before: 11 DB/WebSocket events for
   20 frames. After: 3 events for the same 20 frames** — event_types
   `VACANT, UNKNOWN, VACANT` in both runs (identical detection outcome,
   only the debounce differs, confirming the fix doesn't change what's
   detected, only how often it's reported).
7. **Video-metadata-survives-restart fix — genuine two-process test**,
   not just code review: ran an upload in one Python process (writes the
   sidecar), then started a completely fresh interpreter with an empty
   `_videos` dict pointed at the same `UPLOADED_VIDEOS_DIR` (the same
   condition a real backend restart produces). Output:
   `Recovered 1 video(s) from disk metadata after restart`, and a
   `find_by_hash`-style lookup in the fresh process correctly matched
   the video by its SHA-256 hash.
8. No existing pytest suite was found anywhere in this repo copy
   (`find . -iname "test_*.py"` → no matches) despite
   `stream_worker.py`'s own docstring referencing
   `tests/real-app-smoke/test_stream_worker.py` — same
   references-a-file-that-doesn't-exist pattern already flagged in the
   previous session's changelog for `docs/enrollment-workflow.md`.
   Real functional smoke tests (above) were run in its place; nothing
   is left behind as an automated regression test.
9. Test DB, seeded workstation rows, and temp video/output files created
   during verification were removed before packaging; no test artifacts
   are included in the delivered files.

## Remaining Issues / Not Done

- **Root cause of your real CCTV footage showing VACANT throughout is
  still unconfirmed.** The debounce fix explains why the event log
  looked frozen; it does not explain (and can't, without your actual
  footage) whether occupancy is genuinely absent, the ROI is
  misaligned, or YOLOv8n's `confidence_threshold=0.4` is too strict for
  small/distant/grainy subjects. Recommended next step: run the new
  annotated-video export on a short 30–60s slice of the real footage —
  it will show exactly what YOLO is and isn't detecting, frame by
  frame.
- **`face_backend=stub` is still active** (carried over from the
  earlier session) — all identity results (`UNKNOWN`, similarity
  scores) remain placeholder until `requirements-face-recognition.txt`
  is installed on the deployment server. Not addressed this session;
  the annotation feature will overlay whatever the active backend
  produces, stub or real.
- **`ffmpeg` availability on the actual Lightning deployment server is
  unverified** — confirmed present in this sandbox, but `annotate_video`
  and `extract_clip` both degrade gracefully (raw `mp4v` output,
  OpenCV re-encode fallback respectively) if it's missing there. The
  frontend surfaces `playable_in_browser: false` when this happens so
  it's visible rather than silently broken.
- **Real-world annotation runtime on the actual deployment server's
  CPU is unmeasured** — only this sandbox's hardware was benchmarked
  (~0.675s/YOLO-call warm). A full 5-minute clip at typical 15–30fps
  CCTV could take considerably longer than the 60-second default;
  the 600-second hard cap and `detect_every_n_frames` parameter exist
  specifically to keep this bounded, but the right values for the
  user's actual hardware are still unknown.
- **No automated test coverage added** for `video_export.py` or the new
  `/videos/*` endpoints — same gap noted for the previous session's
  bulk-enrollment work. Recommend adding pytest coverage, especially
  around the `ValueError` paths (no ROI, invalid `max_seconds`/
  `detect_every_n_frames`), before this becomes routine.
- **Annotated video storage is in-memory only** (`_annotated_videos`
  dict) — same restart-loses-everything problem that `_videos` had
  before this session's fix, but **not fixed here**: only the original
  uploaded-video catalog got the sidecar-persistence treatment.
  Regenerating an annotated video is cheap enough (re-run
  `POST /videos/{id}/annotate`) that this was judged lower priority, but
  it's the identical bug, left as-is. Same for disk cleanup — no
  automatic eviction of old clips/annotated files in either directory.

---

## Addendum (same day) — working through the Remaining Issues above

### `backend/app/routers/reports_and_health.py` — `/system/health` now checks real capabilities
Was previously hardcoded (`pipeline_status: "HEALTHY"` always, regardless
of anything). Now actually probes:
`yolo_model_loadable` (+ `yolo_load_error` if not), `opencv_ffmpeg_support`
(checked via `cv2.getBuildInformation()`), `rtsp_capable` (same
underlying dependency as `opencv_ffmpeg_support` — RTSP is demuxed
through OpenCV's FFMPEG backend), `ffmpeg_cli_available` (`shutil.which`),
`annotated_video_playable_in_browser` (same as `ffmpeg_cli_available` —
this is what `video_export.py`'s re-encode step depends on),
`mp4v_video_writer_available` (an actual `cv2.VideoWriter` open/close
probe, not just an assumption), and `face_recognition_is_stub`.
**Directly resolves** the "ffmpeg availability... unverified" item —
`curl -H "Authorization: Bearer $TOKEN" "$BACKEND/system/health?org_id=1"`
now tells you, for real, on your actual server. No frontend UI added
for this yet (checked via `curl`/`/docs` only) — flagging as still open
below.

### `backend/app/routers/videos.py` + `video_export.py` — new diagnostics endpoint
`GET /videos/{video_id}/diagnostics?org_id=&cam_id=&num_samples=8` and
`video_export.sample_diagnostics()`. Samples `num_samples` frames spread
across the **entire** video duration (not just the start, unlike
`annotate_video`), runs real YOLO detection on each, and reports:
frames with ≥1 person, confidence min/mean/max, occupancy hits per
workstation, and the **measured** YOLO seconds-per-call on this specific
server, extrapolated to a full-video runtime estimate. Runs in seconds,
not minutes — meant to be run before committing to a full
`annotate_video` render.

**Directly addresses** both "root cause of VACANT... unconfirmed" (the
confidence/occupancy numbers tell you immediately whether YOLO is
detecting anyone at all, without waiting for a rendered video) and
"real-world annotation runtime... unmeasured" (this measures actual
per-call latency **on whichever server it runs on** — your Lightning
box, your local RTSP machine tomorrow, wherever — not this sandbox's
number).

A real bug was caught and fixed while verifying this: the first
sampled frame's timing included the one-time YOLO model
download/load cost (~40s, per this session's earlier benchmark),
which badly skewed the reported "per call" average (9.1s vs the true
~0.24s once warm). Fixed by calling `yolo_detector.get_model()` once
before the timed sampling loop starts. Caught by actually running the
function against the real sample video twice, before/after the fix —
not by code review alone.

### `backend/app/routers/videos.py` — `_annotated_videos` now also survives a restart
Same sidecar-persistence pattern applied to `_videos` earlier is now
also applied to `_annotated_videos` (`_save_annotated_meta`,
`_load_annotated_videos_from_disk`). **Directly resolves** the
"Annotated video storage is in-memory only" item from the previous
Remaining Issues list.

### `backend/tests/test_video_pipeline.py` (new) — first automated test suite in this repo
16 tests, real end-to-end (real `POST /admin/login`, real SQLite DB,
the actual shipped `sample_data/demo-camera-feed.mp4`, real YOLO
inference — nothing mocked). Covers: duplicate-hash detection,
restart-persistence sidecars (both `_videos` and `_annotated_videos`),
clip download (success + invalid-seconds + 404), diagnostics (success +
missing-ROI + invalid-num_samples), annotate+download round-trip
(success + missing-ROI + over-cap + invalid detect_every_n_frames +
404), and the new system-health endpoint. **Directly resolves** the "No
automated test coverage added" item.

### `frontend/index.html` — Diagnostics card
New card on the Video Analysis tab, above Downloads: "Run diagnostics"
button calling the new endpoint, results table showing frame count,
detection confidence, occupancy hits, measured YOLO time, and the
runtime estimate — meant to be the first thing you run on tomorrow's
RTSP footage before generating a full annotated video.

## Verification (this addendum)

1. `node --check` on extracted JS, `python3 -m py_compile` on all four
   changed/new backend files — all clean.
2. Full route table re-inspected after every change — no conflicts,
   `/videos/{video_id}/diagnostics` registers correctly alongside the
   other six `/videos/*` routes.
3. `sample_diagnostics()` run twice against the real sample video
   (before/after the warmup-skew fix) — confirmed the bug and the fix
   with actual numbers (9.116s → 0.236s per call).
4. `_annotated_videos` persistence verified with the same genuine
   two-process test used for `_videos` earlier — process 1 writes,
   a fresh interpreter (process 2) recovers it correctly.
5. **`pytest tests/ -v` — 16/16 passed**, real run, output captured
   above. Re-ran once more after the frontend/health changes to confirm
   nothing regressed — still 16/16.
6. All test-generated files (DB, uploaded/clip/annotated video
   directories, `.pytest_cache`) removed before packaging.

## Remaining Issues — updated status

- ~~Root cause of VACANT throughout unconfirmed~~ — **tooling now
  exists** (diagnostics endpoint) to answer this quickly against real
  footage; the actual root cause for the user's specific CCTV video is
  still unknown until they run it.
- ~~ffmpeg availability unverified~~ — **now checkable** via
  `/system/health`; not yet run against the actual Lightning/RTSP
  deployment server.
- ~~Real-world annotation runtime unmeasured~~ — **now measurable** via
  the diagnostics endpoint on whichever server it's run on; still not
  actually measured on the user's real hardware.
- ~~No automated test coverage~~ — **added**, 16 tests, all passing.
- ~~`_annotated_videos` in-memory only~~ — **fixed**, same pattern as
  `_videos`.
- **`face_backend=stub` still active** — genuinely not addressed;
  requires installing `requirements-face-recognition.txt` on the
  deployment server, which this session cannot do remotely.
- **`/system/health` has no frontend UI** — checked via `curl`/`/docs`
  only in this session's own verification. Worth adding a small card if
  this becomes a routine pre-flight check.
- **Diagnostics' `extrapolated_full_annotate_seconds` assumes
  `detect_every_n_frames=3`** (hardcoded in the extrapolation formula,
  documented in the key name itself) — if the user runs
  `annotate_video` with a different value, the estimate won't match;
  not parameterized to keep the diagnostics endpoint's own signature
  simple.

---

## Addendum 2 — the actual "always UNKNOWN" root cause, found via the real repo zip

Everything above this point was built against an incomplete local sandbox
copy of this project that was missing `tests/`, `docs/`, and other
top-level files present in the real repository. The person uploaded a
full zip of the actual repo, which corrected several wrong assumptions
made earlier in this session (see below) and surfaced the real root
cause of the persistent "always UNKNOWN" face-recognition result.

### Corrected: a test suite already existed
Earlier changelogs/responses in this session stated "no automated test
suite exists" and cited `stream_worker.py`'s own docstring referencing
`tests/real-app-smoke/test_stream_worker.py` as a file that didn't exist.
That was **wrong** -- it existed all along in the real repository; it
was simply never present in the incomplete local copy being worked from.
There are, in fact, two real suites: `tests/real-app-smoke/` (43 tests,
exercises the actual `app.*` code, not a mock) and
`tests/design-acceptance-suite/` (476 tests, a mock/contract-layer suite
against a separate `src/mock_app.py`, per `100-key-points.md` point 100).

### Real root cause found: `docs/100-key-points.md` point 30
This file documents the project's actual design history and was not
previously available. Point 30 explicitly specifies: **"Crop face
detection to the workstation ROI (+padding) rather than the full
frame"** -- documented as intended, never implemented. Confirmed by
grep: zero cropping logic existed anywhere in `stream_worker.py`,
`video_export.py`, or `workstations.py`.

This is the actual, complete explanation for every "UNKNOWN with
similarity=—" result reported this session, including after installing
real `insightface` and re-enrolling: `extract_embedding` was running
InsightFace's own face detector on the ENTIRE frame at its configured
`det_size=(320,320)`. In a wide workstation/desk-view shot (as opposed
to a close-up enrollment photo), a person's face is a small fraction of
the frame -- after the internal resize to fit det_size, the face can
become too small for the detector to find at all, even though YOLO
correctly detects the full person and the face is clearly visible to a
human. Point 39 of the same doc independently confirms the pixel-width
mechanism: recognition needs ~80-120px face width for a confident match,
and detection itself degrades well before that for a naive full-frame
pass.

### Changes

**`backend/app/vision/face_crop.py` (new)** -- `crop_person_region(frame,
person, padding_fraction=0.4)`: crops an in-memory frame to a detected
person's box plus 40% padding, returns a temp JPEG path (caller unlinks).
`best_overlapping_person(people, roi, min_overlap_fraction=0.3)`: of all
YOLO detections, returns the one most likely to be this ROI's actual
occupant (highest confidence among those clearing `boxes_overlap`), or
`None`. Deliberately not added to `yolo_detector.py` -- that module's
own docstring explicitly excludes ROI-cropping as its responsibility.

**`backend/app/vision/stream_worker.py`** -- `_process_frame` now finds
the specific occupying person via `face_crop.best_overlapping_person`
instead of a bare `any(boxes_overlap(...))` boolean, and passes the raw
`frame` + that `person` into `_identify`, which crops before calling
`extract_embedding` instead of using the full saved frame.

**`backend/app/vision/video_export.py`** -- same change applied to
`annotate_video`'s detection loop and its own `_identify` function.

**`backend/app/routers/workstations.py`** -- same change applied to
`simulate_detection` (the endpoint behind "Try Detection" -- the exact
endpoint used throughout this session's troubleshooting). Added `cv2`
import (needed to read the frame before cropping) and `face_crop` to the
existing `app.vision` import line.

### Verification

1. **The failure, proven real** (not assumed): built a synthetic wide
   frame from a real photo with real faces (`ultralytics`' own bundled
   `zidane.jpg`), empirically calibrated to a scale (0.4x) where YOLO
   still detects both people but InsightFace's full-frame detector fails
   on both -- confirmed via direct calls to `yolo_detector.detect_people`
   and `face_embedder.extract_embedding` before writing any fix code.
2. **The fix, proven real**: cropping to each YOLO-detected person's box
   with 40% padding made face detection succeed for both people in the
   same synthetic frame (measured face widths 55.2px and 43.7px).
3. **End-to-end, through the real API**: enrolled a person from a
   close-up crop (mapped geometrically from the wide frame's detected
   box back to the original full-resolution photo, since YOLO's
   detection order doesn't guarantee the same physical person shares an
   index across different images), assigned them to a workstation, then
   called `POST /workstations/simulate_detection` against the wide
   frame. Before the fix: `event_type=UNKNOWN, similarity=None`
   (confirmed separately). After the fix: `event_type=MATCH,
   detected_employee_id` correct, `similarity=0.939`.
4. **Regression test added**: `tests/real-app-smoke/test_face_crop.py`
   (2 tests) -- one confirms the full-frame failure is real and current
   (fails loudly if `det_size`/detector pack changes enough to make the
   fix's premise stale), one runs the full enroll -> assign -> detect
   flow through the real API and asserts `MATCH` with `similarity > 0.8`.
5. **Full existing suite re-run after the fix**: `tests/real-app-smoke/`
   went from 41 passed / 2 failed to 43 passed (see below for what the 2
   failures actually were and how each was resolved).
   `tests/design-acceptance-suite/` (mock-layer, unaffected by these
   changes since it doesn't touch real `app.*` code): 454 passed, 22
   skipped (pending future work, documented per-skip, not failures).
   Combined `tests/real-app-smoke/` + `backend/tests/`: 62 passed.

### Two pre-existing test issues found and fixed while verifying (neither is a new regression from this session's earlier work being wrong -- see each)

1. **`test_stream_worker_does_not_identify_on_every_occupied_frame`**
   asserted exactly 3 `VACANT` DB rows for 3 vacant frames -- this
   encoded the OLD, buggy, undebounced behavior from before this
   session's earlier VACANT-flood fix (documented in Addendum 1 above),
   which is the actual, confirmed cause of the frontend event-log
   flooding reported earlier in this session. The debounced version
   correctly produces exactly 1 `VACANT` row (initial transition) + 1
   non-vacant row. Updated the test's assertion and docstring to
   document why, rather than silently patching it.
2. **`test_delete_employee_with_real_history_does_not_crash`** failed
   with a 404 on `/attendance/today` -- traced fully and confirmed this
   is **not an application bug**: the test hardcoded
   `"timestamp": "2026-09-09T09:00:00"` when recording attendance, but
   `/attendance/today` correctly looks up the record by the REAL current
   date, which had since moved on. Fixed by using
   `datetime.utcnow().isoformat()` in the test instead of a hardcoded
   date that will inevitably drift.
3. **Unrelated fixture conflict, found while running both suites
   together**: `backend/tests/test_video_pipeline.py` hardcoded the
   login password `"ChangeMe123!"`, which broke when run in the same
   pytest process as `tests/real-app-smoke/` (its `conftest.py` sets
   `DEFAULT_ADMIN_PASSWORD` to a different value process-wide, which the
   real seeding logic then also picks up for the other file's fresh DB).
   Fixed by reading `os.environ.get("DEFAULT_ADMIN_PASSWORD", ...)` the
   same way `app.main._seed_default_admin` actually does.

## Remaining Issues (this addendum)

- The distance-calibration curve issue from Addendum 1 (near_k/near_sigma0
  computed but never consumed by live detection) is **unchanged** by this
  fix -- cropping fixes whether a face is *detected* at all; it does not
  wire distance-adaptive confidence into the match decision. Still an
  open, separate decision for the user.
- `padding_fraction=0.4` was chosen as a reasonable default and validated
  against one synthetic scenario -- not tuned against a range of real
  camera distances/angles. If detection is still unreliable at the
  user's actual deployed camera distance after this fix, tuning this
  value (or the underlying `det_size`) is the next lever to pull.
- This fix improves *detection* (finding a face at all) but does nothing
  for *recognition accuracy* at genuinely long range/small face widths --
  `docs/100-key-points.md` point 39's ~80-120px guidance and points
  39-48's broader camera-placement guidance still apply and were not
  re-evaluated against the user's real deployment in this session.
