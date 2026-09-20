"""
Real stream-processing worker: opens an actual video source (an RTSP URL,
an HTTP stream, or a local video file -- OpenCV's VideoCapture handles all
three transparently) and runs the event-driven detection loop described in
Section 3.2 of the project documentation:

  - YOLO person detection runs on every processed frame to track each
    workstation ROI's occupancy (VACANT/ACTIVE).
  - Face identification does NOT run on every frame. It fires only on a
    VACANT -> ACTIVE transition, plus a slow heartbeat thereafter -- the
    same trigger design discussed at length for the ADAR merge, now
    actually implemented against a real (or file-based, for testing)
    video source instead of only against single uploaded stills.
  - A live annotated-frame preview (boxes + status drawn on the actual
    frame, JPEG-encoded, base64) is ALSO pushed over the same "frame"
    WebSocket message, throttled independently of the above (see
    LIVE_FRAME_MIN_INTERVAL_SECONDS) -- this is what lets both a live
    RTSP camera and a video-analysis run be actually *watched* live,
    not just read as a text event log. Drawing reuses
    app/vision/frame_annotate.py, the same code the "download annotated
    video" feature uses, so live and downloaded output match.

Honest scope note: this was verified in this repository's own build
process against a real multi-frame video (a looped photo containing real,
detectable people -- see tests/real-app-smoke/test_stream_worker.py), not
against a live RTSP camera, since no camera hardware is available in a
sandboxed build environment. The RTSP/HTTP code path is unchanged
OpenCV.VideoCapture usage and should work identically against a real
camera URL; only the *source* differs between what was tested here and a
real deployment. The same applies to the live frame push below: verified
against the synthetic test video's frames, not against real RTSP network
jitter/bandwidth.
"""
import base64
import math
import os
import tempfile
import threading
import time
from datetime import datetime

import cv2

from app import database as db_module
from app.models import Workstation, WorkstationAssignment, WorkstationIdentityEvent, EmployeeFaceGallery, Employee
from app import logic
from app.routers import attendance
from app.vision import yolo_detector, face_embedder, event_bus, frame_annotate, face_crop

HEARTBEAT_SECONDS = int(os.environ.get("IDENTIFY_HEARTBEAT_SECONDS", 60))

# When the last identification result for a desk was UNKNOWN -- occupied,
# but no confident match -- retry sooner than the normal heartbeat rather
# than leaving that desk mislabeled for up to a full HEARTBEAT_SECONDS.
# A face that briefly turned away, was poorly lit, or was too small in
# that one frame is often resolvable on a quick second look; there's no
# reason a transient miss should cost a full minute before it's
# re-checked. This is a documented placeholder, not calibrated against
# real footage -- tune once real deployment data shows how often a quick
# retry actually resolves an UNKNOWN vs. just repeating it.
LOW_CONFIDENCE_RETRY_SECONDS = int(os.environ.get("IDENTIFY_LOW_CONFIDENCE_RETRY_SECONDS", 5))

# Sentinel location value for AttendanceSegment rows representing "in the
# room, but not at any specific workstation" (you confirmed "in the room"
# means anywhere in this camera's frame). Distinguishes this from a real
# workstation name -- a real desk's configured name is never this literal
# string, so no genuine desk could ever collide with it.
ROOM_LOCATION = "ROOM"

# Live frame preview is throttled independently of both the poll interval
# AND the identify heartbeat above: pushing a full base64 JPEG on every
# single processed frame would flood the WebSocket, especially for video
# analysis, which runs with poll_interval_seconds=0 (as fast as the CPU
# can decode+detect). This caps it to a steady, browser-friendly rate
# regardless of how fast frames are actually being processed underneath.
LIVE_FRAME_PUBLISH_FPS = float(os.environ.get("LIVE_FRAME_PUBLISH_FPS", 4))
LIVE_FRAME_MIN_INTERVAL_SECONDS = 1.0 / LIVE_FRAME_PUBLISH_FPS if LIVE_FRAME_PUBLISH_FPS > 0 else 0
LIVE_FRAME_JPEG_QUALITY = int(os.environ.get("LIVE_FRAME_JPEG_QUALITY", 70))

# Activity classification (app/logic.py classify_activity(), decoupled
# from occupancy/identification per docs/100-key-points.md points 91-92):
# is_moving is derived from how far a tracked person's hip position (or,
# when the hip isn't confidently located, their bounding-box center)
# shifts per second. This threshold is a documented placeholder, not a
# calibrated constant -- it has not been validated against real footage
# at a known camera distance/resolution, and should be tuned once real
# footage is available, the same way sim_threshold/snr_threshold in
# logic.py are already flagged as needing real calibration data.
ACTIVITY_MOVING_THRESHOLD_PER_SEC = float(os.environ.get("ACTIVITY_MOVING_THRESHOLD_PER_SEC", 0.05))


class StreamWorker(threading.Thread):
    def __init__(self, source: str, org_id: int, cam_id: int,
                 max_frames: int | None = None, poll_interval_seconds: float = 1.0,
                 stream_id: str | None = None, loop=None, is_live: bool = False):
        super().__init__(daemon=True)
        self.source = source
        self.org_id = org_id
        self.cam_id = cam_id
        self.max_frames = max_frames
        self.poll_interval_seconds = poll_interval_seconds
        self.stream_id = stream_id  # used to route live events via event_bus; None = DB-only, no live push
        self.loop = loop            # the asyncio loop that started this worker, for thread-safe publishing
        # Gates which attendance tables a confirmed MATCH writes to
        # (app/routers/attendance.py record_detection_core vs.
        # record_simulated_detection_core). Defaults to False -- the SAFE
        # default -- because this same StreamWorker class, via
        # streams.py's start_worker(), is also what Video Analysis uses
        # to process an uploaded demo file (see that router's own
        # docstring: "the literal shared code path that makes 'same as
        # RTSP' true"). Without this flag defaulting safely, analyzing a
        # demo video would write real EmployeeAttendance/AttendanceSegment
        # rows -- fabricated sign-in times, fabricated desk time -- into
        # the exact same tables real attendance reporting reads from.
        # When False, attendance is instead written to the fully separate
        # SimulatedAttendance/SimulatedAttendanceSegment tables (item 8),
        # never the real ones. Only a genuinely live camera stream should
        # ever set this True; both real call sites (streams.py, videos.py)
        # now do so explicitly, not by relying on this default.
        self.is_live = is_live

        self._stop_event = threading.Event()
        self.frames_processed = 0
        self.source_opened = False
        self._last_occupancy: dict[str, str] = {}       # workstation_name -> "ACTIVE"/"VACANT"
        self._last_identify_time: dict[str, float] = {}  # workstation_name -> monotonic time
        self._last_result: dict[str, tuple] = {}          # workstation_name -> (event_type, detected_employee_id, similarity), for live-frame labels between identify() calls
        self._last_frame_publish_time: float = -1e9
        self._last_position: dict[int, tuple[float, float, float]] = {}  # track_id -> (x_norm, y_norm, monotonic_time), for is_moving
        self._last_activity: dict[int, str] = {}          # track_id -> Activity.value, so activity is only published on a genuine change
        self._last_identity_by_track: dict[int, tuple] = {}  # track_id -> (event_type, detected_employee_id, employee_name, similarity), so a person's own box label follows them, not their workstation's rectangle
        self._last_employee_name: dict[str, str | None] = {}  # workstation_name -> employee_name from the most recent MATCH/MISMATCH, paired with _last_result
        self._last_room_record_time: dict[int, float] = {}  # track_id -> monotonic time, heartbeat for "in room, not at desk" attendance segments

    def stop(self):
        self._stop_event.set()

    def _publish(self, message: dict):
        if self.stream_id and self.loop:
            event_bus.publish(self.stream_id, message, self.loop)

    def run(self):
        cap = cv2.VideoCapture(self.source)
        self.source_opened = cap.isOpened()
        if not self.source_opened:
            print(f"[stream_worker] Could not open source: {self.source}")
            self._publish({"type": "error", "message": f"Could not open source: {self.source}"})
            self._publish({"type": "completed", "frames_processed": 0, "reason": "source_not_opened"})
            return

        # A model instance PRIVATE to this worker/this one video source --
        # never the shared yolo_detector.get_model() singleton -- so this
        # stream's tracker state (assigned track IDs, motion history)
        # never collides with another concurrent or sequential stream's.
        # Allocated only once the source is confirmed open, so a bad
        # source URL doesn't pay a model-load cost for nothing. See
        # yolo_detector.new_model_instance()'s docstring.
        pose_model = yolo_detector.new_model_instance()

        db = db_module.SessionLocal()
        try:
            rois = self._load_rois(db)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)  # 0/unreliable for live RTSP -- that's expected
            self._publish({"type": "started", "org_id": self.org_id, "cam_id": self.cam_id,
                            "workstations": list(rois.keys()), "total_frames": total_frames or None})

            consecutive_failures = 0
            max_consecutive_failures = 5  # tolerates transient RTSP/decoder hiccups without treating them as end-of-stream
            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if not ok:
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        break  # sustained failure -- source genuinely ended or dropped
                    time.sleep(0.2)
                    continue
                consecutive_failures = 0
                self._process_frame(db, frame, rois, pose_model)
                self.frames_processed += 1
                self._publish({"type": "progress", "frames_processed": self.frames_processed,
                                "total_frames": total_frames or None})
                if self.max_frames is not None and self.frames_processed >= self.max_frames:
                    break
                if self.poll_interval_seconds > 0:
                    time.sleep(self.poll_interval_seconds)
        finally:
            cap.release()
            db.close()
            reason = "stopped" if self._stop_event.is_set() else "source_ended"
            self._publish({"type": "completed", "frames_processed": self.frames_processed, "reason": reason})

    def _load_rois(self, db) -> dict[str, dict]:
        rows = db.query(Workstation).filter_by(org_id=self.org_id, cam_id=self.cam_id).all()
        return {r.name: {"x1": r.x1, "y1": r.y1, "x2": r.x2, "y2": r.y2} for r in rows}

    def _assigned_employee(self, db, workstation_name: str) -> str | None:
        row = (db.query(WorkstationAssignment)
               .filter_by(org_id=self.org_id, cam_id=self.cam_id, workstation_name=workstation_name, effective_to=None)
               .order_by(WorkstationAssignment.effective_from.desc()).first())
        return row.employee_id if row else None

    def _write_event(self, db, workstation_name, event_type, assigned, detected, similarity, snr):
        event = WorkstationIdentityEvent(
            org_id=self.org_id, cam_id=self.cam_id, workstation_name=workstation_name,
            event_type=event_type, assigned_employee_id=assigned, detected_employee_id=detected,
            similarity=similarity, snr=snr,
        )
        db.add(event)
        db.commit()
        # Cached so _maybe_publish_frame can keep labeling the live
        # preview correctly on frames where identify() doesn't run this
        # cycle (mirrors video_export.annotate_video()'s last_result
        # cache, which does the same for the downloaded-video path).
        self._last_result[workstation_name] = (event_type, detected, similarity)
        self._publish({
            "type": "event", "workstation_name": workstation_name, "event_type": event_type,
            "assigned_employee_id": assigned, "detected_employee_id": detected,
            "similarity": similarity, "snr": snr, "frame_number": self.frames_processed,
        })

    def _maybe_publish_frame(self, frame, w: int, h: int, rois: dict[str, dict], people: list):
        """Throttled live preview: draws the current occupancy/identity
        state onto a COPY of the frame (never mutates the frame the
        caller still needs) and pushes it as a "frame" WebSocket message,
        at most LIVE_FRAME_PUBLISH_FPS times per second regardless of how
        fast frames are actually being processed. `people` is this
        frame's own freshly-computed YOLO detections (detection already
        runs on every processed frame, unlike identify(), so this needs
        no cross-frame caching the way _last_result does). A no-op when
        this worker has no stream_id/loop (e.g. a plain synchronous run
        with no live subscriber) -- _publish() already handles that, but
        the JPEG encode is skipped too in that case rather than wasted."""
        if not (self.stream_id and self.loop):
            return
        now = time.monotonic()
        if (now - self._last_frame_publish_time) < LIVE_FRAME_MIN_INTERVAL_SECONDS:
            return
        self._last_frame_publish_time = now

        annotated = frame.copy()
        frame_annotate.draw_annotations(
            annotated, w, h, rois, self._last_occupancy, self._last_result, people, self._last_identity_by_track
        )
        ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, LIVE_FRAME_JPEG_QUALITY])
        if not ok:
            return  # encode failure (extremely unlikely for a valid frame) -- just skip this preview tick
        encoded = base64.b64encode(buf).decode("ascii")
        self._publish({
            "type": "frame", "frame_number": self.frames_processed,
            "image": f"data:image/jpeg;base64,{encoded}",
        })

    def _identify(self, db, frame, occupying_person, assigned_employee_id: str | None):
        """Runs the real matching pipeline: extract an embedding from a
        crop of the frame around the specific person occupying this ROI
        (app/vision/face_crop.py -- the same crop-to-person-box fix
        already wired into workstations.py's simulate_detection, applied
        here to the live/video-analysis path for the first time), single-
        pass match against the enrolled gallery (app/logic.py -- the same
        functions verified by the design-acceptance-suite), and gate the
        decision through decide_match_status.

        Cropping to `occupying_person` rather than passing the full frame
        fixes two real problems at once, not just one:
          1. The one this fix was built for (100-key-points.md point 30):
             a person who is a small part of a wide shot has a
             proportionally tiny face after InsightFace's internal
             det_size=(320,320) resize, which can fail detection entirely
             even though the person is clearly, visibly present.
          2. A second, previously-unnoticed correctness bug: with more
             than one occupied workstation in the same camera frame, the
             old code passed the SAME full frame to every workstation's
             _identify() call, so InsightFace's arbitrary "first face
             found" (faces[0]) had no way to know which detected face
             belonged to THIS workstation's occupant. Cropping to the
             specific person `best_overlapping_person` resolved for this
             ROI (see _process_frame) makes each call see only the face
             that can possibly be relevant to it.
        """
        crop_path = face_crop.crop_person_region(frame, occupying_person)
        try:
            live_embedding, _width_px = face_embedder.extract_embedding(crop_path)
        except ValueError:
            return "UNKNOWN", None, None, None, None  # occupied per YOLO, but no face found in the cropped region
        finally:
            os.unlink(crop_path)

        gallery_rows = db.query(EmployeeFaceGallery).join(Employee).filter(Employee.org_id == self.org_id).all()
        gallery = {f"{row.employee_id}__{row.view}": row.get_embedding() for row in gallery_rows if row.embedding}
        grouped = logic.group_gallery_by_person(gallery)
        best = logic.match_single_pass(live_embedding, grouped)

        if best is None:
            return "UNKNOWN", None, None, None, None

        detected_employee_id, _view, similarity = best
        employee_name = next((row.employee.name for row in gallery_rows if row.employee_id == detected_employee_id), None)
        snr = similarity * 10  # placeholder scale pending a fitted per-employee decay curve
        status = logic.decide_match_status(
            occupancy_present=True, best_similarity=similarity, best_snr=snr,
            is_assigned_employee=(detected_employee_id == assigned_employee_id),
        )
        if status == logic.MatchStatus.UNKNOWN:
            detected_employee_id = None
        return status.value, detected_employee_id, similarity, snr, employee_name

    def _update_position_and_check_moving(self, person, now: float) -> bool:
        """Tracks this person's approximate body position across frames
        (keyed by their persistent track_id) to derive is_moving for
        classify_activity(). Prefers the hip position (more representative
        of body movement than a bounding-box center, which shifts with
        pose/arm position even when someone is stationary) when it's
        confidently located; falls back to the box center otherwise. A
        person's first-seen frame always returns False -- movement can
        only be judged from a second sample, not a first."""
        hip_c, hip_x, hip_y = (0.0, 0.0, 0.0)
        if person.keypoints:
            hip_x, hip_y, hip_c = logic.combine_side_pair(person.keypoints, "left_hip", "right_hip")
        if hip_c >= logic.MIN_KEYPOINT_CONFIDENCE:
            cx, cy = hip_x, hip_y
        else:
            cx, cy = (person.x1 + person.x2) / 2, (person.y1 + person.y2) / 2

        prior = self._last_position.get(person.track_id)
        self._last_position[person.track_id] = (cx, cy, now)
        if prior is None:
            return False
        px, py, pt = prior
        dt = now - pt
        if dt <= 0:
            return False
        distance = math.hypot(cx - px, cy - py)
        return (distance / dt) > ACTIVITY_MOVING_THRESHOLD_PER_SEC

    def _classify_and_publish_activity(self, people: list, now: float):
        """Runs the already-tested classify_activity() (app/logic.py)
        against every tracked person in the frame -- not just workstation
        occupants -- since per 100-key-points.md points 91-92 this is
        deliberately decoupled from occupancy detection and identity
        recognition. Its real value is characterizing people who are NOT
        at any desk (standing, walking, in a group discussion) just as
        much as those who are, so it is not scoped to per-ROI occupants
        the way _identify() is.

        Published only on a genuine per-track change (mirrors the
        occupancy debounce pattern in _process_frame) -- not every frame,
        to avoid flooding the live feed the same way undebounced VACANT
        events once did.
        """
        for person in people:
            if person.track_id is None or not person.keypoints:
                continue
            is_moving = self._update_position_and_check_moving(person, now)
            keypoint_confidence, torso_angle_deg = logic.activity_inputs_from_coco_keypoints(person.keypoints)
            activity = logic.classify_activity(keypoint_confidence, torso_angle_deg, is_moving)

            if self._last_activity.get(person.track_id) != activity.value:
                self._last_activity[person.track_id] = activity.value
                self._publish({
                    "type": "activity", "track_id": person.track_id,
                    "activity": activity.value, "frame_number": self.frames_processed,
                })

    def _identify_retry_interval_seconds(self, workstation_name: str) -> float:
        """The baseline is HEARTBEAT_SECONDS per occupied desk -- but if
        the last identification result for this desk was UNKNOWN, retry
        after LOW_CONFIDENCE_RETRY_SECONDS instead (see that constant's
        docstring for why). A fresh desk with no prior result yet uses
        the normal baseline -- it doesn't matter in practice, since a
        desk with no prior result is always mid-transition and gets
        identified immediately via status_changed regardless of this
        value, but returning something sane rather than a special case
        keeps this function simple to reason about on its own."""
        last_event_type = self._last_result.get(workstation_name, (None, None, None))[0]
        if last_event_type == "UNKNOWN":
            return LOW_CONFIDENCE_RETRY_SECONDS
        return HEARTBEAT_SECONDS

    def _record_room_presence_for_roaming_people(self, db, people: list, occupied_track_ids: set, now: float):
        """Item 7: room-vs-desk time split. A tracked person who is NOT
        currently occupying any workstation ROI is, per your own
        confirmation, still "in the room" (this camera's whole frame IS
        the room). If that person has a KNOWN identity -- from a previous
        confirmed MATCH at some desk, cached in _last_identity_by_track --
        this records their room presence too, using the same
        record_detection_core() attendance path but with location=
        ROOM_LOCATION instead of a workstation name.

        Deliberately does NOT run a fresh face-recognition call for
        roaming people: that would mean identifying every person visible
        anywhere in frame, every heartbeat, which is a much larger and
        more expensive surface than the whole rest of this system's
        "identify rarely, at a desk, event-driven" design. Using the
        cached last-known identity for an already-tracked person is a
        reasonable, honestly-scoped middle ground: it can only ever
        report someone who was ALREADY confirmed at a desk at some point
        in this session, not a stranger who merely walked through frame.

        Heartbeat-gated per track_id (same HEARTBEAT_SECONDS baseline as
        desk identification). Writes to the real attendance tables when
        self.is_live, or the simulated ones (item 8) otherwise -- unlike
        the MATCH path below, this never skips entirely: a Video Analysis
        run should show room presence in its own simulated view too, not
        silently drop it.
        """
        for person in people:
            if person.track_id is None or person.track_id in occupied_track_ids:
                continue
            identity = self._last_identity_by_track.get(person.track_id)
            if not identity or identity[0] != "MATCH" or not identity[1]:
                continue  # never identified, or last known result wasn't a confident match

            last_recorded = self._last_room_record_time.get(person.track_id, -1e9)
            if (now - last_recorded) < HEARTBEAT_SECONDS:
                continue
            self._last_room_record_time[person.track_id] = now

            detected_employee_id = identity[1]
            record_fn = attendance.record_detection_core if self.is_live else attendance.record_simulated_detection_core
            try:
                record_fn(
                    db, self.org_id, detected_employee_id, cam_id=self.cam_id,
                    location=ROOM_LOCATION, timestamp=datetime.utcnow(),
                )
            except Exception as e:
                print(f"[stream_worker] room-presence recording failed for {detected_employee_id}: {e}")

    def _process_frame(self, db, frame, rois: dict[str, dict], pose_model):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            cv2.imwrite(tmp.name, frame)
            tmp_path = tmp.name

        try:
            people = yolo_detector.detect_and_track_people(pose_model, tmp_path)
            now = time.monotonic()
            occupied_track_ids = set()  # track_ids currently occupying SOME workstation this frame -- excluded from the room-presence pass below (item 7), since they're already covered as desk time, not room time

            for name, roi in rois.items():
                # best_overlapping_person applies the same overlap gate as
                # boxes_overlap (used everywhere else in this loop) but
                # also resolves WHICH person occupies this specific ROI,
                # which _identify needs to crop to the right face -- see
                # its docstring for why that matters even in the
                # single-workstation case, and doubly so with more than
                # one occupied desk in frame.
                occupying_person = face_crop.best_overlapping_person(people, roi)
                occupied = occupying_person is not None
                if occupied and occupying_person.track_id is not None:
                    occupied_track_ids.add(occupying_person.track_id)
                new_status = "ACTIVE" if occupied else "VACANT"
                previous_status = self._last_occupancy.get(name)  # None on the very first frame seen
                self._last_occupancy[name] = new_status
                assigned = self._assigned_employee(db, name)

                status_changed = previous_status != new_status  # True on the first frame too
                heartbeat_due = (now - self._last_identify_time.get(name, -1e9)) >= HEARTBEAT_SECONDS

                if not occupied:
                    # Previously wrote+published a VACANT event on EVERY
                    # not-occupied frame, with no de-duplication -- for a
                    # multi-minute video at poll_interval_seconds=0 this
                    # floods both the DB and the WebSocket with thousands
                    # of identical lines, which is what made the frontend
                    # event log look frozen/unresponsive. Now debounced
                    # exactly like the ACTIVE/identify path below: only on
                    # a real transition, or the same slow heartbeat.
                    if status_changed or heartbeat_due:
                        self._last_identify_time[name] = now
                        self._write_event(db, name, "VACANT", assigned, None, None, None)
                    continue

                # Event-driven identification trigger (Section 3.2): fire
                # on a VACANT -> ACTIVE transition, or on a heartbeat
                # thereafter -- never on every frame. The heartbeat
                # interval is dynamic: the normal baseline, or a faster
                # retry if the last result here was UNKNOWN -- see
                # _identify_retry_interval_seconds(). Deliberately a
                # SEPARATE check from the VACANT path's heartbeat_due
                # above: that one governs how often to re-publish/re-write
                # a VACANT event (unrelated to identification confidence)
                # and must stay on the fixed baseline, not the dynamic one.
                identify_retry_due = (now - self._last_identify_time.get(name, -1e9)) \
                    >= self._identify_retry_interval_seconds(name)
                if status_changed or identify_retry_due:
                    self._last_identify_time[name] = now
                    event_type, detected, similarity, snr, employee_name = self._identify(db, frame, occupying_person, assigned)
                    self._write_event(db, name, event_type, assigned, detected, similarity, snr)
                    self._last_employee_name[name] = employee_name

                    # Attendance recording -- real tables for a genuinely
                    # live stream, simulated tables (item 8) otherwise --
                    # and only on an actual confirmed MATCH, never on
                    # MISMATCH or UNKNOWN: attendance is about tracking a
                    # real (or, for a demo run, a real-looking simulated)
                    # employee presence, not logging every inconclusive
                    # glance at a desk.
                    if event_type == "MATCH" and detected:
                        record_fn = attendance.record_detection_core if self.is_live else attendance.record_simulated_detection_core
                        try:
                            record_fn(
                                db, self.org_id, detected, cam_id=self.cam_id,
                                location=name, timestamp=datetime.utcnow(),
                            )
                        except Exception as e:
                            # Attendance recording must never take down
                            # the detection loop itself -- a DB hiccup
                            # here shouldn't stop occupancy/identification
                            # from continuing to work and being logged.
                            print(f"[stream_worker] attendance recording failed for {detected} at {name}: {e}")
                # else: still occupied, within the current interval -- no
                # identification call this frame, matching the documented
                # "tens of calls per day, not per frame" design goal.

                # Re-associate this workstation's latest known identity
                # result with whichever track_id currently occupies it --
                # runs on EVERY occupied frame, not only when _identify()
                # itself just ran above. This matters because a brand-new
                # track's ID is often still unconfirmed (None) on the
                # exact frame _identify() fires: verified directly that
                # Ultralytics' ByteTrack only confirms an ID from a
                # track's SECOND seen frame onward, not its first. Without
                # this being separate from the identify() branch above,
                # the very first identification for a newly-arrived
                # person would silently never reach the display -- lost
                # to a one-frame timing gap, not a logic error as such.
                if occupying_person.track_id is not None:
                    event_type, detected, similarity = self._last_result.get(name, ("VACANT", None, None))
                    self._last_identity_by_track[occupying_person.track_id] = (
                        event_type, detected, self._last_employee_name.get(name), similarity)

            # Item 7 (room-vs-desk time split): anyone tracked but NOT
            # occupying a workstation this frame is still "in the room"
            # (per your confirmation: this camera's whole frame IS the
            # room) -- record that too, for anyone with a known identity.
            self._record_room_presence_for_roaming_people(db, people, occupied_track_ids, now)

            # Activity classification: decoupled from occupancy/identity
            # (see _classify_and_publish_activity's docstring) -- runs
            # against every tracked person in frame, not just workstation
            # occupants.
            self._classify_and_publish_activity(people, now)

            # Live preview push: once per processed frame (covering every
            # workstation's current box/status together in one image),
            # throttled internally by _maybe_publish_frame regardless of
            # how many workstations are configured or how fast frames are
            # arriving.
            h, w = frame.shape[:2]
            self._maybe_publish_frame(frame, w, h, rois, people)
        finally:
            os.unlink(tmp_path)
