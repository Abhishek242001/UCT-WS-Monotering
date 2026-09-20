"""
Stream registry endpoints, now backed by a real per-stream background
worker (see app/vision/stream_worker.py) that actually opens the video
source (RTSP URL or local video file -- OpenCV's VideoCapture handles
both identically, which is exactly what lets the video-upload analysis
feature in routers/videos.py reuse this same endpoint unmodified) and
runs the event-driven detection loop described in Section 3.2 of the
project documentation.

Live results are pushed to any subscribed WebSocket client in real time
via app/vision/event_bus.py -- see routers/streams_ws.py for the
WebSocket endpoint itself (kept on a separate router, deliberately, since
browsers cannot set the Authorization header this router requires on a
WebSocket handshake).
"""
import asyncio
import re
import uuid

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from app.routers.admin_auth import require_admin
from app.vision.stream_worker import StreamWorker

router = APIRouter(tags=["streams"])

_workers: dict[str, StreamWorker] = {}

_CREDENTIALS_IN_URL_RE = re.compile(r"://([^:/@]+):([^@/]+)@")


def _mask_credentials(source: str) -> str:
    """RTSP URLs commonly embed credentials directly (rtsp://user:pass@host/...),
    per the reference stream handler this project's live/RTSP support was
    modeled on. Never echo them back in an API response -- any admin who
    can call /streams/list would otherwise see every active camera's
    plaintext username and password, not just its existence."""
    return _CREDENTIALS_IN_URL_RE.sub("://***:***@", source)


class StreamStartRequest(BaseModel):
    source: str
    org_id: int
    cam_id: int
    user_id: int
    use_nvenc: bool = False
    max_frames: int | None = None       # optional cap, mainly for testing/demo runs
    poll_interval_seconds: float = 1.0  # pacing between frames when reading a local file


@router.get("/")
def health():
    return {"status": "ok", "service": "workstation-monitoring"}


def start_worker(source: str, org_id: int, cam_id: int, loop, is_live: bool,
                  max_frames: int | None = None, poll_interval_seconds: float = 1.0) -> str:
    """The single real entry point for starting analysis on a video
    source, used identically whether the source is a live RTSP camera or
    an uploaded video file's path -- this is the literal shared code path
    that makes "same as RTSP" true, not just a documentation claim.
    Returns the new stream_id; the worker is already running by the time
    this returns.

    is_live has no required default -- both real callers below pass it
    explicitly, deliberately, since it gates whether a confirmed MATCH
    writes a real attendance record (see StreamWorker.is_live's own
    docstring for why getting this wrong would be a real data-integrity
    problem, not just a cosmetic one)."""
    if not source.strip():
        raise ValueError("source must not be empty")
    stream_id = str(uuid.uuid4())
    worker = StreamWorker(
        source=source, org_id=org_id, cam_id=cam_id,
        max_frames=max_frames, poll_interval_seconds=poll_interval_seconds,
        stream_id=stream_id, loop=loop, is_live=is_live,
    )
    worker.start()
    _workers[stream_id] = worker
    return stream_id


@router.post("/streams/start", status_code=201)
async def start_stream(req: StreamStartRequest, _admin=Depends(require_admin)):
    try:
        loop = asyncio.get_running_loop()  # the REAL server event loop, captured here so the
                                            # background worker thread can safely publish live
                                            # events into it via loop.call_soon_threadsafe()
        stream_id = start_worker(req.source, req.org_id, req.cam_id, loop, is_live=True,
                                  max_frames=req.max_frames, poll_interval_seconds=req.poll_interval_seconds)
    except ValueError as e:
        raise HTTPException(422, str(e))
    # NOTE: this response previously also included an "hls_url" field
    # (f"/hls/{stream_id}/playlist.m3u8") that no endpoint anywhere in
    # this backend ever served -- confirmed by searching the whole
    # app/ tree for "hls" while building the live-preview feature below.
    # Removed rather than left in place claiming a capability that
    # doesn't exist; the real live-viewing path is now the "frame"
    # messages on the existing /ws/streams/{stream_id} WebSocket (see
    # stream_worker.py).
    return {"stream_id": stream_id, "status": "started"}


class StreamStopRequest(BaseModel):
    stream_id: str
    org_id: int


@router.post("/streams/stop")
def stop_stream(req: StreamStopRequest, _admin=Depends(require_admin)):
    worker = _workers.get(req.stream_id)
    if not worker or worker.org_id != req.org_id:
        # Without the org_id check, any admin could stop any org's active
        # stream just by knowing (or brute-forcing) a stream_id -- no
        # ownership was ever verified before this fix.
        raise HTTPException(404, "Stream not found")
    worker.stop()
    worker.join(timeout=3)
    del _workers[req.stream_id]
    return {"status": "stopped", "stream_id": req.stream_id}


@router.get("/streams/list")
def list_streams(org_id: int, _admin=Depends(require_admin)):
    return {"streams": [
        {"stream_id": sid, "source": _mask_credentials(w.source), "org_id": w.org_id, "cam_id": w.cam_id,
         "is_alive": w.is_alive(),
         "frames_processed": w.frames_processed, "source_opened": w.source_opened}
        for sid, w in _workers.items() if w.org_id == org_id
    ]}


def get_worker(stream_id: str) -> StreamWorker | None:
    """Small accessor for other modules (routers/streams_ws.py,
    routers/videos.py) to check a worker's status without reaching into
    the module-private _workers dict directly."""
    return _workers.get(stream_id)
