# Changelog: recognition cadence — 60s baseline, faster retry on UNKNOWN

## What changed

`app/vision/stream_worker.py`:

- `IDENTIFY_HEARTBEAT_SECONDS` default raised from 30 to **60** — the
  baseline cadence you asked for: one identification query per occupied
  desk per minute while confidently matched.
- New `IDENTIFY_LOW_CONFIDENCE_RETRY_SECONDS` (default **5**): when the
  last result for a desk was UNKNOWN — occupied, but no confident
  match — the next check happens after this much shorter interval
  instead of waiting the full 60s. A face that briefly turned away, was
  poorly lit, or was too small in that one frame is often resolvable on
  a quick second look; there's no reason a transient miss should leave a
  desk mislabeled UNKNOWN for up to a full minute.
- New `_identify_retry_interval_seconds()` method makes this decision
  explicitly and is unit-tested directly, rather than the interval being
  buried inline in the frame-processing loop.

**Deliberate scope decision, stated explicitly rather than assumed:**
only UNKNOWN gets the fast retry. MISMATCH is left on the normal 60s
baseline — it's a *confident* recognition, just of the wrong person for
that desk's assignment, not the "can't tell who this is" case the fast
retry exists for. Worth revisiting if you'd rather MISMATCH also
retry sooner (e.g., to confirm quickly whether it's a genuine desk-swap
or a fluke), but that's a different judgment call than the one you
described, so I didn't fold it in without asking.

**Kept separate from the VACANT path's own heartbeat**, which still uses
the fixed baseline unconditionally — that one governs how often to
re-publish/re-write a VACANT event while a desk sits empty, which has
nothing to do with identification confidence and shouldn't speed up or
slow down based on it.

## Verification

New test file `tests/real-app-smoke/test_recognition_cadence.py` (6 tests):

- Pure decision-logic tests: no prior result → normal baseline; MATCH →
  normal baseline; UNKNOWN → fast retry; MISMATCH → normal baseline
  (confirming the scope decision above is actually what's implemented,
  not just described); default `HEARTBEAT_SECONDS` is now 60.
- `test_unknown_desk_is_retried_more_often_than_a_matched_one` — a real,
  wall-clock integration test (not simulated time): two workstations on
  the same real synthetic video, one with a correct enrollment (always
  MATCH), one with no enrollment at all (always UNKNOWN, since the
  gallery lookup finds nothing to compare against). Both intervals
  monkeypatched small so the test completes in real seconds; confirms
  the UNKNOWN desk accumulates more identification-triggering DB events
  than the matched desk over the same run — proving the fast-retry path
  actually fires repeatedly in a real `StreamWorker` run, not just that
  the interval-selection function looks right in isolation.

Full regression, all passing:
- `tests/real-app-smoke`: **72/72** (66 previous + 6 new).
- `backend/tests/test_video_pipeline.py`: **17/17** (unaffected — this
  module has its own separate `HEARTBEAT_SECONDS`/identify logic, not
  touched by this change).
- `tests/design-acceptance-suite`: **454 passed, 22 skipped** (unaffected).

## Scope note

`video_export.py`'s own heartbeat logic (a separate copy, per its
existing "kept as a separate copy" design) was **not** given the same
fast-retry-on-UNKNOWN behavior in this step — it's a batch/offline
render, where retrying faster than the sampling stride
(`detect_every_n_frames`) wouldn't meaningfully change anything, since
there's no "real time passing while you wait" concern for a video that's
already fully available on disk. Flagging this as a deliberate
non-change, not an oversight.
