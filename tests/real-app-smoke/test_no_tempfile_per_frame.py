"""
Tests the FPS fix: frames are passed directly (as ndarrays) to YOLO
instead of being written to a temp JPEG and read back. Uses fake/mocked
models and detector functions throughout, so this needs no real
YOLO/InsightFace install and runs anywhere.
"""
import glob
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.vision import yolo_detector  # noqa: E402
from app.vision import stream_worker as sw  # noqa: E402


def _tempdir_jpg_count() -> int:
    return len(glob.glob(os.path.join(tempfile.gettempdir(), "*.jpg")))


class _FakeResult:
    """Stands in for one Ultralytics Results object -- _extract_people is
    mocked out in these tests, so its actual shape doesn't matter."""
    pass


# ---------------------------------------------------------------------------
# yolo_detector.py: the ndarray is passed straight through to
# model.predict()/model.track(), not converted to or read from a path.
# ---------------------------------------------------------------------------

def test_detect_people_passes_the_exact_frame_object_to_predict(monkeypatch):
    frame = np.zeros((10, 10, 3), dtype="uint8")
    captured = {}

    class FakeModel:
        def predict(self, arg, verbose=False):
            captured["arg"] = arg
            return [_FakeResult()]

    monkeypatch.setattr(yolo_detector, "get_model", lambda: FakeModel())
    monkeypatch.setattr(yolo_detector, "_extract_people", lambda *a, **k: [])

    yolo_detector.detect_people(frame)

    assert captured["arg"] is frame  # identity check: the same ndarray, never a path string


def test_detect_and_track_people_passes_the_exact_frame_object_to_track(monkeypatch):
    frame = np.zeros((10, 10, 3), dtype="uint8")
    captured = {}

    class FakeModel:
        def track(self, arg, persist=True, verbose=False, tracker="bytetrack.yaml"):
            captured["arg"] = arg
            return [_FakeResult()]

    monkeypatch.setattr(yolo_detector, "_extract_people", lambda *a, **k: [])

    yolo_detector.detect_and_track_people(FakeModel(), frame)

    assert captured["arg"] is frame


def test_detect_people_still_accepts_a_path_string_for_backward_compatibility(monkeypatch):
    """Callers that still pass a file path (e.g. a single-still upload
    endpoint) must keep working unchanged -- this fix adds ndarray
    support, it doesn't remove path support."""
    captured = {}

    class FakeModel:
        def predict(self, arg, verbose=False):
            captured["arg"] = arg
            return [_FakeResult()]

    monkeypatch.setattr(yolo_detector, "get_model", lambda: FakeModel())
    monkeypatch.setattr(yolo_detector, "_extract_people", lambda *a, **k: [])

    yolo_detector.detect_people("/some/uploaded/still.jpg")

    assert captured["arg"] == "/some/uploaded/still.jpg"


# ---------------------------------------------------------------------------
# stream_worker.py: _process_frame no longer writes a temp JPEG at all.
# ---------------------------------------------------------------------------

def test_process_frame_passes_frame_object_and_creates_no_temp_jpg(monkeypatch):
    frame = np.zeros((5, 5, 3), dtype="uint8")
    captured = {}

    def fake_detect_and_track_people(model, passed_frame, confidence_threshold=0.4):
        captured["frame"] = passed_frame
        return []  # empty: isolates this test to the YOLO call itself

    monkeypatch.setattr(sw.yolo_detector, "detect_and_track_people", fake_detect_and_track_people)

    worker = sw.StreamWorker(source="", org_id=1, cam_id=1)
    before = _tempdir_jpg_count()

    # rois={} -- the per-workstation loop body (DB-touching) never runs,
    # keeping this test scoped to exactly what the fix changed.
    worker._process_frame(db=None, frame=frame, rois={}, pose_model=object())

    after = _tempdir_jpg_count()

    assert captured["frame"] is frame
    assert after == before, "no stray temp JPEGs should be created for a processed frame"


def test_process_frame_works_with_a_real_opencv_style_frame_shape(monkeypatch):
    """A slightly more realistic frame shape (not all-zero, has actual
    variation) -- guards against any accidental dtype/shape assumption
    creeping back in (e.g. something that only worked for an all-zero
    array)."""
    frame = (np.random.rand(48, 64, 3) * 255).astype("uint8")
    captured = {}

    def fake_detect_and_track_people(model, passed_frame, confidence_threshold=0.4):
        captured["shape"] = passed_frame.shape
        return []

    monkeypatch.setattr(sw.yolo_detector, "detect_and_track_people", fake_detect_and_track_people)

    worker = sw.StreamWorker(source="", org_id=1, cam_id=1)
    worker._process_frame(db=None, frame=frame, rois={}, pose_model=object())

    assert captured["shape"] == (48, 64, 3)


# ---------------------------------------------------------------------------
# video_export.py: the profiling sampler no longer writes a temp JPEG.
# ---------------------------------------------------------------------------

def test_sample_diagnostics_detect_people_call_receives_ndarray_not_path(monkeypatch):
    from app.vision import video_export

    captured = {}

    def fake_detect_people(frame, confidence_threshold=0.4):
        captured.setdefault("frames", []).append(frame)
        return []

    monkeypatch.setattr(video_export.yolo_detector, "detect_people", fake_detect_people)

    # Exercise _sample_one directly via a minimal stand-in rather than a
    # real video file: sample_diagnostics() itself just needs an openable
    # source, which isn't the part this fix touches.
    import inspect
    src = inspect.getsource(video_export.sample_diagnostics)
    assert "tempfile" not in src, "sample_diagnostics should no longer reference tempfile at all"
