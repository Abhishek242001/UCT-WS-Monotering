# Changelog — gallery cache hit/miss ratio

Target branch: `local`. Do NOT merge to `main` yet.

## What was asked

Now that `_get_gallery()` caches the face gallery in memory per
`StreamWorker` (see `CHANGELOG-gallery-cache-and-recognition-fix.md`),
check how well that cache is actually performing — the hit/miss ratio.

## What changed

- **`backend/app/vision/stream_worker.py`**:
  - Two new counters, `_gallery_cache_hits` / `_gallery_cache_misses`,
    incremented inside `_get_gallery()` at the exact two return points
    (served-from-cache vs. had-to-query-the-DB) — not recomputed
    separately, so they can't drift from what `_get_gallery()` actually
    did.
  - New method `get_gallery_cache_stats()` → `{"hits", "misses",
    "hit_ratio", "cache_age_seconds"}`. `hit_ratio` is `None` (not `0.0`
    or a `ZeroDivisionError`) before the first call ever happens, since
    there's genuinely no ratio yet at that point, not a 0% one.
    `cache_age_seconds` is how long the current cached copy has been
    sitting in memory (useful to sanity-check against
    `GALLERY_CACHE_TTL_SECONDS`, currently 300s).

- **`backend/app/routers/streams.py`**: `GET /streams/list` now includes
  a `"gallery_cache"` object (the dict above) per stream, alongside the
  existing `frames_processed`/`is_alive`/etc. fields. No existing field
  removed or renamed, no new required request parameter — purely
  additive, so nothing already calling `/streams/list` breaks.

## How to check it

While a stream is running:

```
GET /streams/list?org_id=<your org id>
Authorization: Bearer <admin token>
```

Each stream in the response now has:

```json
"gallery_cache": {
  "hits": 42,
  "misses": 1,
  "hit_ratio": 0.9767441860465116,
  "cache_age_seconds": 118.3
}
```

A healthy running stream should very quickly settle into `misses: 1` (the
very first identify() call for that camera) with `hits` climbing from
there, i.e. a hit ratio approaching 1.0 — a miss only happens again once
every `GALLERY_CACHE_TTL_SECONDS` (300s) when the cache reloads. If
you're seeing a low hit ratio in practice, that would mean something is
restarting the `StreamWorker` far more often than expected (each new
worker starts its own cache from zero), not that the cache itself is
behaving wrong.

## Tests

Extended `tests/real-app-smoke/test_gallery_cache.py` (was 6 tests, now
10) — added hit/miss/hit_ratio assertions to each existing scenario
(first call, served-from-cache, TTL reload, independent per-worker
caches) plus one new test confirming `hit_ratio`/`cache_age_seconds` are
`None` before any call. All use the existing `FakeDB` stand-ins, no real
DB or model needed.

Run in this sandbox: `10 passed`.

## Not done / known limits

- This is in-process, per-`StreamWorker` state — restarting a stream (or
  the backend process) resets the counters to zero. If you want a
  longer-lived/aggregated view (e.g. across restarts, or across all
  cameras in an org) that would need its own small persistence layer;
  say so if you want that next, this is intentionally the minimal
  in-memory version to answer "is the cache actually being hit."
- Not wired into the one-off video-analysis export path
  (`video_export.py`) — that path loads the gallery once per export run
  with no repeat `identify()` calls against the same in-memory copy, so
  "hit/miss ratio" isn't a meaningful concept there.
