"""
Verifies the recognition cadence change: IDENTIFY_HEARTBEAT_SECONDS
default raised from 30 to 60 (the baseline the user asked for), plus a
new, separate, faster retry interval (IDENTIFY_LOW_CONFIDENCE_RETRY_SECONDS,
default 5) that applies specifically when the last result for a desk was
UNKNOWN -- rather than leaving a desk mislabeled for up to a full minute
before the next check.
"""
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def upload(client, admin_token, video_path, org_id=1, filename="demo.mp4"):
    with open(video_path, "rb") as f:
        return client.post("/videos/upload", headers=auth(admin_token),
                            data={"org_id": org_id}, files={"file": (filename, f, "video/mp4")})


# ---------------------------------------------------------------------------
# Pure decision logic: fast, deterministic, no real waiting needed.
# ---------------------------------------------------------------------------

def test_retry_interval_is_normal_heartbeat_when_no_prior_result():
    from app.vision.stream_worker import StreamWorker, HEARTBEAT_SECONDS

    worker = StreamWorker(source="", org_id=1, cam_id=1)
    assert worker._identify_retry_interval_seconds("SomeDesk") == HEARTBEAT_SECONDS


def test_retry_interval_is_normal_heartbeat_after_a_match():
    from app.vision.stream_worker import StreamWorker, HEARTBEAT_SECONDS

    worker = StreamWorker(source="", org_id=1, cam_id=1)
    worker._last_result["SomeDesk"] = ("MATCH", "EMP-1", 0.9)
    assert worker._identify_retry_interval_seconds("SomeDesk") == HEARTBEAT_SECONDS


def test_retry_interval_is_fast_after_unknown():
    from app.vision.stream_worker import StreamWorker, LOW_CONFIDENCE_RETRY_SECONDS

    worker = StreamWorker(source="", org_id=1, cam_id=1)
    worker._last_result["SomeDesk"] = ("UNKNOWN", None, None)
    assert worker._identify_retry_interval_seconds("SomeDesk") == LOW_CONFIDENCE_RETRY_SECONDS


def test_retry_interval_is_normal_heartbeat_after_mismatch():
    """MISMATCH is a CONFIDENT recognition, just of the wrong person for
    this desk's assignment -- not the "can't tell who this is" case the
    fast retry exists for. Documenting this as a deliberate scope choice:
    only UNKNOWN gets the fast retry."""
    from app.vision.stream_worker import StreamWorker, HEARTBEAT_SECONDS

    worker = StreamWorker(source="", org_id=1, cam_id=1)
    worker._last_result["SomeDesk"] = ("MISMATCH", "EMP-2", 0.7)
    assert worker._identify_retry_interval_seconds("SomeDesk") == HEARTBEAT_SECONDS


def test_default_heartbeat_is_now_60_seconds():
    from app.vision.stream_worker import HEARTBEAT_SECONDS
    assert HEARTBEAT_SECONDS == 60


# ---------------------------------------------------------------------------
# Real elapsed-time integration test: confirms an UNKNOWN desk actually
# gets re-checked more often than a MATCHed one, over the same real
# wall-clock window -- not just that the pure decision function looks
# right in isolation.
# ---------------------------------------------------------------------------

def test_unknown_desk_is_retried_more_often_than_a_matched_one(client, admin_token, monkeypatch, tmp_path):
    """Two workstations, same camera, same synthetic video (continuously
    occupied for its whole length): one has NO enrolled employee (so
    every identify() call there returns UNKNOWN, no matter how many
    times it's retried), the other has a correctly enrolled match. Both
    monkeypatched to short, fast intervals so this completes in real
    seconds, not real minutes. The UNKNOWN desk should accumulate
    noticeably more identification-triggering events than the matched
    one over the same run, proving the fast-retry path actually fires
    repeatedly rather than just being reachable in theory.
    """
    import cv2
    import numpy as np
    import ultralytics
    from app.vision import stream_worker as sw_module

    monkeypatch.setattr(sw_module, "HEARTBEAT_SECONDS", 10.0)
    monkeypatch.setattr(sw_module, "LOW_CONFIDENCE_RETRY_SECONDS", 0.2)

    # A longer synthetic video than the shared fixture: ~3 real seconds
    # of continuously-occupied frames at a small poll interval, so real
    # wall-clock time actually elapses between processed frames (the
    # heartbeat/retry timers use time.monotonic(), real time -- there is
    # no simulated clock to fast-forward here).
    photo_path = str(Path(ultralytics.__file__).parent / "assets" / "zidane.jpg")
    photo = cv2.imread(photo_path)
    video_path = str(tmp_path / "long_synthetic.mp4")
    writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*"mp4v"), 5, (photo.shape[1], photo.shape[0]))
    for _ in range(15):
        writer.write(photo)
    writer.release()

    h = {"Authorization": f"Bearer {admin_token}"}
    # EMP-CADENCE-MATCH is enrolled with the real sample photo, so
    # matches every time; UnmatchedDesk has no enrollment at all -- the
    # gallery is empty for it, so every identify() call there is UNKNOWN.
    client.post("/employees/enroll", headers=h,
                data={"org_id": 1, "employee_id": "EMP-CADENCE-MATCH", "name": "Cadence Match",
                      "view": "front", "depth_m": 1.0},
                files={"photo": ("front.jpg", open(photo_path, "rb"), "image/jpeg")})
    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 970,
        "workstations": [
            {"name": "MatchedDesk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0},
        ],
    })
    client.post("/workstations/assign", headers=h, json={
        "org_id": 1, "cam_id": 970, "workstation_name": "MatchedDesk",
        "employee_id": "EMP-CADENCE-MATCH", "effective_from": "2026-01-01",
    })

    from app.vision.stream_worker import StreamWorker
    matched_worker = StreamWorker(source=video_path, org_id=1, cam_id=970, poll_interval_seconds=0.2)
    matched_worker.start()
    matched_worker.join(timeout=30)

    # A second org/cam with the SAME video, but with NO employee enrolled
    # at all for org_id=2 -- the gallery lookup finds nothing, so
    # match_single_pass() returns None and every identify() call here is
    # UNKNOWN, unconditionally, no matter how many times it's retried.
    client.post("/workstations/save", headers=h, json={
        "org_id": 2, "cam_id": 971,
        "workstations": [{"name": "UnmatchedDesk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    unmatched_worker = StreamWorker(source=video_path, org_id=2, cam_id=971, poll_interval_seconds=0.2)
    unmatched_worker.start()
    unmatched_worker.join(timeout=30)

    import sqlite3
    db_url = __import__("os").environ["DATABASE_URL"]
    conn = sqlite3.connect(db_url.replace("sqlite:///", ""))
    matched_events = conn.execute(
        "SELECT event_type FROM workstation_identity_events WHERE cam_id=970 AND event_type != 'VACANT' ORDER BY id"
    ).fetchall()
    unmatched_events = conn.execute(
        "SELECT event_type FROM workstation_identity_events WHERE cam_id=971 AND event_type != 'VACANT' ORDER BY id"
    ).fetchall()
    conn.close()

    assert all(e[0] == "MATCH" for e in matched_events), matched_events
    assert all(e[0] == "UNKNOWN" for e in unmatched_events), unmatched_events
    assert len(unmatched_events) > len(matched_events), (
        f"expected the UNKNOWN desk (fast {sw_module.LOW_CONFIDENCE_RETRY_SECONDS}s retry) to accumulate "
        f"more identify-triggering events than the matched desk (slow {sw_module.HEARTBEAT_SECONDS}s heartbeat) "
        f"over the same run: matched={len(matched_events)}, unmatched={len(unmatched_events)}"
    )
