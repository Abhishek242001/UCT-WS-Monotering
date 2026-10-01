"""
Lightweight session for RTSP (or file) based employee enrollment.

Deliberately NOT built on StreamWorker (app/vision/stream_worker.py):
StreamWorker runs the occupancy/identification state machine against a
workstation and, when is_live=True, can write real attendance records --
reusing it here for "point a camera at someone while they stand at a few
distances so we can capture face crops" would risk writing spurious
occupancy/attendance data for a desk that was never actually the subject
of the capture. Enrollment only ever needs "give me the newest frame from
this source, on demand" -- nothing event-driven, no ROI, no attendance
side effects -- so it gets its own minimal session type instead, built
directly on app/vision/capture.py (the same RTSP hardening -- reconnect,
timeouts, newest-frame reads -- used by the real stream worker).

One session per connection, keyed by an opaque session_id. Frames are
pulled on demand (by the live-preview WebSocket loop and by the capture
endpoint), never pushed anywhere on a timer.
"""
import threading
import time
import uuid

import cv2

from app.vision import capture

# Unused sessions are swept so a forgotten enrollment browser tab doesn't
# hold an RTSP connection (and its reader thread) open forever.
IDLE_TIMEOUT_SECONDS = 15 * 60


class EnrollmentSession:
    def __init__(self, source: str, org_id: int):
        self.source = source
        self.org_id = org_id
        self.session_id = str(uuid.uuid4())
        self._stop_event = threading.Event()
        self._cap = capture.open_frame_source(self.source, stop_event=self._stop_event)
        self.last_used = time.monotonic()

    def is_opened(self) -> bool:
        return bool(self._cap) and self._cap.isOpened()

    def get_frame(self):
        """Returns the newest available frame (a BGR numpy array), or None
        if the source has nothing yet / has been closed. A finite file
        source loops back to its first frame on EOF instead of being
        treated as "stopped" -- so a short demo video can be used as a
        repeating enrollment preview, same as it's used elsewhere in this
        app (see the Live Stream tab's own demo-video guidance)."""
        self.last_used = time.monotonic()
        ok, frame = self._cap.read()
        if not ok and not capture.is_rtsp_source(self.source):
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
        return frame if ok else None

    def close(self):
        self._stop_event.set()
        try:
            self._cap.release()
        except Exception:
            pass


_sessions: dict[str, EnrollmentSession] = {}
_lock = threading.Lock()


def start_session(source: str, org_id: int) -> EnrollmentSession:
    _sweep_idle()
    session = EnrollmentSession(source, org_id)
    if not session.is_opened():
        session.close()
        raise ValueError(f"Could not open source: {capture.mask_credentials(source)}")
    with _lock:
        _sessions[session.session_id] = session
    return session


def get_session(session_id: str) -> EnrollmentSession | None:
    return _sessions.get(session_id)


def stop_session(session_id: str) -> bool:
    with _lock:
        session = _sessions.pop(session_id, None)
    if session is None:
        return False
    session.close()
    return True


def _sweep_idle():
    now = time.monotonic()
    with _lock:
        stale_ids = [sid for sid, s in _sessions.items() if now - s.last_used > IDLE_TIMEOUT_SECONDS]
        stale = [_sessions.pop(sid) for sid in stale_ids]
    for session in stale:
        session.close()
