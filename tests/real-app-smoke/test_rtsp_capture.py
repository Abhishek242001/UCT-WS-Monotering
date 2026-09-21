"""Tests for app/vision/capture.py: RTSP detection, credential masking, and
the RtspFrameSource reader (newest-frame delivery, reconnect, stall
detection, stop). No camera and no network: OpenCV captures are replaced by
small fakes, and all timings are shrunk so the file runs in a few seconds.

A real-camera check is separate: python backend/tools/check_rtsp.py <url>
"""
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.vision import capture  # noqa: E402  (imports only cv2 + stdlib, not YOLO)

import cv2  # noqa: E402


# ---- helpers ------------------------------------------------------------
class FakeCap:
    """A stand-in for cv2.VideoCapture. `script` is called with the read
    index and returns (ok, value); value becomes a 2x2x3 frame."""

    def __init__(self, script, opened=True):
        self._script = script
        self._opened = opened
        self.released = False
        self.reads = 0

    def isOpened(self):
        return self._opened

    def read(self):
        i = self.reads
        self.reads += 1
        ok, value = self._script(i)
        if not ok:
            return False, None
        return True, np.full((2, 2, 3), value, dtype=np.int32)

    def get(self, prop):
        return 7

    def release(self):
        self.released = True


def frames_from(start, delay=0.0):
    def script(i):
        if delay:
            time.sleep(delay)
        return True, start + i
    return script


def dead_script(i):
    return False, None  # returns immediately, like a closed connection


def fast_source(opener, **kw):
    params = dict(initial_attempts=2, initial_retry_seconds=0.01, backoff_initial=0.01,
                  backoff_max=0.05, stall_seconds=0.5, startup_grace_seconds=0.0)
    params.update(kw)
    return capture.RtspFrameSource("rtsp://u:p@cam/1", opener=opener, **params)


def value_of(frame):
    return int(frame[0, 0, 0])


def wait_until(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---- source detection and masking ---------------------------------------
@pytest.mark.parametrize("source,expected", [
    ("rtsp://192.168.1.101:554/Streaming/Channels/101", True),
    ("RTSP://cam/1", True),
    ("  rtsps://cam/1", True),
    ("../sample_data/demo-camera-feed.mp4", False),
    ("C:\\videos\\clip.mp4", False),
    ("http://host/stream.m3u8", False),
    ("/mnt/videos/a.mp4", False),
])
def test_is_rtsp_source(source, expected):
    assert capture.is_rtsp_source(source) is expected


def test_mask_credentials_hides_user_and_password():
    masked = capture.mask_credentials("Could not open rtsp://admin:s3cret@192.168.1.101:554/x")
    assert "admin" not in masked and "s3cret" not in masked
    assert "192.168.1.101:554/x" in masked


def test_mask_credentials_leaves_plain_text_alone():
    assert capture.mask_credentials("../sample_data/a.mp4") == "../sample_data/a.mp4"
    assert capture.mask_credentials("rtsp://cam/1") == "rtsp://cam/1"


# ---- opening ---------------------------------------------------------------
def test_file_sources_open_exactly_as_before(monkeypatch):
    calls = []
    sentinel = object()
    monkeypatch.setattr(capture.cv2, "VideoCapture", lambda *a, **k: calls.append((a, k)) or sentinel)
    result = capture.open_frame_source("../sample_data/demo-camera-feed.mp4")
    assert result is sentinel
    assert calls == [(("../sample_data/demo-camera-feed.mp4",), {})]  # one plain call, no extra args


def test_rtsp_open_passes_timeouts(monkeypatch):
    monkeypatch.setenv("RTSP_OPEN_TIMEOUT_MS", "1234")
    monkeypatch.setenv("RTSP_READ_TIMEOUT_MS", "4321")
    monkeypatch.delenv("RTSP_TRANSPORT", raising=False)
    seen = {}
    def fake(source, api=None, params=None):
        seen.update(source=source, api=api, params=params)
        return FakeCap(dead_script)
    monkeypatch.setattr(capture.cv2, "VideoCapture", fake)
    capture.open_capture("rtsp://cam/1")
    assert seen["api"] == cv2.CAP_FFMPEG
    assert seen["params"] == [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 1234, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 4321]


def test_rtsp_transport_env_is_set_only_during_open_and_restored(monkeypatch):
    monkeypatch.setenv("RTSP_TRANSPORT", "tcp")
    monkeypatch.delenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", raising=False)
    during = []
    def fake(source, api=None, params=None):
        during.append(os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS"))
        return FakeCap(dead_script)
    monkeypatch.setattr(capture.cv2, "VideoCapture", fake)
    capture.open_capture("rtsp://cam/1")
    assert during == ["rtsp_transport;tcp"]
    assert "OPENCV_FFMPEG_CAPTURE_OPTIONS" not in os.environ


def test_users_own_capture_options_are_never_overridden(monkeypatch):
    monkeypatch.setenv("RTSP_TRANSPORT", "udp")
    monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|fflags;nobuffer")
    during = []
    monkeypatch.setattr(capture.cv2, "VideoCapture",
                        lambda s, a=None, p=None: during.append(os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]) or FakeCap(dead_script))
    capture.open_capture("rtsp://cam/1")
    assert during == ["rtsp_transport;tcp|fflags;nobuffer"]
    assert os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "rtsp_transport;tcp|fflags;nobuffer"


def test_transport_not_forced_by_default(monkeypatch):
    monkeypatch.delenv("RTSP_TRANSPORT", raising=False)
    monkeypatch.delenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", raising=False)
    during = []
    monkeypatch.setattr(capture.cv2, "VideoCapture",
                        lambda s, a=None, p=None: during.append(os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")) or FakeCap(dead_script))
    capture.open_capture("rtsp://cam/1")
    assert during == [None]


def test_old_opencv_without_params_overload_still_opens(monkeypatch):
    calls = []
    def fake(source, *args):
        calls.append(args)
        if args:
            raise TypeError("no such overload")
        return FakeCap(dead_script)
    monkeypatch.setattr(capture.cv2, "VideoCapture", fake)
    assert capture.open_capture("rtsp://cam/1").isOpened()
    assert len(calls) == 2 and calls[1] == ()


# ---- RtspFrameSource --------------------------------------------------------
def test_reads_frames_in_order_when_consumer_keeps_up():
    src = fast_source(lambda s: FakeCap(frames_from(0, delay=0.01)))
    assert src.start()
    try:
        values = [value_of(src.read()[1]) for _ in range(5)]
    finally:
        src.release()
    assert values == sorted(values) and len(set(values)) == 5


def test_slow_consumer_gets_the_newest_frame_not_a_backlog():
    src = fast_source(lambda s: FakeCap(frames_from(0, delay=0.005)))
    assert src.start()
    try:
        src.read()
        time.sleep(0.4)  # the reader keeps draining the camera meanwhile
        newest_at_that_moment = src._seq
        ok, frame = src.read()
    finally:
        src.release()
    assert ok
    # A backlog would hand back frame ~1. The newest frame is a lot later.
    assert value_of(frame) >= newest_at_that_moment - 2
    assert value_of(frame) > 20


def test_each_frame_is_delivered_at_most_once():
    src = fast_source(lambda s: FakeCap(frames_from(0, delay=0.05)))
    assert src.start()
    try:
        first = src.read()[1]
        second = src.read()[1]
    finally:
        src.release()
    assert value_of(second) > value_of(first)


def test_reconnects_after_the_connection_closes():
    caps = []
    def opener(source):
        # first cap: 3 good frames, then a closed connection. later caps: healthy.
        n = len(caps)
        cap = FakeCap((lambda i: (True, i) if i < 3 else (False, None)) if n == 0 else frames_from(1000, 0.01))
        caps.append(cap)
        return cap
    events = []
    src = fast_source(opener, on_status=events.append)
    assert src.start()
    try:
        seen = []
        end = time.time() + 5
        while time.time() < end and not any(v >= 1000 for v in seen):
            ok, frame = src.read()
            assert ok
            seen.append(value_of(frame))
    finally:
        src.release()
    assert any(v >= 1000 for v in seen), "never received a frame from the reconnected capture"
    states = [e["state"] for e in events]
    assert states[0] == "reconnecting" and "connected" in states
    assert states.index("connected") > states.index("reconnecting")
    assert caps[0].released, "the dead capture must be released"


def test_keeps_retrying_with_growing_delay_until_the_camera_returns():
    attempts = []
    def opener(source):
        attempts.append(time.monotonic())
        n = len(attempts)
        if n == 1:
            return FakeCap(lambda i: (True, i) if i < 2 else (False, None))
        if n <= 4:
            return FakeCap(dead_script, opened=False)  # camera still offline
        return FakeCap(frames_from(500, 0.01))
    events = []
    src = fast_source(opener, on_status=events.append, backoff_initial=0.02, backoff_max=0.08)
    assert src.start()
    try:
        end = time.time() + 5
        got = False
        while time.time() < end and not got:
            got = value_of(src.read()[1]) >= 500
    finally:
        src.release()
    assert got
    delays = [e["retry_in_seconds"] for e in events if e["state"] == "reconnecting"]
    assert delays[:4] == [0.02, 0.04, 0.08, 0.08]  # doubles, then stays at the cap
    assert [e["attempt"] for e in events if e["state"] == "reconnecting"][:4] == [1, 2, 3, 4]


def test_silent_camera_is_treated_as_stalled_and_reconnected():
    n = {"opens": 0}
    def opener(source):
        n["opens"] += 1
        if n["opens"] == 1:
            def script(i):
                if i < 2:
                    return True, i
                time.sleep(0.3)  # a read that only fails after its timeout
                return False, None
            return FakeCap(script)
        return FakeCap(frames_from(900, 0.01))
    src = fast_source(opener, stall_seconds=0.5)
    assert src.start()
    try:
        end = time.time() + 6
        got = False
        while time.time() < end and not got:
            got = value_of(src.read()[1]) >= 900
    finally:
        src.release()
    assert got and n["opens"] >= 2


def test_one_slow_failure_after_connecting_does_not_reconnect():
    """The decoder waiting for its first keyframe makes the first read slow
    and False. That must not tear the connection down."""
    opens = []
    def opener(source):
        opens.append(1)
        def script(i):
            if i == 0:
                time.sleep(0.2)
                return False, None
            return True, i
        return FakeCap(script)
    src = fast_source(opener, stall_seconds=5.0, startup_grace_seconds=5.0)
    assert src.start()
    try:
        ok, _ = src.read()
    finally:
        src.release()
    assert ok and len(opens) == 1


def test_start_gives_up_when_the_camera_never_opens():
    attempts = []
    def opener(source):
        attempts.append(1)
        return FakeCap(dead_script, opened=False)
    src = fast_source(opener, initial_attempts=3)
    assert src.start() is False
    assert not src.isOpened()
    assert len(attempts) == 3
    src.release()  # safe to call on a source that never opened


def test_stop_event_ends_read_promptly():
    stop = threading.Event()
    src = fast_source(lambda s: FakeCap(lambda i: (time.sleep(0.05), (False, None))[1]), stall_seconds=60.0,
                      startup_grace_seconds=60.0)
    src._stop_event = stop
    assert src.start()
    result = {}
    t = threading.Thread(target=lambda: result.update(r=src.read()))
    t.start()
    time.sleep(0.2)
    began = time.time()
    stop.set()
    t.join(3)
    src.release()
    assert not t.is_alive()
    assert result["r"] == (False, None)
    assert time.time() - began < 2


def test_release_closes_the_camera_and_later_reads_fail():
    cap_holder = []
    def opener(source):
        cap = FakeCap(frames_from(0, 0.01))
        cap_holder.append(cap)
        return cap
    src = fast_source(opener)
    assert src.start()
    src.read()
    src.release()
    assert wait_until(lambda: cap_holder[0].released)
    assert src.read() == (False, None)
    assert not src.isOpened()


def test_frame_count_is_zero_for_a_live_camera():
    src = fast_source(lambda s: FakeCap(frames_from(0, 0.01)))
    assert src.start()
    try:
        assert src.get(cv2.CAP_PROP_FRAME_COUNT) == 0
    finally:
        src.release()


def test_a_failing_status_callback_never_breaks_the_reader():
    caps = []
    def opener(source):
        n = len(caps)
        cap = FakeCap((lambda i: (True, i) if i < 2 else (False, None)) if n == 0 else frames_from(300, 0.01))
        caps.append(cap)
        return cap
    def bad_callback(info):
        raise RuntimeError("boom")
    src = fast_source(opener, on_status=bad_callback)
    assert src.start()
    try:
        end = time.time() + 5
        got = False
        while time.time() < end and not got:
            got = value_of(src.read()[1]) >= 300
    finally:
        src.release()
    assert got
