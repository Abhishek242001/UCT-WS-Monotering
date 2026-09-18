"""
Video upload + "Run AI Analysis" for the sales-demo use case: a person
uploads a recorded video instead of pointing at a live RTSP camera, and
gets the exact same real-time occupancy/identity analysis experience --
because under the hood it IS the exact same code path (app/routers/
streams.py's start_worker(), and app/vision/stream_worker.py itself,
completely unmodified). The only thing this router adds is (1) accepting
a file upload and (2) sensible default org_id/cam_id/workstation so a
demo works with zero prior setup, while still letting the caller override
any of those defaults explicitly.

Upload progress ("show in % how much video is uploaded") is a client-side
concern, not a server one -- see frontend/index.html's use of
XMLHttpRequest.upload.onprogress, which reports real bytes-sent progress
during the multipart upload itself. Nothing special is needed here beyond
accepting the upload normally.
"""
import hashlib
import json
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Workstation, AdminSession
from app.routers.admin_auth import require_admin, verify_org_access
from app.routers import streams as streams_router
from app.vision import video_export
import asyncio

router = APIRouter(tags=["videos"])

def _video_dir() -> str:
    """Read at call time, not import time -- see dataset_calibration.py's
    _data_dir() for why."""
    return os.environ.get("UPLOADED_VIDEOS_DIR", "./uploaded_videos")
MAX_VIDEO_BYTES = int(os.environ.get("MAX_VIDEO_UPLOAD_BYTES", 500 * 1024 * 1024))  # 500MB
DEMO_WORKSTATION_NAME = "Demo-Desk"

_videos: dict[str, dict] = {}  # video_id -> {filename, path, size_bytes, org_id, file_hash}


def _meta_path(video_path: str) -> str:
    return video_path + ".meta.json"


def _save_video_meta(video_id: str, meta: dict) -> None:
    """Writes a JSON sidecar next to the video file so _videos survives a
    backend restart -- previously _videos was purely in-memory, so every
    restart silently orphaned every already-uploaded video: find_by_hash
    could never match them again (making re-uploads look "new" instead of
    being flagged as duplicates), and /videos/{id}/analyze|clip|annotate
    all 404'd on any video_id from before the restart, even though the
    actual file was still sitting on disk the whole time."""
    with open(_meta_path(meta["path"]), "w") as f:
        json.dump({"video_id": video_id, **meta}, f)


def _load_videos_from_disk() -> None:
    """Runs once at process start (module import time -- FastAPI routers
    are imported exactly once per process). Rebuilds _videos from the
    *.meta.json sidecars written by _save_video_meta, so videos uploaded
    in a previous process run are usable again: find_by_hash matches
    them, and their video_id keeps working for analyze/clip/annotate.
    Sidecar-less video files (from before this fix, or a sidecar that
    failed to write) are simply not recovered -- their bytes remain on
    disk but are orphaned, same as before this fix; nothing here deletes
    or modifies them."""
    video_dir = _video_dir()
    if not os.path.isdir(video_dir):
        return
    loaded = 0
    for name in os.listdir(video_dir):
        if not name.endswith(".meta.json"):
            continue
        try:
            with open(os.path.join(video_dir, name)) as f:
                data = json.load(f)
            video_id = data.pop("video_id")
            if os.path.exists(data.get("path", "")):
                _videos[video_id] = data
                loaded += 1
        except (json.JSONDecodeError, KeyError, OSError):
            continue  # a corrupt/partial sidecar -- skip it, don't crash startup
    if loaded:
        print(f"[videos] Recovered {loaded} video(s) from disk metadata after restart.")


_load_videos_from_disk()


def _default_cam_id_for(video_id: str) -> int:
    """Deterministic per-video default camera slot, so re-running analysis
    on the same uploaded video reuses the same demo workstation instead of
    creating a duplicate each time. Starts at 90000 specifically to stay
    out of the way of real camera IDs a customer's own deployment might
    already be using."""
    digest = hashlib.md5(video_id.encode()).hexdigest()
    return 90000 + (int(digest, 16) % 9999)


@router.post("/videos/upload", status_code=201)
async def upload_video(org_id: int = Form(...), file: UploadFile = File(...), admin: AdminSession = Depends(require_admin)):
    """org_id is required here (matching /dataset/upload's existing
    pattern), not just accepted at analysis time as an override -- without
    it, an uploaded video had no owner at all, meaning /videos (list) and
    /videos/{id}/analyze had nothing to check ownership against, and any
    admin could analyze or enumerate any other org's uploaded videos."""
    verify_org_access(admin, org_id)
    allowed_ext = (".mp4", ".mov", ".avi", ".mkv", ".webm")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in allowed_ext:
        raise HTTPException(422, f"Unsupported video format '{ext}'. Allowed: {', '.join(allowed_ext)}")

    os.makedirs(_video_dir(), exist_ok=True)
    video_id = f"VID-{uuid.uuid4().hex[:12]}"
    dest_path = os.path.abspath(os.path.join(_video_dir(), f"{video_id}{ext}"))

    size = 0
    hasher = hashlib.sha256()
    with open(dest_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_VIDEO_BYTES:
                f.close()
                os.remove(dest_path)
                raise HTTPException(413, f"Video exceeds maximum allowed size of {MAX_VIDEO_BYTES // (1024*1024)}MB")
            hasher.update(chunk)  # computed in the same streaming pass -- no extra read of the file afterward
            f.write(chunk)
    file_hash = hasher.hexdigest()

    _videos[video_id] = {"filename": file.filename, "path": dest_path, "size_bytes": size,
                          "org_id": org_id, "file_hash": file_hash}
    _save_video_meta(video_id, _videos[video_id])
    return {"video_id": video_id, "filename": file.filename, "size_bytes": size, "status": "uploaded", "file_hash": file_hash}


@router.get("/videos/find_by_hash")
def find_by_hash(org_id: int, file_hash: str, admin: AdminSession = Depends(require_admin)):
    """Lets the frontend check whether a video's content already exists
    BEFORE uploading it -- the actual efficiency win, since the browser
    computes the hash locally (Web Crypto API) and this call is a cheap
    lookup, avoiding re-sending a large file over the network entirely
    when it's a genuine duplicate. Scoped by org_id: two different orgs
    uploading the identical stock demo video are not "the same upload" as
    far as ownership/analysis history goes, even though the bytes match."""
    verify_org_access(admin, org_id)
    for video_id, v in _videos.items():
        if v["org_id"] == org_id and v["file_hash"] == file_hash:
            return {"found": True, "video_id": video_id, "filename": v["filename"], "size_bytes": v["size_bytes"]}
    return {"found": False}


@router.get("/videos")
def list_videos(org_id: int, admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    return {"videos": [
        {"video_id": vid, "filename": v["filename"], "size_bytes": v["size_bytes"]}
        for vid, v in _videos.items() if v["org_id"] == org_id
    ]}


def _ensure_demo_workstation(db: Session, org_id: int, cam_id: int) -> str:
    """Auto-provisions a full-frame ROI if this org/cam has no workstation
    configured yet, so 'upload -> Run AI Analysis' produces a visible
    result with zero prior setup. A real deployment would normally draw a
    proper per-desk ROI via the Workstations tab; this exists specifically
    to remove that friction for a first-look sales demo."""
    existing = db.query(Workstation).filter_by(org_id=org_id, cam_id=cam_id).first()
    if existing:
        return existing.name
    db.add(Workstation(org_id=org_id, cam_id=cam_id, name=DEMO_WORKSTATION_NAME, x1=0.0, y1=0.0, x2=1.0, y2=1.0))
    db.commit()
    return DEMO_WORKSTATION_NAME


@router.post("/videos/{video_id}/analyze", status_code=201)
async def analyze_video(
    video_id: str,
    org_id: int | None = Form(None),
    cam_id: int | None = Form(None),
    user_id: int | None = Form(None),
    max_frames: int | None = Form(None),
    poll_interval_seconds: float = Form(0.0),
    db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin),
):
    """Starts real analysis on an uploaded video using the exact same
    start_worker() call /streams/start uses for a live RTSP camera. Every
    parameter is optional: omit them entirely for an instant, zero-setup
    demo run against the video's own org, or supply org_id explicitly to
    analyze it in a different org's context (e.g. a customer's real
    floorplan) -- provided the caller actually has access to that org too."""
    video = _videos.get(video_id)
    if not video:
        raise HTTPException(404, "Video not found -- upload it first via POST /videos/upload")
    verify_org_access(admin, video["org_id"])  # must have access to the video's own org to use it at all

    resolved_org_id = org_id if org_id is not None else video["org_id"]
    if org_id is not None:
        verify_org_access(admin, org_id)  # an explicit override still needs access to THAT org too
    resolved_cam_id = cam_id if cam_id is not None else _default_cam_id_for(video_id)
    resolved_user_id = user_id if user_id is not None else 1

    workstation_name = _ensure_demo_workstation(db, resolved_org_id, resolved_cam_id)

    loop = asyncio.get_running_loop()
    try:
        stream_id = streams_router.start_worker(
            source=video["path"], org_id=resolved_org_id, cam_id=resolved_cam_id, loop=loop,
            max_frames=max_frames, poll_interval_seconds=poll_interval_seconds,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))

    return {
        "stream_id": stream_id, "video_id": video_id, "status": "started",
        "org_id": resolved_org_id, "cam_id": resolved_cam_id, "user_id": resolved_user_id,
        "workstation_name": workstation_name,
        "defaults_used": {"org_id": org_id is None, "cam_id": cam_id is None, "user_id": user_id is None},
    }


def _clips_dir() -> str:
    return os.environ.get("VIDEO_CLIPS_DIR", "./video_clips")


def _annotated_dir() -> str:
    return os.environ.get("ANNOTATED_VIDEOS_DIR", "./annotated_videos")


_annotated_videos: dict[str, dict] = {}  # annotated_id -> {path, org_id, source_video_id}


@router.get("/videos/{video_id}/clip")
def download_clip(video_id: str, seconds: int = 300, admin: AdminSession = Depends(require_admin)):
    """Downloads the first `seconds` of the original uploaded video, no
    detection/annotation -- for quickly reviewing raw footage without
    pulling the full file. Default 300s = first 5 minutes, per the
    original request. Cached: a repeat call with the same video_id+seconds
    reuses the already-extracted file instead of re-cutting it."""
    video = _videos.get(video_id)
    if not video:
        raise HTTPException(404, "Video not found -- upload it first via POST /videos/upload")
    verify_org_access(admin, video["org_id"])
    if seconds <= 0 or seconds > 3600:
        raise HTTPException(422, "seconds must be between 1 and 3600")

    os.makedirs(_clips_dir(), exist_ok=True)
    dest_path = os.path.abspath(os.path.join(_clips_dir(), f"{video_id}_{seconds}s.mp4"))
    if not os.path.exists(dest_path):
        try:
            video_export.extract_clip(video["path"], dest_path, seconds)
        except ValueError as e:
            raise HTTPException(422, str(e))

    base_name = os.path.splitext(video["filename"] or video_id)[0]
    return FileResponse(dest_path, media_type="video/mp4", filename=f"{base_name}_first{seconds}s.mp4")


@router.post("/videos/{video_id}/annotate", status_code=201)
def create_annotated_video(
    video_id: str,
    org_id: int = Form(...),
    cam_id: int = Form(...),
    max_seconds: int = Form(60),
    detect_every_n_frames: int = Form(3),
    db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin),
):
    """Runs real YOLO detection + identity matching over up to max_seconds
    of the uploaded video and renders the result (ROI boxes, detected
    people, occupancy/identity status) onto a new downloadable video --
    see app/vision/video_export.py for exactly what runs and why it's
    capped and frame-skipped by default. This call is synchronous and
    blocks until rendering finishes, so keep max_seconds modest; there is
    no background job/progress reporting for this endpoint."""
    video = _videos.get(video_id)
    if not video:
        raise HTTPException(404, "Video not found -- upload it first via POST /videos/upload")
    verify_org_access(admin, video["org_id"])
    verify_org_access(admin, org_id)  # org_id used for the ROI/gallery lookup may differ from the video's own org
    if max_seconds <= 0 or max_seconds > 600:
        raise HTTPException(422, "max_seconds must be between 1 and 600 (10 min) -- CPU-only YOLO "
                                  "inference makes a longer synchronous run impractically slow")
    if detect_every_n_frames < 1:
        raise HTTPException(422, "detect_every_n_frames must be >= 1")

    annotated_id = f"ANNOT-{uuid.uuid4().hex[:12]}"
    os.makedirs(_annotated_dir(), exist_ok=True)
    dest_path = os.path.abspath(os.path.join(_annotated_dir(), f"{annotated_id}.mp4"))
    try:
        result = video_export.annotate_video(
            video["path"], dest_path, db, org_id, cam_id,
            max_seconds=max_seconds, detect_every_n_frames=detect_every_n_frames,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))

    _annotated_videos[annotated_id] = {"path": dest_path, "org_id": org_id, "source_video_id": video_id}
    return {"annotated_id": annotated_id, "download_url": f"/videos/annotated/{annotated_id}/download", **result}


@router.get("/videos/annotated/{annotated_id}/download")
def download_annotated(annotated_id: str, admin: AdminSession = Depends(require_admin)):
    row = _annotated_videos.get(annotated_id)
    if not row:
        raise HTTPException(404, "Annotated video not found -- it must be generated via POST "
                                  "/videos/{video_id}/annotate first, and only exists in this "
                                  "backend process's memory (lost on restart, same as uploaded videos)")
    verify_org_access(admin, row["org_id"])
    return FileResponse(row["path"], media_type="video/mp4", filename=f"{annotated_id}.mp4")
