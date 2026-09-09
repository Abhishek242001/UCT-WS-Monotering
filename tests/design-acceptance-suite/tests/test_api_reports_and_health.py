import pytest


def test_desk_utilization_report_shape(client):
    resp = client.get("/reports/desk-utilization", params={
        "org_id": 1, "cam_id": 101, "from_": "2026-09-01", "to": "2026-09-05",
    })
    assert resp.status_code == 200
    body = resp.json()
    for key in ("org_id", "cam_id", "range", "employees"):
        assert key in body


def test_desk_utilization_range_echoes_request(client):
    resp = client.get("/reports/desk-utilization", params={
        "org_id": 1, "cam_id": 101, "from_": "2026-09-01", "to": "2026-09-05",
    })
    assert resp.json()["range"] == {"from": "2026-09-01", "to": "2026-09-05"}


def test_mismatches_report_shape(client):
    resp = client.get("/reports/mismatches", params={"org_id": 1, "cam_id": 101, "date": "2026-09-05"})
    assert resp.status_code == 200
    body = resp.json()
    for key in ("org_id", "cam_id", "date", "events"):
        assert key in body


@pytest.mark.parametrize("cam_id", [101, 102, 999])
def test_mismatches_report_various_cameras(client, cam_id):
    resp = client.get("/reports/mismatches", params={"org_id": 1, "cam_id": cam_id, "date": "2026-09-05"})
    assert resp.status_code == 200


def test_system_health_shape(client):
    resp = client.get("/system/health", params={"org_id": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert "cameras" in body
    assert "pipeline_status" in body


def test_system_health_default_pipeline_status(client):
    resp = client.get("/system/health", params={"org_id": 1})
    assert resp.json()["pipeline_status"] == "HEALTHY"


VALID_OUTAGE = {"org_id": 1, "cam_id": 101, "start_time": "2026-09-06T02:00:00Z", "reason": "Network maintenance"}


def test_record_outage_success(client):
    resp = client.post("/system/outage", json=VALID_OUTAGE)
    assert resp.status_code == 201
    assert resp.json()["outage_id"].startswith("OUT-")


@pytest.mark.parametrize("missing_field", ["org_id", "cam_id", "start_time", "reason"])
def test_record_outage_missing_field_rejected(client, missing_field):
    payload = dict(VALID_OUTAGE)
    del payload[missing_field]
    resp = client.post("/system/outage", json=payload)
    assert resp.status_code == 422


def test_outage_ids_are_unique(client):
    id1 = client.post("/system/outage", json=VALID_OUTAGE).json()["outage_id"]
    id2 = client.post("/system/outage", json=VALID_OUTAGE).json()["outage_id"]
    assert id1 != id2


@pytest.mark.parametrize("org_id,cam_id", [(1, 101), (2, 202), (99, 1)])
def test_health_and_outage_various_org_cam_combinations(client, org_id, cam_id):
    resp = client.get("/system/health", params={"org_id": org_id})
    assert resp.status_code == 200
    resp2 = client.post("/system/outage", json=dict(VALID_OUTAGE, org_id=org_id, cam_id=cam_id))
    assert resp2.status_code == 201
