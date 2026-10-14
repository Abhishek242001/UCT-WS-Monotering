"""
Tests the on-screen FPS counter overlay: a small "FPS: NN.N" label drawn
in the top-left corner of every live-preview frame, requested so an admin
watching the live feed (or reviewing a screenshot) can see the actual
processing rate without digging through logs.

Two independent things are tested, both without needing a real
YOLO/InsightFace install:

1. `frame_annotate.draw_annotations()` draws the overlay when given an
   `fps` value, and draws nothing extra (byte-for-byte unchanged
   behavior) when `fps` is omitted -- this is what keeps the change
   backward compatible for any caller that hasn't adopted it.
2. `StreamWorker._tick_fps()` computes a smoothed frames/sec estimate
   from real wall-clock gaps between processed frames, independent of
   the (unrelated, intentionally throttled) live-preview publish rate.
"""
import sys
import time
from pathlib import Path

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.vision import frame_annotate  # noqa: E402
from app.vision.stream_worker import StreamWorker  # noqa: E402


def _blank_frame():
    return np.zeros((120, 220, 3), dtype=np.uint8)


def test_draw_annotations_without_fps_is_unchanged():
    """Omitting `fps` must draw exactly what it did before this feature
    existed -- no stray pixels in the top-left corner."""
    frame = _blank_frame()
    frame_annotate.draw_annotations(frame, 220, 120, {}, {}, {}, [])
    assert not frame.any()


def test_draw_annotations_with_fps_draws_something_top_left():
    """Passing a real fps value should draw into the top-left region and
    nowhere require any other caller changes."""
    frame = _blank_frame()
    frame_annotate.draw_annotations(frame, 220, 120, {}, {}, {}, [], fps=29.97)
    # The overlay is drawn at (10, 20) in a ~25px-tall band -- confirm the
    # top rows actually changed and the rest of the (empty-ROI) frame did not.
    assert frame[:30, :, :].any()
    assert not frame[60:, :, :].any()


def test_draw_annotations_fps_does_not_crash_with_zero_or_negative():
    """A pathological fps value (e.g. right after a clock anomaly) should
    still render rather than raising -- this is purely a display label."""
    frame = _blank_frame()
    frame_annotate.draw_annotations(frame, 220, 120, {}, {}, {}, [], fps=0.0)
    frame_annotate.draw_annotations(frame, 220, 120, {}, {}, {}, [], fps=-1.0)


def test_tick_fps_computes_smoothed_rate_from_real_elapsed_time():
    """Bypasses StreamWorker.__init__ (which needs a DB/models) since
    _tick_fps only touches two plain instance attributes -- this keeps
    the test fast and dependency-free while still exercising the real
    method body, not a reimplementation of it."""
    worker = StreamWorker.__new__(StreamWorker)
    worker._fps_last_tick = None
    worker._fps_ema = None

    worker._tick_fps()
    assert worker._fps_ema is None  # first tick has no prior sample to diff against
    assert worker._fps_last_tick is not None

    time.sleep(0.05)  # ~20 fps gap
    worker._tick_fps()
    assert worker._fps_ema is not None
    # Real instantaneous rate for a ~50ms gap is ~20fps; allow generous
    # slack for scheduler jitter in a shared CI/sandbox environment.
    assert 5.0 < worker._fps_ema < 200.0


def test_tick_fps_smooths_rather_than_jumping_to_latest_sample():
    """A single artificially slow frame (e.g. the heartbeat identify()
    tick) shouldn't make the displayed number collapse to that one
    frame's rate -- the EMA should land between the steady-state value
    and the outlier, not jump all the way to the outlier."""
    worker = StreamWorker.__new__(StreamWorker)
    worker._fps_last_tick = None
    worker._fps_ema = None

    for _ in range(5):
        worker._tick_fps()
        time.sleep(1.0 / 30.0)
    after_steady = worker._fps_ema
    assert after_steady is not None and after_steady > 10.0

    # Now one deliberately slow tick (simulated via a longer sleep).
    time.sleep(0.3)
    worker._tick_fps()
    after_slow_tick = worker._fps_ema
    # EMA should have moved DOWN toward the slow sample but not collapsed
    # all the way to it (alpha=0.2 means it's mostly still the old value).
    assert after_slow_tick < after_steady
    assert after_slow_tick > (1.0 / 0.3)  # not fully collapsed to the single slow sample's instantaneous rate
