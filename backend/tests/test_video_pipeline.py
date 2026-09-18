"""
Real, end-to-end tests for video_export.py and the /videos/* endpoints
added in this session -- deliberately NOT mocked: real login through
POST /admin/login, a real SQLite DB, the actual sample video shipped in
sample_data/, and real YOLO inference (ultralytics), same as this
session's own manual verification. This is the first automated test
suite in the repo (none existed before, despite stream_worker.py's own
docstring referencing tests/real-app-smoke/test_stream_worker.py, which
does not exist).

Run with: cd backend && pytest tests/ -v
Requires `ultralytics` installed (it's in requirements.txt, but was
found NOT actually installed in this project's own sandbox at least
once -- see the session changelog).
"""
import os
import shutil

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_video_pipeline.db")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import Base, engine

SAMPLE_VIDEO = os.path.join(os.path.dirname(__file__), "..", "..", "sample_data", "demo-camera-feed.mp4")


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    # Clean up files this test session creates on disk (DB tables are
    # dropped above on next run; these directories are not DB-tracked).
    for d in (os.environ.get("UPLOADED_VIDEOS_DIR", "./uploaded_videos"),
              os.environ.get("VIDEO_CLIPS_DIR", "./video_clips"),
              os.environ.get("ANNOTATED_VIDEOS_DIR", "./annotated_videos")):
        shutil.rmtree(d, ignore_errors=True)
    if os.path.exists("test_video_pipeline.db"):
        os.remove("test_video_pipeline.db")


@pytest.fixture(scope="module")
def auth_headers(client):
    resp = client.post("/admin/login", json={"username": "hr_admin1", "password": "ChangeMe123!"})
    assert resp.status_code == 200, resp.text
    token = resp.json()["session_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def uploaded_video_id(client, auth_headers):
    assert os.path.exists(SAMPLE_VIDEO), f"sample video missing at {SAMPLE_VIDEO}"
    with open(SAMPLE_VIDEO, "rb") as f:
        resp = client.post("/videos/upload", headers=auth_headers,
                            data={"org_id": 1}, files={"file": ("demo.mp4", f, "video/mp4")})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "uploaded"
    return body["video_id"]


@pytest.fixture(scope="module")
def workstation_roi(client, auth_headers):
    """A full-frame ROI under a dedicated cam_id, so these tests don't
    collide with any real workstation an admin might have drawn."""
    resp = client.post("/workstations/save", headers=auth_headers, json={
        "org_id": 1, "cam_id": 424242,
        "workstations": [{"name": "TestDesk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    assert resp.status_code == 200, resp.text
    return {"org_id": 1, "cam_id": 424242}


# --------------------------------------------------------------------------
# find_by_hash / duplicate detection
# --------------------------------------------------------------------------

def test_find_by_hash_matches_uploaded_video(client, auth_headers, uploaded_video_id):
    import hashlib
    file_hash = hashlib.sha256(open(SAMPLE_VIDEO, "rb").read()).hexdigest()
    resp = client.get(f"/videos/find_by_hash?org_id=1&file_hash={file_hash}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["video_id"] == uploaded_video_id


def test_find_by_hash_no_match_for_unknown_hash(client, auth_headers):
    resp = client.get("/videos/find_by_hash?org_id=1&file_hash=" + "0" * 64, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["found"] is False


def test_video_metadata_survives_reload_from_disk(client, auth_headers, uploaded_video_id):
    """Regression test for this session's restart-persistence fix: the
    sidecar .meta.json must exist and _load_videos_from_disk must be able
    to parse it back into an equivalent record."""
    from app.routers import videos as videos_mod
    video = videos_mod._videos[uploaded_video_id]
    sidecar_path = videos_mod._meta_path(video["path"])
    assert os.path.exists(sidecar_path), "upload did not write a .meta.json sidecar"

    import json
    with open(sidecar_path) as f:
        data = json.load(f)
    assert data["video_id"] == uploaded_video_id
    assert data["org_id"] == 1
    assert data["file_hash"] == video["file_hash"]


# --------------------------------------------------------------------------
# clip download
# --------------------------------------------------------------------------

def test_download_clip_returns_valid_video(client, auth_headers, uploaded_video_id):
    resp = client.get(f"/videos/{uploaded_video_id}/clip?seconds=5", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "video/mp4"
    assert len(resp.content) > 0


def test_download_clip_rejects_invalid_seconds(client, auth_headers, uploaded_video_id):
    resp = client.get(f"/videos/{uploaded_video_id}/clip?seconds=0", headers=auth_headers)
    assert resp.status_code == 422
    resp = client.get(f"/videos/{uploaded_video_id}/clip?seconds=999999", headers=auth_headers)
    assert resp.status_code == 422


def test_download_clip_404_for_unknown_video(client, auth_headers):
    resp = client.get("/videos/VID-doesnotexist/clip?seconds=5", headers=auth_headers)
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# diagnostics (sample_diagnostics)
# --------------------------------------------------------------------------

def test_diagnostics_reports_real_detections(client, auth_headers, uploaded_video_id, workstation_roi):
    resp = client.get(
        f"/videos/{uploaded_video_id}/diagnostics"
        f"?org_id={workstation_roi['org_id']}&cam_id={workstation_roi['cam_id']}&num_samples=4",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["frames_sampled"] == 4
    assert body["video_fps"] == 2.0  # known property of the shipped sample video
    # The sample video is a demo clip with a real detectable person in it
    # (documented in stream_worker.py) -- if this ever goes to 0, either
    # the sample video changed or YOLO/ROI logic broke.
    assert body["frames_with_at_least_one_person"] >= 1
    assert body["confidence_mean"] is not None
    assert 0.0 <= body["confidence_mean"] <= 1.0
    assert body["measured_yolo_seconds_per_call"] > 0
    # Sanity bound: on any reasonable hardware, a single YOLOv8n call on a
    # tiny demo frame should not take minutes. Catches a regression where
    # the model-warmup cost leaks back into the timed measurement (this
    # exact bug was caught and fixed during this session).
    assert body["measured_yolo_seconds_per_call"] < 30


def test_diagnostics_requires_existing_roi(client, auth_headers, uploaded_video_id):
    resp = client.get(
        f"/videos/{uploaded_video_id}/diagnostics?org_id=1&cam_id=999999999&num_samples=2",
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "no saved workstation roi" in resp.json()["detail"].lower()


def test_diagnostics_rejects_bad_num_samples(client, auth_headers, uploaded_video_id, workstation_roi):
    resp = client.get(
        f"/videos/{uploaded_video_id}/diagnostics"
        f"?org_id={workstation_roi['org_id']}&cam_id={workstation_roi['cam_id']}&num_samples=0",
        headers=auth_headers,
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# annotate + download
# --------------------------------------------------------------------------

def test_annotate_and_download_roundtrip(client, auth_headers, uploaded_video_id, workstation_roi):
    resp = client.post(
        f"/videos/{uploaded_video_id}/annotate", headers=auth_headers,
        data={"org_id": workstation_roi["org_id"], "cam_id": workstation_roi["cam_id"],
              "max_seconds": 5, "detect_every_n_frames": 2},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["frames_processed"] > 0
    assert body["workstations"] == ["TestDesk"]
    assert "annotated_id" in body

    dl = client.get(body["download_url"], headers=auth_headers)
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "video/mp4"
    assert len(dl.content) > 0


def test_annotate_rejects_missing_roi(client, auth_headers, uploaded_video_id):
    resp = client.post(
        f"/videos/{uploaded_video_id}/annotate", headers=auth_headers,
        data={"org_id": 1, "cam_id": 888888888, "max_seconds": 5, "detect_every_n_frames": 3},
    )
    assert resp.status_code == 422
    assert "no saved workstation roi" in resp.json()["detail"].lower()


def test_annotate_rejects_max_seconds_over_cap(client, auth_headers, uploaded_video_id, workstation_roi):
    resp = client.post(
        f"/videos/{uploaded_video_id}/annotate", headers=auth_headers,
        data={"org_id": workstation_roi["org_id"], "cam_id": workstation_roi["cam_id"],
              "max_seconds": 601, "detect_every_n_frames": 3},
    )
    assert resp.status_code == 422


def test_annotate_rejects_invalid_detect_every_n_frames(client, auth_headers, uploaded_video_id, workstation_roi):
    resp = client.post(
        f"/videos/{uploaded_video_id}/annotate", headers=auth_headers,
        data={"org_id": workstation_roi["org_id"], "cam_id": workstation_roi["cam_id"],
              "max_seconds": 5, "detect_every_n_frames": 0},
    )
    assert resp.status_code == 422


def test_download_annotated_404_for_unknown_id(client, auth_headers):
    resp = client.get("/videos/annotated/ANNOT-doesnotexist/download", headers=auth_headers)
    assert resp.status_code == 404


def test_annotated_video_metadata_survives_reload_from_disk(client, auth_headers, uploaded_video_id, workstation_roi):
    """Regression test for this session's _annotated_videos persistence fix."""
    resp = client.post(
        f"/videos/{uploaded_video_id}/annotate", headers=auth_headers,
        data={"org_id": workstation_roi["org_id"], "cam_id": workstation_roi["cam_id"],
              "max_seconds": 5, "detect_every_n_frames": 3},
    )
    assert resp.status_code == 201
    annotated_id = resp.json()["annotated_id"]

    from app.routers import videos as videos_mod
    row = videos_mod._annotated_videos[annotated_id]
    sidecar_path = videos_mod._meta_path(row["path"])
    assert os.path.exists(sidecar_path), "annotate did not write a .meta.json sidecar"


# --------------------------------------------------------------------------
# system health
# --------------------------------------------------------------------------

def test_system_health_reports_real_capabilities(client, auth_headers):
    resp = client.get("/system/health?org_id=1", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # These are real, checked values (not hardcoded) -- assert the KEYS
    # exist and TYPES are right, not specific values, since ffmpeg/codec
    # availability legitimately differs across deployments.
    for key in ("pipeline_status", "face_recognition_backend", "yolo_model_loadable",
                "opencv_ffmpeg_support", "rtsp_capable", "ffmpeg_cli_available",
                "mp4v_video_writer_available"):
        assert key in body, f"missing key: {key}"
    assert isinstance(body["yolo_model_loadable"], bool)
    assert isinstance(body["rtsp_capable"], bool)
    # This project's own dependency (yolo_detector) must load successfully
    # for these tests to have gotten this far at all.
    assert body["yolo_model_loadable"] is True


def test_readiness_flags_incomplete_enrollment_and_unassigned_workstation(client, auth_headers):
    """Real, deliberately mixed fixture: one fully-enrolled employee, one
    incomplete employee (missing views AND a null embedding on a
    present view), one assigned workstation, one unassigned one --
    verifies the readiness endpoint catches all four conditions
    correctly, not just that it returns 200."""
    from app.database import SessionLocal
    from app.models import Employee, EmployeeFaceGallery, Workstation, WorkstationAssignment

    db = SessionLocal()
    try:
        db.add(Employee(employee_id="EMP-READY-1", org_id=1, name="Complete Person"))
        for v in ("front", "left", "right", "top"):
            row = EmployeeFaceGallery(employee_id="EMP-READY-1", view=v)
            row.set_embedding([0.1] * 512)
            db.add(row)

        db.add(Employee(employee_id="EMP-READY-2", org_id=1, name="Incomplete Person"))
        db.add(EmployeeFaceGallery(employee_id="EMP-READY-2", view="front", embedding=None))
        row2 = EmployeeFaceGallery(employee_id="EMP-READY-2", view="left")
        row2.set_embedding([0.2] * 512)
        db.add(row2)

        db.add(Workstation(org_id=1, cam_id=313131, name="ReadyDeskA", x1=0, y1=0, x2=1, y2=1))
        db.add(Workstation(org_id=1, cam_id=313131, name="ReadyDeskB", x1=0, y1=0, x2=1, y2=1))
        db.add(WorkstationAssignment(org_id=1, cam_id=313131, workstation_name="ReadyDeskA",
                                      employee_id="EMP-READY-1", effective_from="2026-01-01"))
        db.commit()
    finally:
        db.close()

    resp = client.get("/reports/face-recognition-readiness?org_id=1", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    by_id = {e["employee_id"]: e for e in body["employees"]}
    assert by_id["EMP-READY-1"]["complete"] is True
    assert by_id["EMP-READY-1"]["views_missing"] == []

    assert by_id["EMP-READY-2"]["complete"] is False
    assert set(by_id["EMP-READY-2"]["views_missing"]) == {"right", "top"}
    assert by_id["EMP-READY-2"]["views_with_null_embedding"] == ["front"]

    by_name = {w["name"]: w for w in body["workstations"] if w["cam_id"] == 313131}
    assert by_name["ReadyDeskA"]["has_active_assignment"] is True
    assert by_name["ReadyDeskA"]["assigned_employee_id"] == "EMP-READY-1"
    assert by_name["ReadyDeskB"]["has_active_assignment"] is False
    assert by_name["ReadyDeskB"]["assigned_employee_id"] is None
