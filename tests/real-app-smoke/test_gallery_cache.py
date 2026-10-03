"""
Tests StreamWorker._get_gallery()'s new in-memory cache: the gallery
should be queried from the database at most once per
GALLERY_CACHE_TTL_SECONDS, not on every single identify() call. Uses a
fake db/row pair instead of a real database, so this needs no DB setup
and no real YOLO/InsightFace model.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.vision import stream_worker as sw  # noqa: E402


class FakeEmployee:
    def __init__(self, name):
        self.name = name


class FakeGalleryRow:
    def __init__(self, employee_id, view, employee_name, embedding=(0.1, 0.2, 0.3)):
        self.employee_id = employee_id
        self.view = view
        self.embedding = "placeholder"  # only needs to be truthy for the `if row.embedding` filter
        self._embedding_value = list(embedding)
        self.employee = FakeEmployee(employee_name)

    def get_embedding(self):
        return self._embedding_value


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def join(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class FakeDB:
    """Counts how many times .query() was actually called, so tests can
    assert the cache is genuinely avoiding repeat queries, not just
    returning equal-looking data coincidentally."""
    def __init__(self, rows):
        self._rows = rows
        self.query_count = 0

    def query(self, model):
        self.query_count += 1
        return FakeQuery(self._rows)


def _worker():
    return sw.StreamWorker(source="", org_id=1, cam_id=1)


def test_default_ttl_is_five_minutes():
    # Documents the chosen default; if this changes on purpose, update
    # the comment next to GALLERY_CACHE_TTL_SECONDS in stream_worker.py
    # at the same time.
    assert sw.GALLERY_CACHE_TTL_SECONDS == 300.0


def test_gallery_queries_database_on_first_call():
    worker = _worker()
    db = FakeDB([FakeGalleryRow("EMP-1", "front", "Alice")])

    grouped, names = worker._get_gallery(db)

    assert db.query_count == 1
    assert names == {"EMP-1": "Alice"}
    assert "EMP-1" in grouped


def test_gallery_is_served_from_cache_within_ttl():
    worker = _worker()
    db = FakeDB([FakeGalleryRow("EMP-1", "front", "Alice")])

    grouped1, names1 = worker._get_gallery(db)
    grouped2, names2 = worker._get_gallery(db)
    grouped3, names3 = worker._get_gallery(db)

    assert db.query_count == 1  # still just the one query from the first call
    assert grouped2 is grouped1
    assert names2 is names1
    assert grouped3 is grouped1


def test_gallery_reloads_once_ttl_has_elapsed():
    worker = _worker()
    db = FakeDB([FakeGalleryRow("EMP-1", "front", "Alice")])

    worker._get_gallery(db)
    assert db.query_count == 1

    # Simulate TTL expiry without actually waiting -- back-date the
    # cache's recorded load time past the TTL window.
    worker._gallery_cache["loaded_at"] -= (sw.GALLERY_CACHE_TTL_SECONDS + 1)

    worker._get_gallery(db)
    assert db.query_count == 2


def test_reloaded_gallery_reflects_updated_rows():
    """The whole point of a TTL (vs. caching forever) -- after an
    employee is re-enrolled and the TTL elapses, the new embedding
    should actually be picked up, not the stale cached one."""
    worker = _worker()
    db = FakeDB([FakeGalleryRow("EMP-1", "front", "Alice (old photo)")])

    _, names1 = worker._get_gallery(db)
    assert names1 == {"EMP-1": "Alice (old photo)"}

    db._rows = [FakeGalleryRow("EMP-1", "front", "Alice (corrected photo)")]
    worker._gallery_cache["loaded_at"] -= (sw.GALLERY_CACHE_TTL_SECONDS + 1)

    _, names2 = worker._get_gallery(db)
    assert names2 == {"EMP-1": "Alice (corrected photo)"}
    assert db.query_count == 2


def test_each_worker_instance_has_its_own_independent_cache():
    """Two StreamWorkers (e.g. two cameras) must not share one gallery
    cache -- each should query independently on its own first call."""
    worker_a = _worker()
    worker_b = _worker()
    db_a = FakeDB([FakeGalleryRow("EMP-1", "front", "Alice")])
    db_b = FakeDB([FakeGalleryRow("EMP-2", "front", "Bob")])

    _, names_a = worker_a._get_gallery(db_a)
    _, names_b = worker_b._get_gallery(db_b)

    assert names_a == {"EMP-1": "Alice"}
    assert names_b == {"EMP-2": "Bob"}
    assert db_a.query_count == 1
    assert db_b.query_count == 1
