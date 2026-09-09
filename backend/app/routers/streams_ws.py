"""
WebSocket endpoint for watching a stream's analysis results live, as they
happen -- the real-time push experience the video-upload "Run AI Analysis"
feature needs, built on the same event-driven pattern as the reference
`people_websocket_handler` this was modeled on (accept the connection,
register the client, relay messages until disconnect, always clean up in
a finally block).

Deliberately on its OWN router, not attached to routers/streams.py's
router: browsers cannot set custom headers (like Authorization) on a
WebSocket handshake request. The standard, well-known workaround --
passing the session token as a query parameter -- is used here instead,
validated with the exact same session-lookup logic (get_valid_session)
that the header-based path uses, so it's an equivalent security check,
not a weaker one.

This single endpoint serves BOTH a live RTSP camera stream and an
uploaded-video analysis run identically, since both are just a stream_id
pointing at a StreamWorker underneath -- directly satisfying "all the
things we do in RTSP or live analysis should be the same."
"""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.orm import Session

from app import database as db_module
from app.routers.admin_auth import get_valid_session
from app.routers import streams as streams_router
from app.vision import event_bus

logger = logging.getLogger("websockets")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(ch)

router = APIRouter(tags=["streams-websocket"])


@router.websocket("/ws/streams/{stream_id}")
async def stream_analysis_ws(websocket: WebSocket, stream_id: str, token: str = Query(...)):
    db: Session = db_module.SessionLocal()
    try:
        session = get_valid_session(token, db)
    finally:
        db.close()

    if not session:
        await websocket.close(code=4401)  # custom close code in the 4000-4999 (application) range
        return

    worker = streams_router.get_worker(stream_id)
    if not worker:
        await websocket.close(code=4404)
        return

    if session.role != "SUPER_ADMIN" and session.org_id != worker.org_id:
        # Previously only checked "is this a valid admin session", never
        # that the connecting admin actually owns this stream's org --
        # any authenticated admin could watch any other org's live
        # analysis results just by knowing (or brute-forcing) a stream_id.
        await websocket.close(code=4403)
        return

    await websocket.accept()
    logger.info("[%s] Live analysis WebSocket connected (stream_id=%s)", session.username, stream_id)

    queue = event_bus.register(stream_id)
    try:
        # With event_bus's buffering (a stream_id's queue is created by
        # whichever side -- publisher or subscriber -- acts first), a
        # worker that already finished will have left its full message
        # history sitting in the queue, and the loop below drains it
        # normally, ending at the real "completed" message. This fallback
        # only covers the genuinely-empty case: a worker that finished
        # AND whose queue was already fully drained by an earlier
        # subscriber, so there's truly nothing left to relay.
        if not worker.is_alive() and queue.empty():
            await websocket.send_json({
                "type": "completed", "frames_processed": worker.frames_processed,
                "reason": "already_finished_and_drained",
            })
            return

        while True:
            get_message = asyncio.ensure_future(queue.get())
            disconnect_watch = asyncio.ensure_future(websocket.receive_text())
            done, pending = await asyncio.wait(
                {get_message, disconnect_watch}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

            if disconnect_watch in done:
                try:
                    disconnect_watch.result()  # raises WebSocketDisconnect if the client closed
                except WebSocketDisconnect:
                    logger.info("[%s] Client disconnected from stream %s", session.username, stream_id)
                    break
                except Exception:
                    pass  # a stray non-disconnect message from the client -- ignore and keep relaying
                continue

            message = get_message.result()
            await websocket.send_json(message)
            if message.get("type") == "completed":
                break
    except WebSocketDisconnect:
        logger.info("[%s] Client disconnected from stream %s", session.username, stream_id)
    except Exception:
        logger.exception("[%s] Unexpected error in stream %s WebSocket", session.username, stream_id)
    finally:
        event_bus.unregister(stream_id)
