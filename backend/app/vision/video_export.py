"""
Batch video export: (1) a fast raw clip extractor, and (2) a detection-
annotated renderer that draws what the live StreamWorker pipeline would
have decided directly onto the video frames, for download.

Why this exists: StreamWorker (see stream_worker.py) only ever pushes
JSON events (VACANT/ACTIVE, identity, similarity) back to the browser --
it never streams frame images. There is currently no way to *see* the
detection boxes, ROI boxes, or identity result overlaid on footage
anywhere in this project. This module produces that as a downloadable
file instead, reusing the exact same detection/identify logic
(yolo_detector, boxes_overlap, face_embedder, app.logic) StreamWorker
uses, so what's drawn here matches what the live pipeline would decide --
same confidence threshold, same overlap rule, same identify() gating.

Honest scope notes:
 - This was exercised in this sandbox against a short synthetic clip
   with cv2/ultralytics installed and a stub face backend; it has NOT
   been run against a real multi-minute CCTV export or verified with a
   real (non-stub) face model, since neither is available here.
 - Only 'mp4v' (MPEG-4 Part 2) is confirmed available via OpenCV's
   VideoWriter in this sandbox -- most browsers will NOT play that
   natively in an HTML5 <video> tag, only download/open-elsewhere. If an
   `ffmpeg` binary is on PATH, we re-encode to H.264 afterward for real
   browser playback; if ffmpeg isn't available on your deployment, the
   file still downloads correctly, just won't preview in-browser.
 - YOLO inference on every single frame of a multi-minute video is slow
   on CPU (no GPU assumed) -- callers should cap max_seconds and can
   raise detect_every_n_frames to trade a choppier detection update rate
   for speed; frames between detections keep the last known boxes/status
   so the output video itself still plays back smoothly.
"""
import os
import shutil
import subprocess
import tempfile
import time

import cv2

from app import logic
from app.models import Workstation, WorkstationAssignment, EmployeeFaceGallery, Employee
from app.vision import yolo_detector, face_embedder

HEARTBEAT_SECONDS = int(os.environ.get("IDENTIFY_HEARTBEAT_SECONDS", 30))

_PERSON_COLOR = (60, 200, 60)     # BGR: green
_ROI_COLOR = (255, 130, 40)       # BGR: blue-orange
_ACTIVE_COLOR = (0, 200, 0)       # BGR: green
_VACANT_COLOR = (60, 60, 220)     # BGR: red


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def extract_clip(source_path: str, dest_path: str, max_seconds: int) -> dict:
    """Writes the first `max_seconds` of source_path to dest_path.

    Prefers an ffmpeg stream-copy (no re-encoding -- exact original
    quality, keeps audio, fast regardless of video length) when ffmpeg is
    on PATH. Falls back to a frame-by-frame OpenCV copy (video only, no
    audio, re-encoded as mp4v) if ffmpeg is unavailable, or if the
    stream-copy fails (can happen if the cut point lands mid-GOP for some
    codecs)."""
    if _ffmpeg_available():
        cmd = ["ffmpeg", "-y", "-i", source_path, "-t", str(max_seconds),
               "-c", "copy", "-movflags", "+faststart", dest_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return {"method": "ffmpeg_stream_copy", "has_audio": True}
        # fall through to the OpenCV path below rather than fail outright

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        raise ValueError(f"Could not open source video: {source_path}")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        writer = cv2.VideoWriter(dest_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        max_frames = int(fps * max_seconds)
        n = 0
        try:
            while n < max_frames:
                ok, frame = cap.read()
                if not ok:
                    break
                writer.write(frame)
                n += 1
        finally:
            writer.release()
    finally:
        cap.release()
    return {"method": "opencv_reencode", "has_audio": False}


def _load_rois(db, org_id: int, cam_id: int) -> dict[str, dict]:
    rows = db.query(Workstation).filter_by(org_id=org_id, cam_id=cam_id).all()
    return {r.name: {"x1": r.x1, "y1": r.y1, "x2": r.x2, "y2": r.y2} for r in rows}


def _assigned_employee(db, org_id: int, cam_id: int, name: str) -> str | None:
    row = (db.query(WorkstationAssignment)
           .filter_by(org_id=org_id, cam_id=cam_id, workstation_name=name, effective_to=None)
           .order_by(WorkstationAssignment.effective_from.desc()).first())
    return row.employee_id if row else None


def _identify(db, org_id: int, frame_path: str, assigned_employee_id: str | None):
    """Same logic as StreamWorker._identify -- kept as a separate copy
    here (rather than importing StreamWorker's method) since that method
    is bound to a live worker's per-stream state; this module runs in a
    batch/offline context with its own frame-level state instead."""
    try:
        live_embedding, _w = face_embedder.extract_embedding(frame_path)
    except ValueError:
        return "UNKNOWN", None, None

    gallery_rows = db.query(EmployeeFaceGallery).join(Employee).filter(Employee.org_id == org_id).all()
    gallery = {f"{row.employee_id}__{row.view}": row.get_embedding() for row in gallery_rows if row.embedding}
    grouped = logic.group_gallery_by_person(gallery)
    best = logic.match_single_pass(live_embedding, grouped)
    if best is None:
        return "UNKNOWN", None, None

    detected_employee_id, _view, similarity = best
    snr = similarity * 10
    status = logic.decide_match_status(
        occupancy_present=True, best_similarity=similarity, best_snr=snr,
        is_assigned_employee=(detected_employee_id == assigned_employee_id),
    )
    if status == logic.MatchStatus.UNKNOWN:
        detected_employee_id = None
    return status.value, detected_employee_id, similarity


def annotate_video(source_path: str, dest_path: str, db, org_id: int, cam_id: int,
                    max_seconds: int = 60, detect_every_n_frames: int = 3) -> dict:
    """Renders ROI boxes, detected-person boxes, and occupancy/identity
    status onto up to max_seconds of source_path, writing the result to
    dest_path. Raises ValueError if there's no saved ROI for org_id/cam_id
    (nothing meaningful to draw) or if the source can't be opened."""
    rois = _load_rois(db, org_id, cam_id)
    if not rois:
        raise ValueError(
            f"No saved workstation ROI for org_id={org_id}, cam_id={cam_id} -- "
            "draw one on the Workstations tab first, or check the camera/org ID."
        )
    if detect_every_n_frames < 1:
        raise ValueError("detect_every_n_frames must be >= 1")

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        raise ValueError(f"Could not open source video: {source_path}")

    raw_path = dest_path + ".raw.mp4"
    writer = None
    last_status: dict[str, str] = {}
    last_result: dict[str, tuple] = {}       # name -> (event_type, detected_employee_id, similarity)
    last_identify_time: dict[str, float] = {}
    last_people = []
    frame_idx = 0
    start = time.monotonic()

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        max_frames = int(fps * max_seconds)
        writer = cv2.VideoWriter(raw_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))

        while frame_idx < max_frames:
            ok, frame = cap.read()
            if not ok:
                break

            if frame_idx % detect_every_n_frames == 0:
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    cv2.imwrite(tmp.name, frame)
                    tmp_path = tmp.name
                try:
                    last_people = yolo_detector.detect_people(tmp_path)
                    now = time.monotonic()
                    for name, roi in rois.items():
                        occupied = any(yolo_detector.boxes_overlap(p, roi) for p in last_people)
                        new_status = "ACTIVE" if occupied else "VACANT"
                        previous_status = last_status.get(name)
                        last_status[name] = new_status
                        if not occupied:
                            last_result[name] = ("VACANT", None, None)
                            continue
                        assigned = _assigned_employee(db, org_id, cam_id, name)
                        became_active = previous_status != "ACTIVE"
                        heartbeat_due = (now - last_identify_time.get(name, -1e9)) >= HEARTBEAT_SECONDS
                        if became_active or heartbeat_due:
                            last_identify_time[name] = now
                            last_result[name] = _identify(db, org_id, tmp_path, assigned)
                finally:
                    os.unlink(tmp_path)

            # Draw on every frame using the latest cached detection --
            # keeps output video smooth even when detect_every_n_frames > 1.
            for p in last_people:
                x1, y1 = int(p.x1 * w), int(p.y1 * h)
                x2, y2 = int(p.x2 * w), int(p.y2 * h)
                cv2.rectangle(frame, (x1, y1), (x2, y2), _PERSON_COLOR, 2)
                cv2.putText(frame, f"person {p.confidence:.2f}", (x1, max(12, y1 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, _PERSON_COLOR, 1, cv2.LINE_AA)

            banner_y = 24
            for name, roi in rois.items():
                rx1, ry1 = int(roi["x1"] * w), int(roi["y1"] * h)
                rx2, ry2 = int(roi["x2"] * w), int(roi["y2"] * h)
                status = last_status.get(name, "VACANT")
                color = _ACTIVE_COLOR if status == "ACTIVE" else _VACANT_COLOR
                cv2.rectangle(frame, (rx1, ry1), (rx2, ry2), _ROI_COLOR, 2)

                event_type, detected, similarity = last_result.get(name, ("VACANT", None, None))
                box_label = f"{name}: {status}"
                if status == "ACTIVE" and event_type != "ACTIVE":
                    box_label += f" | {event_type}"
                    if detected:
                        box_label += f" ({detected}, sim={similarity:.2f})"
                cv2.putText(frame, box_label, (rx1, max(12, ry1 - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)

                cv2.putText(frame, box_label, (10, banner_y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
                banner_y += 24

            writer.write(frame)
            frame_idx += 1
    finally:
        cap.release()
        if writer is not None:
            writer.release()

    elapsed = time.monotonic() - start
    playable_in_browser = False
    if _ffmpeg_available():
        cmd = ["ffmpeg", "-y", "-i", raw_path, "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", dest_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            playable_in_browser = True
            os.remove(raw_path)
    if not playable_in_browser:
        # No ffmpeg, or the re-encode failed for some reason -- ship the
        # OpenCV-native mp4v file as-is rather than fail the whole request.
        shutil.move(raw_path, dest_path)

    return {
        "frames_processed": frame_idx,
        "seconds_processed": round(frame_idx / fps, 1) if fps else None,
        "processing_time_seconds": round(elapsed, 1),
        "playable_in_browser": playable_in_browser,
        "workstations": list(rois.keys()),
    }
