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
import os
import tempfile
import threading
import time

import cv2

from app import database as db_module
from app.models import Workstation, WorkstationAssignment, WorkstationIdentityEvent, EmployeeFaceGallery, Employee
from app import logic
from app.vision import yolo_detector, face_embedder, event_bus, frame_annotate, face_crop

HEARTBEAT_SECONDS = int(os.environ.get("IDENTIFY_HEARTBEAT_SECONDS", 30))

# Live frame preview is throttled independently of both the poll interval
# AND the identify heartbeat above: pushing a full base64 JPEG on every
# single processed frame would flood the WebSocket, especially for video
# analysis, which runs with poll_interval_seconds=0 (as fast as the CPU
# can decode+detect). This caps it to a steady, browser-friendly rate
# regardless of how fast frames are actually being processed underneath.
LIVE_FRAME_PUBLISH_FPS = float(os.environ.get("LIVE_FRAME_PUBLISH_FPS", 4))
LIVE_FRAME_MIN_INTERVAL_SECONDS = 1.0 / LIVE_FRAME_PUBLISH_FPS if LIVE_FRAME_PUBLISH_FPS > 0 else 0
LIVE_FRAME_JPEG_QUALITY = int(os.environ.get("LIVE_FRAME_JPEG_QUALITY", 70))


class StreamWorker(threading.Thread):
    def __init__(self, source: str, org_id: int, cam_id: int,
                 max_frames: int | None = None, poll_interval_seconds: float = 1.0,
                 stream_id: str | None = None, loop=None):
        super().__init__(daemon=True)
        self.source = source
        self.org_id = org_id
        self.cam_id = cam_id
        self.max_frames = max_frames
        self.poll_interval_seconds = poll_interval_seconds
        self.stream_id = stream_id  # used to route live events via event_bus; None = DB-only, no live push
        self.loop = loop            # the asyncio loop that started this worker, for thread-safe publishing

        self._stop_event = threading.Event()
        self.frames_processed = 0
        self.source_opened = False
        self._last_occupancy: dict[str, str] = {}       # workstation_name -> "ACTIVE"/"VACANT"
        self._last_identify_time: dict[str, float] = {}  # workstation_name -> monotonic time
        self._last_result: dict[str, tuple] = {}          # workstation_name -> (event_type, detected_employee_id, similarity), for live-frame labels between identify() calls
        self._last_frame_publish_time: float = -1e9

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
                self._process_frame(db, frame, rois)
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
            annotated, w, h, rois, self._last_occupancy, self._last_result, people
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
            return "UNKNOWN", None, None, None  # occupied per YOLO, but no face found in the cropped region
        finally:
            os.unlink(crop_path)

        gallery_rows = db.query(EmployeeFaceGallery).join(Employee).filter(Employee.org_id == self.org_id).all()
        gallery = {f"{row.employee_id}__{row.view}": row.get_embedding() for row in gallery_rows if row.embedding}
        grouped = logic.group_gallery_by_person(gallery)
        best = logic.match_single_pass(live_embedding, grouped)

        if best is None:
            return "UNKNOWN", None, None, None

        detected_employee_id, _view, similarity = best
        snr = similarity * 10  # placeholder scale pending a fitted per-employee decay curve
        status = logic.decide_match_status(
            occupancy_present=True, best_similarity=similarity, best_snr=snr,
            is_assigned_employee=(detected_employee_id == assigned_employee_id),
        )
        if status == logic.MatchStatus.UNKNOWN:
            detected_employee_id = None
        return status.value, detected_employee_id, similarity, snr

    def _process_frame(self, db, frame, rois: dict[str, dict]):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            cv2.imwrite(tmp.name, frame)
            tmp_path = tmp.name

        try:
            people = yolo_detector.detect_people(tmp_path)
            now = time.monotonic()

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
                # on a VACANT -> ACTIVE transition, or on a slow heartbeat
                # thereafter -- never on every frame.
                if status_changed or heartbeat_due:
                    self._last_identify_time[name] = now
                    event_type, detected, similarity, snr = self._identify(db, frame, occupying_person, assigned)
                    self._write_event(db, name, event_type, assigned, detected, similarity, snr)
                # else: still occupied, within the heartbeat window -- no
                # identification call this frame, matching the documented
                # "tens of calls per day, not per frame" design goal.

            # Live preview push: once per processed frame (covering every
            # workstation's current box/status together in one image),
            # throttled internally by _maybe_publish_frame regardless of
            # how many workstations are configured or how fast frames are
            # arriving.
            h, w = frame.shape[:2]
            self._maybe_publish_frame(frame, w, h, rois, people)
        finally:
            os.unlink(tmp_path)
