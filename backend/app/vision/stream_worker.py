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

Honest scope note: this was verified in this repository's own build
process against a real multi-frame video (a looped photo containing real,
detectable people -- see tests/real-app-smoke/test_stream_worker.py), not
against a live RTSP camera, since no camera hardware is available in a
sandboxed build environment. The RTSP/HTTP code path is unchanged
OpenCV.VideoCapture usage and should work identically against a real
camera URL; only the *source* differs between what was tested here and a
real deployment.
"""
import os
import tempfile
import threading
import time

import cv2

from app import database as db_module
from app.models import Workstation, WorkstationAssignment, WorkstationIdentityEvent, EmployeeFaceGallery, Employee
from app import logic
from app.vision import yolo_detector, face_embedder, event_bus

HEARTBEAT_SECONDS = int(os.environ.get("IDENTIFY_HEARTBEAT_SECONDS", 30))


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
        self._publish({
            "type": "event", "workstation_name": workstation_name, "event_type": event_type,
            "assigned_employee_id": assigned, "detected_employee_id": detected,
            "similarity": similarity, "snr": snr, "frame_number": self.frames_processed,
        })

    def _identify(self, db, frame_path: str, assigned_employee_id: str | None):
        """Runs the real matching pipeline: extract an embedding from the
        frame, single-pass match against the enrolled gallery (app/logic.py
        -- the same functions verified by the design-acceptance-suite),
        and gate the decision through decide_match_status."""
        try:
            live_embedding, _width_px = face_embedder.extract_embedding(frame_path)
        except ValueError:
            return "UNKNOWN", None, None, None  # occupied per YOLO, but no face found in the frame

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
                occupied = any(yolo_detector.boxes_overlap(p, roi) for p in people)
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
                    event_type, detected, similarity, snr = self._identify(db, tmp_path, assigned)
                    self._write_event(db, name, event_type, assigned, detected, similarity, snr)
                # else: still occupied, within the heartbeat window -- no
                # identification call this frame, matching the documented
                # "tens of calls per day, not per frame" design goal.
        finally:
            os.unlink(tmp_path)
