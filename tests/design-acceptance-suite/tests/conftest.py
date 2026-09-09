import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mock_app import app, DB, _reset_db  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "schema.sql"


@pytest.fixture()
def client():
    """A TestClient against the mock API, with in-memory state reset before
    and after every test so tests never leak state into one another."""
    _reset_db()
    yield TestClient(app)
    _reset_db()


@pytest.fixture()
def db():
    """A fresh in-memory SQLite connection with the full 17-table schema
    applied, foreign keys enforced, for real constraint-behaviour tests."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    yield conn
    conn.close()


@pytest.fixture()
def sample_employee():
    return {
        "org_id": 1,
        "employee_id": "EMP-1001",
        "name": "Test Employee",
        "captures": [
            {"view": v, "depth_m": 1.0, "image_base64": "AAAA"}
            for v in ("front", "left", "right", "top")
        ],
    }


@pytest.fixture()
def enrolled_employee(client, sample_employee):
    resp = client.post("/employees/enroll", json=sample_employee)
    assert resp.status_code == 201
    return sample_employee


@pytest.fixture()
def sample_workstation():
    return {"org_id": 1, "cam_id": 101, "workstations": [
        {"name": "Desk-A", "x1": 0.1, "y1": 0.2, "x2": 0.4, "y2": 0.8},
    ]}


@pytest.fixture()
def saved_workstation(client, sample_workstation):
    resp = client.post("/workstations/save", json=sample_workstation)
    assert resp.status_code == 200
    return sample_workstation


@pytest.fixture()
def admin_token(client):
    resp = client.post("/admin/login", json={"username": "hr_admin1", "password": "correct-horse-battery-staple"})
    assert resp.status_code == 200
    return resp.json()["session_token"]
