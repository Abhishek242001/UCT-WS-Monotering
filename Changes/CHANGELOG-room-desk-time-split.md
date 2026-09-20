# Changelog: item 7 — room-vs-desk time split

## What changed

`app/vision/stream_worker.py` — a tracked person who is NOT currently
occupying any workstation ROI, but has a known identity from a previous
confirmed MATCH at a desk (cached in `_last_identity_by_track` since an
earlier round of this work), now gets their room presence recorded too —
via the same `record_detection_core()` attendance path used for desk
time, but with a new sentinel location, `ROOM_LOCATION = "ROOM"`, instead
of a workstation name.

This directly answers the room-vs-desk question from earlier: since you
confirmed "in the room" means anywhere in this camera's frame, and a
frame can hold multiple desks, "room time" is now: anyone with a known
identity, visible in frame, not currently at any of those desks.

## Deliberate design choice, stated explicitly

**No fresh face recognition runs for roaming people.** Identifying
everyone visible anywhere in frame, on a heartbeat, would be a much
larger and more expensive surface than the "identify rarely, at a desk,
event-driven" design the rest of this system follows. Instead, a
roaming person is only recorded if they were **already** confirmed at
some desk earlier in the session — using their cached last-known
identity. This is an honest, bounded claim: it can only ever report
someone who genuinely was identified at some point, never a stranger who
merely walked through frame. If your actual use case needs to recognize
someone who arrives and immediately roams without ever sitting at a
desk, that's a different, larger feature (running identification on
every tracked person, not just desk occupants) — not something this step
quietly assumes away.

Someone currently AT a desk is excluded from the room pass — that time
is already counted as desk time, not double-counted as both. Gated on
`self.is_live`, same as every other attendance write in this file, and
heartbeat-gated per track_id at the same `HEARTBEAT_SECONDS` baseline as
desk identification, so it doesn't write on every frame.

## Verification

New test file `tests/real-app-smoke/test_room_presence.py` (6 tests):

- A roaming, previously-identified person gets a real `ROOM` segment.
- Someone currently at a desk is correctly excluded (no double-counting).
- Someone never identified is correctly excluded (no fresh recognition
  invented for them).
- `is_live=False` (Video Analysis) is correctly excluded.
- Two calls within the heartbeat window produce only one segment, not
  two.
- `test_process_frame_records_room_presence_for_a_desk_no_one_occupies`
  — drives the **real** `_process_frame()` method directly (not the
  standalone function in isolation), with a workstation ROI the real
  photo's detections don't overlap at all, confirming the actual wiring
  inside `_process_frame` (building `occupied_track_ids`, calling the
  new pass) is correct, not just that the underlying function works when
  called with hand-picked arguments.

**Test setup honesty note**: the bundled real test photo is static (same
person position in every frame), so a short synthetic video can't
naturally show someone "walking away from a desk." Most of this suite
tests the recording logic directly with a real db session rather than
staging that motion; only the last test exercises the full per-frame
method, with pre-seeded identity state standing in for "was already
matched earlier in the session" rather than reproducing that from
scratch (already covered by earlier tests in this round of work).

Full regression, all passing:
- `tests/real-app-smoke`: **85/85** (79 previous + 6 new).
- `backend/tests/test_video_pipeline.py`: **17/17** (unaffected).
- `tests/design-acceptance-suite`: **454 passed, 22 skipped** (unaffected).

## Scope note

- `AttendanceSegment.department_or_workstation = "ROOM"` is a plain
  string sentinel, not a new schema column — reporting/UI code that reads
  segments should treat this value specially (e.g. "In room" instead of
  a desk name) rather than assuming every segment's location is a real
  workstation. No reporting endpoint currently does this yet — that's
  frontend/reporting work, not part of this step.
- This does not yet cross-reference the activity classifier
  (SITTING/STANDING/WALKING) at all — room time is currently just "known
  identity, not at a desk," regardless of whether they're standing still
  or walking around. Combining the two (e.g., only counting WALKING/
  STANDING as room time, not counting a moment mid-transition) would be
  a further refinement, not done here.
