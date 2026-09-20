# Changelog: crop-to-person-box wired into the live pipeline

## What changed

`backend/app/vision/stream_worker.py` — `_identify()` now crops the frame
to the specific person occupying the workstation's ROI
(`app/vision/face_crop.py`) before calling
`face_embedder.extract_embedding()`, instead of passing the full,
uncropped frame.

This is the same crop-to-person-box fix already wired into
`workstations.py`'s `simulate_detection` endpoint (see
`docs/100-key-points.md` point 30 and `tests/real-app-smoke/test_face_crop.py`),
now also applied to `stream_worker.py` — the code path actually used by
both the Live Stream and Video Analysis tabs. It was not previously
connected there; `_identify()` called `face_embedder.extract_embedding()`
on the full frame.

Two real problems were being caused by this, not one:

1. **The diagnosed cause of "low recognition performance."** A person who
   is a small part of a wide workstation-desk shot has a proportionally
   tiny face after InsightFace's internal `det_size=(320,320)` resize,
   which can fail detection entirely even though the person is clearly,
   visibly present — logged repeatedly as `"No face detected in
   1280x720 image..."` against real footage.
2. **A second, previously-unnoticed bug**, found while making this fix:
   with more than one occupied workstation in the same camera frame, the
   old code passed the identical full frame to every workstation's
   `_identify()` call, so InsightFace's arbitrary "first face found" had
   no way to know which face belonged to which workstation's occupant.
   Cropping to the specific person resolved for each ROI
   (`face_crop.best_overlapping_person`) fixes this as a side effect.

## Verification (not just reasoned about — run, with evidence)

A new test, `test_stream_worker_matches_person_via_crop` in
`tests/real-app-smoke/test_face_crop.py`, drives a real `StreamWorker`
end to end (not just the single-image `simulate_detection` endpoint,
which was already covered) against a synthetic video reproducing the
exact small-face-in-a-wide-frame scenario.

- **Against the pre-fix code**: test fails —
  `assert ws["similarity"] is not None` → `similarity: None`. Confirms
  the live pipeline really did silently produce UNKNOWN for a real,
  enrolled, visibly-present person.
- **Against the fixed code**: test passes — `similarity > 0.8`, correct
  employee ID matched.
- **Full regression run, fixed code**:
  - `tests/real-app-smoke`: 47/47 passed.
  - `tests/design-acceptance-suite`: 454 passed, 22 skipped (all
    pre-existing, documented hardware/infra skips — this suite does not
    import `stream_worker.py`, so it was not expected to be affected).

## Scope note

This fixes the crop-before-identification gap only. It does not touch:
tracking / persistent bounding-box labeling, the 60s recognition cadence
with faster low-confidence retry, activity classification, or the
attendance/time-tracking wiring (`record_detection`, `AttendanceSegment`
closing) — those are separate, still-pending pieces of the same round of
work.
