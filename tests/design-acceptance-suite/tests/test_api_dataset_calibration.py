import pytest


def upload(client, org_id=1, file_name="dataset.zip", size_bytes=1024):
    return client.post("/dataset/upload", params={"org_id": org_id, "file_name": file_name, "size_bytes": size_bytes})


def test_upload_success(client):
    resp = upload(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "received"
    assert body["upload_id"].startswith("UPL-")


@pytest.mark.parametrize("file_name", ["dataset.rar", "dataset.tar.gz", "dataset", "dataset.docx"])
def test_upload_rejects_non_zip_files(client, file_name):
    resp = upload(client, file_name=file_name)
    assert resp.status_code == 422


@pytest.mark.parametrize("size_bytes", [0, -1, -1000])
def test_upload_rejects_nonpositive_size(client, size_bytes):
    resp = upload(client, size_bytes=size_bytes)
    assert resp.status_code == 422


def test_upload_rejects_oversized_file(client):
    resp = upload(client, size_bytes=300 * 1024 * 1024)  # 300MB > 200MB cap
    assert resp.status_code == 413


def test_upload_ids_are_unique(client):
    id1 = upload(client).json()["upload_id"]
    id2 = upload(client).json()["upload_id"]
    assert id1 != id2


def test_validate_unknown_upload_id_returns_404(client):
    resp = client.get("/dataset/validate/UPL-does-not-exist")
    assert resp.status_code == 404


def test_validate_known_upload_returns_summary_shape(client):
    upload_id = upload(client).json()["upload_id"]
    resp = client.get(f"/dataset/validate/{upload_id}")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("upload_id", "status", "errors", "warnings", "summary"):
        assert key in body
    for key in ("people_found", "calibration_volunteers_found", "total_images", "faces_not_detected"):
        assert key in body["summary"]


def test_calibration_run_requires_valid_upload_id(client):
    resp = client.post("/calibration/run", json={"org_id": 1, "upload_id": "UPL-nope"})
    assert resp.status_code == 404


def test_calibration_run_success(client):
    upload_id = upload(client).json()["upload_id"]
    resp = client.post("/calibration/run", json={"org_id": 1, "upload_id": upload_id})
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"


def test_calibration_status_unknown_job_returns_404(client):
    resp = client.get("/calibration/status/CAL-JOB-nope")
    assert resp.status_code == 404


def test_calibration_status_returns_fitted_parameters(client):
    upload_id = upload(client).json()["upload_id"]
    job_id = client.post("/calibration/run", json={"org_id": 1, "upload_id": upload_id}).json()["job_id"]
    resp = client.get(f"/calibration/status/{job_id}")
    body = resp.json()
    assert body["status"] == "COMPLETED"
    for key in ("near_k", "near_sigma0", "near_gamma", "people_enrolled"):
        assert key in body["result"]


def test_calibration_job_ids_are_unique(client):
    upload_id = upload(client).json()["upload_id"]
    job1 = client.post("/calibration/run", json={"org_id": 1, "upload_id": upload_id}).json()["job_id"]
    job2 = client.post("/calibration/run", json={"org_id": 1, "upload_id": upload_id}).json()["job_id"]
    assert job1 != job2


@pytest.mark.parametrize("size_bytes", [1, 1024, 1024 * 1024, 50 * 1024 * 1024, 200 * 1024 * 1024])
def test_upload_accepts_range_of_valid_sizes(client, size_bytes):
    resp = upload(client, size_bytes=size_bytes)
    assert resp.status_code == 201


# --- folder-name / structural validation regex, tested directly ------------
# (Mirrors ADAR's depth_XXXm parsing convention documented in Section 5.2.)

import re

DEPTH_RE = re.compile(r"^depth_(\d+(?:\.\d+)?)m$")


@pytest.mark.parametrize("folder_name,expected_depth", [
    ("depth_001m", 1.0),
    ("depth_003m", 3.0),
    ("depth_000.50m", 0.5),
    ("depth_004m", 4.0),
    ("depth_020m", 20.0),
    ("depth_150m", 150.0),
])
def test_depth_folder_regex_parses_valid_names(folder_name, expected_depth):
    match = DEPTH_RE.match(folder_name)
    assert match is not None
    assert float(match.group(1)) == expected_depth


@pytest.mark.parametrize("folder_name", [
    "Depth_001m", "depth_001", "depth-001m", "001m", "depth_abcm", "",
])
def test_depth_folder_regex_rejects_invalid_names(folder_name):
    # These are intentionally "close but wrong" to catch loose regexes.
    assert DEPTH_RE.match(folder_name) is None


def test_depth_folder_regex_accepts_unpadded_single_digit():
    # Not zero-padded, but still structurally valid per the \d+ pattern.
    match = DEPTH_RE.match("depth_1m")
    assert match is not None
    assert float(match.group(1)) == 1.0


@pytest.mark.parametrize("view", ["front", "left", "right", "top"])
def test_required_views_are_exactly_four(view):
    required_views = {"front", "left", "right", "top"}
    assert view in required_views
    assert len(required_views) == 4
