import pytest


VALID_START = {"source": "rtsp://cam/1", "org_id": 1, "cam_id": 101, "user_id": 1, "use_nvenc": False}


def test_health_check(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "workstation-monitoring"}


def test_streams_start_success(client):
    resp = client.post("/streams/start", json=VALID_START)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "started"
    assert "stream_id" in body
    assert body["hls_url"].startswith(f"/hls/{body['stream_id']}/")


def test_streams_start_returns_unique_ids(client):
    id1 = client.post("/streams/start", json=VALID_START).json()["stream_id"]
    id2 = client.post("/streams/start", json=VALID_START).json()["stream_id"]
    assert id1 != id2


@pytest.mark.parametrize("missing_field", ["source", "org_id", "cam_id", "user_id"])
def test_streams_start_missing_required_field_rejected(client, missing_field):
    payload = dict(VALID_START)
    del payload[missing_field]
    resp = client.post("/streams/start", json=payload)
    assert resp.status_code == 422


def test_streams_start_empty_source_rejected(client):
    resp = client.post("/streams/start", json=dict(VALID_START, source=""))
    assert resp.status_code == 422


def test_streams_start_defaults_use_nvenc_false(client):
    payload = dict(VALID_START)
    del payload["use_nvenc"]
    resp = client.post("/streams/start", json=payload)
    assert resp.status_code == 201


def test_streams_stop_success(client):
    stream_id = client.post("/streams/start", json=VALID_START).json()["stream_id"]
    resp = client.post("/streams/stop", json={"stream_id": stream_id})
    assert resp.status_code == 200
    assert resp.json() == {"status": "stopped", "stream_id": stream_id}


def test_streams_stop_unknown_id_returns_404(client):
    resp = client.post("/streams/stop", json={"stream_id": "does-not-exist"})
    assert resp.status_code == 404


def test_streams_stop_is_not_idempotent_second_call_404s(client):
    stream_id = client.post("/streams/start", json=VALID_START).json()["stream_id"]
    client.post("/streams/stop", json={"stream_id": stream_id})
    resp = client.post("/streams/stop", json={"stream_id": stream_id})
    assert resp.status_code == 404


def test_streams_list_empty_initially(client):
    resp = client.get("/streams/list")
    assert resp.status_code == 200
    assert resp.json() == {"streams": []}


def test_streams_list_reflects_started_streams(client):
    client.post("/streams/start", json=VALID_START)
    client.post("/streams/start", json=dict(VALID_START, cam_id=102))
    resp = client.get("/streams/list")
    assert len(resp.json()["streams"]) == 2


def test_streams_list_omits_stopped_streams(client):
    stream_id = client.post("/streams/start", json=VALID_START).json()["stream_id"]
    client.post("/streams/stop", json={"stream_id": stream_id})
    resp = client.get("/streams/list")
    assert resp.json()["streams"] == []


@pytest.mark.parametrize("cam_id,org_id", [(101, 1), (999, 42), (1, 1), (0, 0)])
def test_streams_start_various_org_cam_ids(client, cam_id, org_id):
    payload = dict(VALID_START, cam_id=cam_id, org_id=org_id)
    resp = client.post("/streams/start", json=payload)
    assert resp.status_code == 201


def test_streams_list_entries_include_hls_url(client):
    client.post("/streams/start", json=VALID_START)
    entry = client.get("/streams/list").json()["streams"][0]
    assert "hls_url" in entry and entry["hls_url"].endswith("playlist.m3u8")
