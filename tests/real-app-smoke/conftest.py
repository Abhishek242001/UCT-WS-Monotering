import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("YOLO_MODEL_PATH", str(BACKEND_DIR / "yolov8n-pose.pt"))  # reuse the bundled weight, no re-download -- switched from the plain (detection-only) yolov8n.pt now that yolo_detector.py uses a pose model for tracking + activity keypoints

# Import the app and its submodules exactly ONCE for the whole test
# session, so heavy singletons (the YOLO model, and InsightFace if
# installed) load once -- matching real production behavior, where a
# server loads its models once at startup, not per-request. An earlier
# version of this fixture deleted every `app.*` module from sys.modules
# and re-imported them before each test (to force a fresh DATABASE_URL to
# take effect). That worked fine in stub mode, but once real InsightFace
# was installed it meant reloading 5 ONNX models from scratch on every
# single test -- in this project's own 4GB-RAM sandbox, that exhausted
# memory and got the test run killed partway through. See
# app/database.py's reset_engine() for how DATABASE_URL now takes effect
# per test WITHOUT re-importing anything.
import app.database
import app.security
from app.routers import streams as _streams_module
from app.routers import videos as _videos_module
from app.vision import event_bus as _event_bus_module
from app.main import app as _fastapi_app


@pytest.fixture()
def client():
    """Boots the real FastAPI app against a fresh temporary SQLite
    database per test. The app object and its heavy vision-model
    singletons are loaded ONCE for the whole test session (see the
    module-level imports above); only the database and the small amount
    of process-local state that genuinely needs per-test isolation
    (active workers, uploaded videos, failed-login counters, live event
    queues) are reset here, which is fast and memory-safe."""
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test.db")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["ENROLLMENT_PHOTO_DIR"] = os.path.join(tmp_dir, "photos")
    os.environ["DATASET_DIR"] = os.path.join(tmp_dir, "datasets")
    os.environ["UPLOADED_VIDEOS_DIR"] = os.path.join(tmp_dir, "videos")
    os.environ["DEFAULT_ADMIN_PASSWORD"] = "TestPassword123!"

    app.database.reset_engine()             # rebind engine/SessionLocal to the fresh db file
    app.security._failed_logins.clear()     # don't let lockout counters leak between tests
    _streams_module._workers.clear()        # don't let a previous test's worker linger
    _videos_module._videos.clear()
    _event_bus_module._queues.clear()

    from fastapi.testclient import TestClient
    with TestClient(_fastapi_app) as c:
        yield c

    # Stop any StreamWorker threads this test left running (e.g. a video
    # analysis started but never watched to completion via WebSocket).
    # Without this, a background thread can still be using the engine
    # reset() is about to replace out from under it in the NEXT test,
    # which manifested as a real (if rare) crash during test teardown:
    # "RuntimeError: deque mutated during iteration" inside SQLAlchemy's
    # connection-pool event system.
    for worker in list(_streams_module._workers.values()):
        worker.stop()
        worker.join(timeout=3)


@pytest.fixture()
def admin_token(client):
    resp = client.post("/admin/login", json={"username": "hr_admin1", "password": "TestPassword123!"})
    assert resp.status_code == 200
    return resp.json()["session_token"]


@pytest.fixture()
def sample_photo_path():
    """A real photo with real faces in it, bundled with ultralytics for
    exactly this kind of demo/test purpose."""
    import ultralytics
    return str(Path(ultralytics.__file__).parent / "assets" / "zidane.jpg")


@pytest.fixture()
def synthetic_video(sample_photo_path):
    """A real, genuinely-decodable video: 3 empty frames, then 3 frames
    containing the real sample photo (real, YOLO-detectable people).
    Shared across test files (stream worker tests and video-upload/
    analysis tests) since both exercise the same underlying pipeline
    against the same kind of source."""
    import tempfile
    import cv2
    import numpy as np

    photo = cv2.imread(sample_photo_path)
    h, w = photo.shape[:2]
    blank = np.full((h, w, 3), 40, dtype="uint8")

    path = os.path.join(tempfile.mkdtemp(), "synthetic.mp4")
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5, (w, h))
    for _ in range(3):
        writer.write(blank)
    for _ in range(3):
        writer.write(photo)
    writer.release()
    return path
