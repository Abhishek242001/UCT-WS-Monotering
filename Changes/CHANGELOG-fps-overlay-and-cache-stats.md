# Changelog — Live FPS overlay + gallery cache hit/miss stats

Target branch: `local`. Do NOT merge to `main` yet.

Combines two independent, previously-separate changes into one zip for a
single apply/test pass:

1. **Live FPS counter overlay** — full details in
   `CHANGELOG-fps-overlay.md`.
2. **Gallery cache hit/miss ratio** — full details in
   `CHANGELOG-gallery-cache-hit-miss-stats.md`.

Both build on top of the already-applied
`CHANGELOG-gallery-cache-and-recognition-fix.md` (distance/confidence
gate + in-memory gallery cache). Nothing here changes that logic — this
only adds visibility into how it's performing.

## Files touched

- `backend/app/vision/frame_annotate.py` — new optional `fps` param on
  `draw_annotations()`; draws "FPS: NN.N" top-left when given, otherwise
  byte-for-byte unchanged.
- `backend/app/vision/stream_worker.py` —
  - `_tick_fps()` / `_fps_ema`: smoothed real processing-rate estimate,
    fed into the live preview overlay.
  - `_gallery_cache_hits` / `_gallery_cache_misses` /
    `get_gallery_cache_stats()`: counts how often `_get_gallery()` served
    from the in-memory cache vs. had to re-query the DB.
- `backend/app/routers/streams.py` — `GET /streams/list` now returns a
  `"gallery_cache"` object per stream (purely additive).
- `tests/real-app-smoke/test_fps_overlay_display.py` (new, 5 tests).
- `tests/real-app-smoke/test_gallery_cache.py` (extended, 6 → 10 tests).

Run in this sandbox: `12 passed` (both test files together). No existing
test file's assertions changed — only new ones added.

## What to check once this is live

- **FPS**: top-left of the live preview frame. Target from this
  session's plan: ≥15-20 fps on GPU.
- **Cache hit ratio**: `GET /streams/list?org_id=<id>` →
  `gallery_cache.hit_ratio` should climb toward 1.0 quickly (first call
  is always a miss, everything after within 300s is a hit).
- **Face matching**: re-verify the two-person case from the original bug
  report now resolves correctly — this isn't touched by either change
  here, but is the other half of what you said you're testing end-to-end
  right now.
