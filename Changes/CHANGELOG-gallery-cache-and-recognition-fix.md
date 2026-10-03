# Gallery cache + finally-wiring the recognition fix

**Target branch: `local`** (not `main`). This builds on whatever `local`
currently has (the RTSP-camera enrollment feature) — do not apply this to
`main`.

## The headline finding

While investigating "two people get recognized as the same employee,"
I found that the recognition fix designed in an earlier session
(`classify_confidence_zone`, `face_width_px_at_distance` in `logic.py`)
had **never actually been applied** to this codebase — the helper
functions existed, but `decide_match_status()` never received a
`face_width_px` argument, and both `stream_worker.py` and
`video_export.py` were still discarding the face width (`_width_px`,
underscore-prefixed = unused) instead of passing it through. The
FPS fix (removing the per-frame temp-JPEG round trip) was in the same
boat — also never applied.

This change finishes applying both of those, and adds the gallery cache
you asked for on top.

## What changed

**`backend/app/logic.py`** — `decide_match_status()` now takes an
optional `face_width_px` parameter. A face crop under 50px is always
`UNKNOWN` regardless of similarity; 50–79px needs a stricter 0.60
threshold instead of the default 0.45; 80px+ is unchanged. This is the
actual fix for two different people's faces, both shrunk by distance,
coincidentally both clearing the same flat threshold against the same
stored embedding. Omitting the parameter reproduces the exact prior
behavior — fully backward compatible.

**`tests/design-acceptance-suite/src/logic.py`** — mirrored the same
change, since this is a byte-for-byte duplicate of `logic.py` used by
the independent contract-test suite. Left unmirrored, that suite would
have kept testing stale behavior.

**`backend/app/vision/yolo_detector.py`** — `detect_people()` and
`detect_and_track_people()` now explicitly accept either a file path or
an in-memory frame (numpy ndarray). No functional change was needed here
— Ultralytics already dispatches on type internally — just docstrings
making it explicit, since this is what the caller-side fix below relies on.

**`backend/app/vision/stream_worker.py`** —
1. *FPS fix*: `_process_frame()` no longer writes every frame to a temp
   JPEG and reads it back; the frame ndarray goes straight to YOLO.
2. *Gallery cache (what you asked for)*: new `_get_gallery()` method
   caches the org's face gallery in memory, reused for
   `GALLERY_CACHE_TTL_SECONDS` (env-configurable, default 300 = 5 min)
   instead of querying the database on every single `_identify()` call.
   Each `StreamWorker` instance (one per camera) keeps its own
   independent cache.
3. *Recognition fix wiring*: `_identify()` now captures the face width
   from `extract_embedding()` (previously discarded) and passes it to
   `decide_match_status()`.

**`backend/app/vision/video_export.py`** — same three fixes, adapted to
this module's batch/offline shape: the two temp-file round trips are
gone, a new `_load_gallery()` is called once per `annotate_video()` run
instead of once per detection interval within that run, and the width
gate is wired into `_identify()` here too.

## What I verified (actually ran, not just designed)

Unlike previous sessions, `cv2`, `numpy`, `fastapi`, `sqlalchemy`, and
`pytest` were all already available in my environment, and
`ultralytics`/`insightface` are only lazily imported in this codebase —
so I could actually import and test these modules this time, not just
hand you code I couldn't run.

- **27 new tests**, all passing:
  - `test_distance_confidence_gate.py` (15 tests) — pure logic tests on
    the width gate: reliable/degraded/unreliable zone boundaries, the
    SNR gate still applies, backward compatibility when the parameter
    is omitted, VACANT/missing-data early-outs still work.
  - `test_gallery_cache.py` (6 tests) — confirms the cache is actually
    hit (query count stays at 1 across repeated calls within the TTL),
    reloads after TTL expiry, picks up corrected data on reload, and
    that two `StreamWorker` instances don't share a cache.
  - `test_no_tempfile_per_frame.py` (6 tests) — confirms the exact frame
    object (not a path string) reaches `model.predict()`/`model.track()`,
    that path-string calls still work (backward compat for single-still
    endpoints), and that zero temp JPEGs are created during
    `_process_frame()`.
- **Re-ran the full `tests/real-app-smoke` suite**: 107 passed. The only
  failures were the same pre-existing `ModuleNotFoundError: ultralytics`
  gap this project has always had in my sandbox (tests that directly
  `import ultralytics` to build synthetic test video, plus one
  concurrency test whose built-in 60s thread-join exceeded the 15s
  per-test timeout I set to protect my own sandbox) — **zero regressions
  caused by these changes.**
- **Re-ran `tests/design-acceptance-suite`**: all 65 existing tests still
  pass unchanged against the mirrored `logic.py`.

## What to run on your end

```bash
git checkout local
# apply this zip's contents on top (xcopy as usual), then:
cd backend
python -m pytest ../tests/real-app-smoke -q
python -m pytest ../tests/design-acceptance-suite -q
```

You should see everything pass except the same `ultralytics`-dependent
tests that have never run in my sandbox — those need your Lightning
environment, same as always.

## Honest gaps / things to know

- **The gallery cache means up to 5 minutes of staleness** on an
  already-running stream after you re-enroll someone. Restarting the
  stream always picks up the latest gallery immediately, since the
  cache lives on the worker instance. Tune `GALLERY_CACHE_TTL_SECONDS`
  down if 5 minutes is too long for your workflow.
- **This does NOT touch the tracker ID-switch bug** (one label
  incorrectly following the wrong person after a track reassignment) —
  you confirmed this session that the bug you're seeing is the
  distance/confidence one, not that one. The ID-switch issue is still
  open if you want it tackled separately.
- **The distance calibration curve fitted at enrollment is still not
  consumed by live matching.** This fix uses the width-based gate, which
  is a real, tested improvement, but it's a simpler/separate mechanism
  from the per-employee `near_k`/`near_sigma0` decay curve. That
  remains a known gap.
