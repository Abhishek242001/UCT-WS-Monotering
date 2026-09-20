"""
Verifies item 11's backend piece: a real, persisted record of past Video
Analysis runs. Previously nothing tracked that a video had ever been
analyzed, when, with what org/cam, or how it went -- only the ephemeral
in-memory stream_id (lost on restart or once the WebSocket closed).
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_analyze_creates_a_run_record_in_progress(client, admin_token, synthetic_video):
    h = auth(admin_token)
    with open(synthetic_video, "rb") as f:
        up = client.post("/videos/upload", headers=h, data={"org_id": 1}, files={"file": ("history_test.mp4", f, "video/mp4")})
    video_id = up.json()["video_id"]

    analyze = client.post(f"/videos/{video_id}/analyze", headers=h, data={"max_frames": 6})
    stream_id = analyze.json()["stream_id"]

    runs = client.get("/videos/analysis_runs", headers=h, params={"org_id": 1}).json()["runs"]
    match = next((r for r in runs if r["stream_id"] == stream_id), None)
    assert match is not None, f"expected a run record for {stream_id}, got: {runs}"
    assert match["video_id"] == video_id
    assert match["filename"] == "history_test.mp4"
    assert match["status"] == "running"
    assert match["completed_at"] is None
    assert match["frames_processed"] is None


def test_run_closes_out_as_completed_after_the_stream_finishes(client, admin_token, synthetic_video):
    from app.routers import streams as streams_router

    h = auth(admin_token)
    with open(synthetic_video, "rb") as f:
        up = client.post("/videos/upload", headers=h, data={"org_id": 1}, files={"file": ("history_test2.mp4", f, "video/mp4")})
    video_id = up.json()["video_id"]

    analyze = client.post(f"/videos/{video_id}/analyze", headers=h, data={"max_frames": 6, "poll_interval_seconds": 0})
    stream_id = analyze.json()["stream_id"]

    worker = streams_router.get_worker(stream_id)
    worker.join(timeout=30)  # wait for the actual background thread to finish, not just the HTTP response

    runs = client.get("/videos/analysis_runs", headers=h, params={"org_id": 1}).json()["runs"]
    match = next(r for r in runs if r["stream_id"] == stream_id)
    assert match["status"] == "completed"
    assert match["completed_at"] is not None
    assert match["frames_processed"] == 6


def test_analysis_runs_scoped_per_org(client, admin_token, synthetic_video):
    h = auth(admin_token)
    with open(synthetic_video, "rb") as f:
        up1 = client.post("/videos/upload", headers=h, data={"org_id": 1}, files={"file": ("org1.mp4", f, "video/mp4")})
    with open(synthetic_video, "rb") as f:
        up2 = client.post("/videos/upload", headers=h, data={"org_id": 2}, files={"file": ("org2.mp4", f, "video/mp4")})

    r1 = client.post(f"/videos/{up1.json()['video_id']}/analyze", headers=h, data={"max_frames": 1}).json()
    r2 = client.post(f"/videos/{up2.json()['video_id']}/analyze", headers=h, data={"org_id": 2, "max_frames": 1}).json()

    org1_runs = [r["stream_id"] for r in client.get("/videos/analysis_runs", headers=h, params={"org_id": 1}).json()["runs"]]
    org2_runs = [r["stream_id"] for r in client.get("/videos/analysis_runs", headers=h, params={"org_id": 2}).json()["runs"]]
    assert r1["stream_id"] in org1_runs
    assert r1["stream_id"] not in org2_runs
    assert r2["stream_id"] in org2_runs
    assert r2["stream_id"] not in org1_runs


def test_live_stream_does_not_create_a_video_analysis_run(client, admin_token, synthetic_video):
    """A live camera stream's stream_id has no corresponding row --
    confirms StreamWorker's run-closing lookup is a harmless no-op for
    live streams, and that /streams/start doesn't accidentally create a
    VideoAnalysisRun entry the way /videos/.../analyze does."""
    h = auth(admin_token)
    resp = client.post("/streams/start", headers=h, json={
        "source": synthetic_video, "org_id": 1, "cam_id": 995, "user_id": 1, "max_frames": 1,
    })
    stream_id = resp.json()["stream_id"]

    runs = client.get("/videos/analysis_runs", headers=h, params={"org_id": 1}).json()["runs"]
    assert not any(r["stream_id"] == stream_id for r in runs)
