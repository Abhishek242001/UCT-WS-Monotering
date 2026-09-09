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
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Workstation, AdminSession
from app.routers.admin_auth import require_admin, verify_org_access
from app.routers import streams as streams_router
import asyncio

router = APIRouter(tags=["videos"])

def _video_dir() -> str:
    """Read at call time, not import time -- see dataset_calibration.py's
    _data_dir() for why."""
    return os.environ.get("UPLOADED_VIDEOS_DIR", "./uploaded_videos")
MAX_VIDEO_BYTES = int(os.environ.get("MAX_VIDEO_UPLOAD_BYTES", 500 * 1024 * 1024))  # 500MB
DEMO_WORKSTATION_NAME = "Demo-Desk"

_videos: dict[str, dict] = {}  # video_id -> {filename, path, size_bytes}


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
