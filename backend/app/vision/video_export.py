"""
Batch video export: (1) a fast raw clip extractor, and (2) a detection-
annotated renderer that draws what the live StreamWorker pipeline would
have decided directly onto the video frames, for download.

The actual box/ROI/status drawing is shared with the live pipeline via
app/vision/frame_annotate.py (see that module for why) -- this module
still owns the render loop, YOLO/identify calls, and video I/O.

Note: StreamWorker (see stream_worker.py) now ALSO pushes live annotated
frames over the "frame" WebSocket message (added alongside this refactor)
using the same frame_annotate.draw_annotations() this module calls below
-- so what you see live and what this module renders for download match,
from one drawing implementation, not two that could drift apart.

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
from app.vision import yolo_detector, face_embedder, frame_annotate

HEARTBEAT_SECONDS = int(os.environ.get("IDENTIFY_HEARTBEAT_SECONDS", 30))


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def sample_diagnostics(source_path: str, db, org_id: int, cam_id: int, num_samples: int = 8) -> dict:
    """Fast diagnostic pass: spreads num_samples frames evenly across the
    ENTIRE video (not just the start, unlike annotate_video), runs real
    YOLO detection on each, and reports what it actually found -- person
    counts, confidence scores, and whether any detection overlapped the
    saved ROI -- plus the real per-call YOLO latency measured on THIS
    server. Exists specifically to answer two questions cheaply (seconds,
    not minutes) before committing to a full annotate_video render:
      1. Is YOLO detecting people in this footage at all, and at what
         confidence -- distinguishes "ROI/threshold problem" from
         "genuinely no one in frame" without needing to watch a rendered
         video.
      2. How long would a full annotate_video call actually take on this
         specific server's hardware -- the measured per-call latency
         here is a direct, real extrapolation basis, not a guess ported
         from a different machine.
    Raises ValueError if there's no saved ROI (same requirement as
    annotate_video) or the source can't be opened.
    """
    rois = _load_rois(db, org_id, cam_id)
    if not rois:
        raise ValueError(
            f"No saved workstation ROI for org_id={org_id}, cam_id={cam_id} -- "
            "draw one on the Workstations tab first, or check the camera/org ID."
        )
    if num_samples < 1:
        raise ValueError("num_samples must be >= 1")

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        raise ValueError(f"Could not open source video: {source_path}")

    # Force the model to load/download BEFORE timing starts -- otherwise
    # the first sample's latency includes a one-time cost (confirmed
    # ~40s on a cold start in this project's own testing) that has
    # nothing to do with per-frame inference speed and badly skews the
    # average this function exists to report.
    yolo_detector.get_model()

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration_seconds = round(total_frames / fps, 1) if (fps and total_frames) else None

        if total_frames > 0:
            sample_indices = sorted(set(
                int(i * (total_frames - 1) / max(1, num_samples - 1)) for i in range(num_samples)
            ))
        else:
            # Frame count unavailable (same metadata gap noted in
            # runAiAnalysis's progress-bar handling) -- fall back to
            # reading sequentially and sampling every Kth frame.
            sample_indices = None

        per_call_seconds: list[float] = []
        confidences: list[float] = []
        frames_with_person = 0
        frames_with_occupancy: dict[str, int] = {name: 0 for name in rois}
        frames_sampled = 0

        def _sample_one(frame) -> None:
            nonlocal frames_with_person, frames_sampled
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                cv2.imwrite(tmp.name, frame)
                tmp_path = tmp.name
            try:
                t0 = time.monotonic()
                people = yolo_detector.detect_people(tmp_path)
                per_call_seconds.append(time.monotonic() - t0)
                frames_sampled += 1
                if people:
                    frames_with_person += 1
                    confidences.extend(p.confidence for p in people)
                for name, roi in rois.items():
                    if any(yolo_detector.boxes_overlap(p, roi) for p in people):
                        frames_with_occupancy[name] += 1
            finally:
                os.unlink(tmp_path)

        if sample_indices is not None:
            for idx in sample_indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
                if ok:
                    _sample_one(frame)
        else:
            stride = 30  # arbitrary fallback spacing when total_frames is unknown
            i = 0
            while frames_sampled < num_samples:
                ok, frame = cap.read()
                if not ok:
                    break
                if i % stride == 0:
                    _sample_one(frame)
                i += 1
    finally:
        cap.release()

    avg_call_seconds = round(sum(per_call_seconds) / len(per_call_seconds), 3) if per_call_seconds else None
    extrapolated = None
    if avg_call_seconds and duration_seconds:
        # Matches annotate_video's own detect_every_n_frames=3 default --
        # if the caller plans a different value, they should scale this.
        default_detect_every_n = 3
        calls_for_full_video = (duration_seconds * fps) / default_detect_every_n
        extrapolated = round(calls_for_full_video * avg_call_seconds, 1)

    return {
        "frames_sampled": frames_sampled,
        "video_total_frames": total_frames or None,
        "video_duration_seconds": duration_seconds,
        "video_fps": round(fps, 2),
        "frames_with_at_least_one_person": frames_with_person,
        "confidence_min": round(min(confidences), 3) if confidences else None,
        "confidence_mean": round(sum(confidences) / len(confidences), 3) if confidences else None,
        "confidence_max": round(max(confidences), 3) if confidences else None,
        "occupancy_hits_by_workstation": frames_with_occupancy,
        "measured_yolo_seconds_per_call": avg_call_seconds,
        "extrapolated_full_annotate_seconds_at_detect_every_3_frames": extrapolated,
    }


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
            frame_annotate.draw_annotations(frame, w, h, rois, last_status, last_result, last_people)

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
