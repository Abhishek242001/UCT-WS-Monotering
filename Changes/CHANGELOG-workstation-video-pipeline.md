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
