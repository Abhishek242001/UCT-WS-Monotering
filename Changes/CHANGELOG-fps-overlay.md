# Changelog — Live FPS counter overlay

Target branch: `local`. Do NOT merge to `main` yet.

## What was asked

Show the current processing rate (frames per second) on the top-left of
the video frame, so an admin watching the live preview (or looking at a
screenshot) can see how fast the pipeline is actually running without
digging through logs.

Not to be confused with the earlier, differently-named "FPS fix" from
this project's history (removing a per-frame temp-JPEG round trip to
speed real throughput up) — that was a performance fix with no on-screen
UI; this is a small visible counter.

## What changed

- **`backend/app/vision/frame_annotate.py`** — `draw_annotations()` takes
  one new optional parameter, `fps: float | None = None`. When given, it
  draws `"FPS: NN.N"` in yellow at the top-left corner (10, 20) before
  anything else. When omitted (the default), it draws nothing extra —
  byte-for-byte the same output as before this change, so the one other
  caller (`video_export.annotate_video()`, the downloadable annotated
  video) is completely unaffected; the counter only shows up on the live
  preview for now, since "processing rate" is a less meaningful number on
  a video being analyzed faster or slower than real time.
  - The per-workstation status banner (drawn just below, also top-left)
    shifts down from y=24 to y=48 whenever `fps` is provided, so the two
    don't overlap. With `fps` omitted it stays at y=24, unchanged.

- **`backend/app/vision/stream_worker.py`**:
  - Two new instance attributes: `_fps_last_tick` (monotonic timestamp of
    the previous processed frame) and `_fps_ema` (smoothed frames/sec).
  - New method `_tick_fps()`, called once per processed frame at the top
    of the main loop (right where `consecutive_failures` resets, before
    `_process_frame`). It measures the real wall-clock gap since the
    previous frame and folds it into an exponential moving average
    (`alpha=0.2`) — this is actual processing throughput (detection +
    identification work included), not the separate, deliberately
    throttled `LIVE_FRAME_PUBLISH_FPS` preview-push rate. The EMA keeps
    the on-screen number from jumping around on every single frame (e.g.
    the slower heartbeat identify() tick wouldn't make the counter
    collapse to one bad frame's instantaneous rate).
  - `_maybe_publish_frame()` now passes `fps=self._fps_ema` through to
    `draw_annotations()`.

No other file changed. No API response shape changed. No existing
behavior changed for any caller that doesn't look at the new parameter.

## Tests

New file: `tests/real-app-smoke/test_fps_overlay_display.py` (5 tests, no
real YOLO/InsightFace install needed):

- `draw_annotations()` without `fps` draws nothing into an all-black
  frame (proves backward compatibility byte-for-byte).
- `draw_annotations()` with `fps` draws into the top-left region and
  nowhere else (on an otherwise-empty frame).
- Pathological `fps` values (`0.0`, `-1.0`) don't crash — it's just a
  label, not a gate on anything.
- `StreamWorker._tick_fps()` (constructed via `__new__` to skip the
  DB/model-heavy `__init__`, exercising the real method body) computes a
  plausible smoothed rate from two real ticks ~50ms apart.
- A single artificially slow tick visibly pulls the EMA down without
  collapsing it all the way to that one sample — proves the smoothing is
  doing something, not just storing the latest instantaneous value.

Run in this sandbox: `5 passed`. The rest of `tests/real-app-smoke/`
still needs the real `ultralytics`/YOLO weight to run end-to-end tests
like `test_stream_worker.py` — same pre-existing sandbox limitation as
earlier zips this session, so those weren't re-run here; nothing in this
change touches what they exercise (detection, tracking, identification
logic are all untouched).

## Not done / known limits

- The counter is only wired into the **live preview** path
  (`stream_worker.py`). It was deliberately left out of the downloadable
  annotated-video path (`video_export.py`) — that export can run faster
  or slower than real time depending on hardware, so a "processing FPS"
  number baked into a saved video file would be misleading rather than
  useful. Say if you want it there too (e.g. as "export speed: Nx
  realtime") and I'll add it as its own thing.
- This has not been run against your real camera/GPU yet — once you pull
  this into `local` and run the live stream, the number you see is the
  actual end-to-end rate (YOLO + tracking + the gallery-cache-backed
  identify() calls), so it should also reflect the GPU work from the
  `requirements-gpu.txt` change, if that's applied in the same
  environment.
