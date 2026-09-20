# Changelog: item 13 — occupancy hysteresis (the original flicker fix)

## What changed

The issue this entire round of work actually started from, finally
addressed directly: occupancy itself — not just face-match identity —
was observed flipping VACANT/ACTIVE rapidly frame to frame on real
footage. Since `status_changed` was never itself debounced, every single
flip fired a fresh DB event write, a fresh `_identify()` call, and (as
of later steps in this round of work) a fresh attendance write. The
noisy occupancy signal was driving most of the downstream flicker —
including a fair share of what looked like "identity flickering," since
each spurious re-identification is an independent chance for a
borderline face read to land on MATCH or UNKNOWN.

`app/logic.py` — new pure function `apply_occupancy_hysteresis()`: a
workstation's committed ACTIVE/VACANT status only changes once the raw,
per-frame detection has held consistently for
`OCCUPANCY_HYSTERESIS_FRAMES` (default 3) **consecutive** calls. A flip
that reverts before reaching that threshold doesn't count at all — the
attempt resets, it doesn't partially carry over.

Wired into **both** `app/vision/stream_worker.py` (the live pipeline)
and `app/vision/video_export.py` (the downloaded-video export) — the
same shared, single-tested function, not two separate copies of the same
state machine. Consistent with this project's own stated design goal for
that file ("live and downloaded output match").

A person no longer detected on a specific frame, while the committed
status is still ACTIVE (because hysteresis hasn't let a brief blip flip
it to VACANT yet — e.g. a momentary occlusion), is now handled
explicitly: identification is skipped for that one frame (there's
genuinely no one to crop), while the desk correctly keeps reading as
occupied rather than incorrectly reporting VACANT.

## Verification

New test file `tests/real-app-smoke/test_occupancy_hysteresis.py`
(6 tests):

- `apply_occupancy_hysteresis()` tested directly and exhaustively: first
  observation commits immediately; a single differing frame does not
  commit; a flip that reverts before threshold does not commit AND does
  not partially carry over into a later attempt; a genuinely sustained
  flip commits on exactly the Nth consecutive frame, not before; two
  workstations are tracked with fully independent state.
- `test_single_frame_blip_produces_no_identification_event` — the real
  proof, not just isolated logic: a real synthetic video with a
  deliberate single-frame occupancy blip, run through a real
  `StreamWorker`, produces **zero** identification events for that blip
  and **exactly one** MATCH for a separately-included, genuinely
  sustained occupancy period later in the same video — checked against
  real rows in `workstation_identity_events`, not mocked.

Full regression, all passing:
- `tests/real-app-smoke`: **94/94** (88 previous + 6 new). Notably,
  every existing test that exercises occupancy transitions through the
  standard `synthetic_video` fixture (3 blank frames, then 3 real-photo
  frames) still passes unmodified — that fixture's 3 consecutive
  occupied frames happen to land exactly at the default
  `OCCUPANCY_HYSTERESIS_FRAMES` threshold, confirmed empirically by this
  run rather than assumed.
- `backend/tests/test_video_pipeline.py`: **17/17** (exercises
  `video_export.py`'s now-also-hysteresis-aware path).
- `tests/design-acceptance-suite`: **454 passed, 22 skipped** (unaffected).

## Scope note

- `OCCUPANCY_HYSTERESIS_FRAMES=3` is a documented starting point, not a
  measured constant — like several other thresholds in this codebase, it
  should be tuned against real footage once available (a higher value
  trades faster real-transition responsiveness for stronger flicker
  suppression, and vice versa).
- This addresses occupancy-level flicker specifically. A secondary,
  smaller refinement discussed but **not built** in this step: identity-
  level hysteresis, so a single borderline UNKNOWN read doesn't
  immediately overwrite a previously-confident MATCH label (while still
  updating immediately the moment a fresh MATCH comes in — asymmetric,
  quick to trust a good read, slower to discard one). Worth doing as a
  follow-up if residual identity flicker is still visible on real
  footage after this fix; not assumed necessary without seeing that.
