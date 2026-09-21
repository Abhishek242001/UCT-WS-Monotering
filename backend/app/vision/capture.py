"""Opening and reading video sources, with extra care for RTSP cameras.

Two kinds of source go through the same StreamWorker:

  * A video FILE (Video Analysis uploads, or a server-side path typed into
    Live Stream). open_frame_source() returns a plain cv2.VideoCapture,
    exactly as before. Nothing here changes how files are read.

  * An RTSP camera (rtsp:// or rtsps://). open_frame_source() returns an
    RtspFrameSource, which fixes three problems a bare cv2.VideoCapture has
    with a real camera:

      1. Lag that grows over time. A camera delivers ~25 frames/s, but the
         worker processes far fewer (YOLO on CPU, plus a pause between
         frames). Unread frames pile up inside OpenCV/FFmpeg, so the worker
         ends up analysing video that is seconds or minutes old. A reader
         thread now drains the camera continuously and keeps only the
         newest frame; the worker always receives the newest one.
      2. Hangs. With no read timeout, a stalled camera (cable pulled,
         Wi-Fi drop) blocks cap.read() for 30 s or more. Open and read
         timeouts are now set, so a stall is detected in seconds.
      3. Permanent stop. The old loop treated ~1 s of failed reads as
         "end of stream". A camera reboot or brief network drop ended the
         run for good. RtspFrameSource reconnects with exponential backoff
         until the worker is stopped.

Tunables (environment variables, all optional):
    RTSP_OPEN_TIMEOUT_MS            default 8000
    RTSP_READ_TIMEOUT_MS            default 5000
    RTSP_RECONNECT_INITIAL_SECONDS  default 1
    RTSP_RECONNECT_MAX_SECONDS      default 30
    RTSP_INITIAL_OPEN_ATTEMPTS      default 3   (before giving up at start)
    RTSP_STALL_SECONDS              default 10  (no new frame for this long
                                    means the camera has stalled; reconnect)
    RTSP_TRANSPORT                  unset by default. "tcp" or "udp" forces
                                    the RTP transport. Unset keeps OpenCV's
                                    own default (prefers TCP).

This module deliberately imports only cv2 and the standard library, so it
can be tested and used (see backend/tools/check_rtsp.py) without loading
YOLO.
"""
import os
import re
import threading
import time

import cv2

RTSP_SCHEMES = ("rtsp://", "rtsps://")
_CREDENTIALS_IN_URL_RE = re.compile(r"://([^:/@]+):([^@/]+)@")
_CAPTURE_OPTIONS_ENV = "OPENCV_FFMPEG_CAPTURE_OPTIONS"

# Opening a capture reads the environment, and os.environ is per process,
# not per thread -- so temporary changes to it are serialised.
_open_lock = threading.Lock()


def is_rtsp_source(source: str) -> bool:
    return source.strip().lower().startswith(RTSP_SCHEMES)


def mask_credentials(text: str) -> str:
    """rtsp://user:pass@host/... -> rtsp://***:***@host/...  Use on ANY
    string that may reach a log, a print, or an API/WebSocket message."""
    return _CREDENTIALS_IN_URL_RE.sub("://***:***@", text)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def open_capture(source: str):
    """Opens one cv2.VideoCapture. Files are opened exactly as before.
    RTSP sources get open/read timeouts (and an optional forced transport)."""
    if not is_rtsp_source(source):
        return cv2.VideoCapture(source)

    open_ms = _env_int("RTSP_OPEN_TIMEOUT_MS", 8000)
    read_ms = _env_int("RTSP_READ_TIMEOUT_MS", 5000)
    transport = os.environ.get("RTSP_TRANSPORT", "").strip().lower()

    with _open_lock:
        previous = os.environ.get(_CAPTURE_OPTIONS_ENV)
        # Respect a value the user already set globally; only add ours if
        # they asked for a transport and did not set their own options.
        override = transport in ("tcp", "udp") and previous is None
        if override:
            os.environ[_CAPTURE_OPTIONS_ENV] = f"rtsp_transport;{transport}"
        try:
            try:
                cap = cv2.VideoCapture(
                    source, cv2.CAP_FFMPEG,
                    [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, read_ms],
                )
            except (TypeError, cv2.error):
                # Older OpenCV without the params overload: still works,
                # just without the timeouts.
                cap = cv2.VideoCapture(source)
        finally:
            if override:
                os.environ.pop(_CAPTURE_OPTIONS_ENV, None)
    return cap


class RtspFrameSource:
    """Looks like the small part of cv2.VideoCapture that StreamWorker
    uses (isOpened / read / get / release), but is backed by a reader
    thread that always keeps the newest frame and reconnects on failure.

    read() blocks until a frame NEWER than the last one returned is
    available. It returns (False, None) only when the source was released
    or the worker's stop event is set -- never merely because the camera
    dropped; during a reconnect it simply keeps waiting.
    """

    def __init__(self, source, stop_event=None, on_status=None, opener=None,
                 initial_attempts=None, initial_retry_seconds=2.0,
                 backoff_initial=None, backoff_max=None, transient_failures=3,
                 stall_seconds=None, startup_grace_seconds=5.0):
        self.source = source
        self._stop_event = stop_event
        self._on_status = on_status
        self._opener = opener or open_capture
        self._initial_attempts = initial_attempts if initial_attempts is not None else _env_int("RTSP_INITIAL_OPEN_ATTEMPTS", 3)
        self._initial_retry_seconds = initial_retry_seconds
        self._backoff_initial = backoff_initial if backoff_initial is not None else _env_float("RTSP_RECONNECT_INITIAL_SECONDS", 1.0)
        self._backoff_max = backoff_max if backoff_max is not None else _env_float("RTSP_RECONNECT_MAX_SECONDS", 30.0)
        self._transient_failures = transient_failures
        self._stall_seconds = stall_seconds if stall_seconds is not None else _env_float("RTSP_STALL_SECONDS", 10.0)
        self._startup_grace_seconds = startup_grace_seconds  # extra time for the first keyframe after (re)connecting

        self._cap = None
        self._opened = False
        self._closed = threading.Event()
        self._cond = threading.Condition()
        self._frame = None
        self._seq = 0
        self._consumed_seq = 0
        self._reconnecting = False
        self._thread = None

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> bool:
        for attempt in range(self._initial_attempts):
            cap = self._opener(self.source)
            if cap.isOpened():
                self._cap = cap
                break
            cap.release()
            if attempt < self._initial_attempts - 1 and self._wait(self._initial_retry_seconds):
                break
        if self._cap is None:
            return False
        self._opened = True
        self._thread = threading.Thread(target=self._run, name="rtsp-reader", daemon=True)
        self._thread.start()
        return True

    def isOpened(self) -> bool:
        return self._opened and not self._closed.is_set()

    def get(self, prop):
        # Frame count is meaningless for a live camera. Everything else is
        # answered by the current capture if there is one.
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return 0
        cap = self._cap
        return cap.get(prop) if cap is not None else 0

    def release(self):
        self._closed.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # The reader may be inside a read that lasts up to the read
            # timeout; it releases the capture itself when it exits.
            thread.join(timeout=_env_int("RTSP_READ_TIMEOUT_MS", 5000) / 1000.0 + 3.0)

    # ---- consumer side -----------------------------------------------
    def read(self):
        with self._cond:
            while True:
                if self._should_stop():
                    return False, None
                if self._thread is not None and not self._thread.is_alive():
                    return False, None
                if self._seq != self._consumed_seq and self._frame is not None:
                    self._consumed_seq = self._seq
                    return True, self._frame
                self._cond.wait(timeout=0.25)

    # ---- reader thread -----------------------------------------------
    def _should_stop(self) -> bool:
        return self._closed.is_set() or (self._stop_event is not None and self._stop_event.is_set())

    def _wait(self, seconds: float) -> bool:
        """Sleeps up to `seconds`, waking early if closed. True if stopping."""
        deadline = time.monotonic() + seconds
        while not self._should_stop():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._closed.wait(min(remaining, 0.1))
        return True

    def _emit(self, **info):
        if self._on_status:
            try:
                self._on_status(info)
            except Exception:
                pass  # a status callback must never take the reader down

    def _run(self):
        # Two ways a camera is judged gone:
        #  * the connection was closed or errored: read() fails again and
        #    again, immediately -> reconnect after a few quick failures;
        #  * the camera went silent: read() only fails after its timeout,
        #    so failures are slow -> reconnect once no frame has arrived
        #    for _stall_seconds.
        # A single slow failure right after connecting is normal (the
        # decoder waits for the first keyframe) and does NOT reconnect.
        last_progress = time.monotonic() + self._startup_grace_seconds
        quick_failures = 0
        try:
            while not self._should_stop():
                started = time.monotonic()
                try:
                    ok, frame = self._cap.read()
                except Exception:
                    ok, frame = False, None
                now = time.monotonic()
                if ok and frame is not None:
                    last_progress = now
                    quick_failures = 0
                    if self._reconnecting:
                        self._reconnecting = False
                        self._emit(state="connected")
                    with self._cond:
                        self._frame = frame
                        self._seq += 1
                        self._cond.notify_all()
                    continue
                quick = (now - started) < 0.5
                quick_failures = quick_failures + 1 if quick else 0
                if quick_failures >= self._transient_failures or (now - last_progress) >= self._stall_seconds:
                    self._reconnect()
                    last_progress = time.monotonic() + self._startup_grace_seconds
                    quick_failures = 0
                elif quick:
                    self._wait(0.2)
        finally:
            cap, self._cap = self._cap, None
            if cap is not None:
                cap.release()
            with self._cond:
                self._cond.notify_all()

    def _reconnect(self):
        self._reconnecting = True
        try:
            self._cap.release()
        except Exception:
            pass
        self._cap = None
        delay = self._backoff_initial
        attempt = 0
        while not self._should_stop():
            attempt += 1
            self._emit(state="reconnecting", attempt=attempt, retry_in_seconds=delay)
            if self._wait(delay):
                return
            cap = self._opener(self.source)
            if cap.isOpened():
                self._cap = cap
                return  # "connected" is announced when the first frame arrives
            cap.release()
            delay = min(delay * 2, self._backoff_max)
        # stopping: _cap stays None, _run's loop condition ends it


def open_frame_source(source: str, stop_event=None, on_status=None):
    """What StreamWorker calls. A plain cv2.VideoCapture for files (and
    HTTP/other sources), an RtspFrameSource for rtsp:// and rtsps://."""
    if not is_rtsp_source(source):
        return cv2.VideoCapture(source)
    frame_source = RtspFrameSource(source, stop_event=stop_event, on_status=on_status)
    frame_source.start()
    return frame_source
