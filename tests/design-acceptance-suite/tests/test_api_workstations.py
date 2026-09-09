import pytest


VALID_SAVE = {"org_id": 1, "cam_id": 101, "workstations": [
    {"name": "Desk-A", "x1": 0.1, "y1": 0.2, "x2": 0.4, "y2": 0.8},
]}


def test_save_workstation_success(client):
    resp = client.post("/workstations/save", json=VALID_SAVE)
    assert resp.status_code == 200
    assert resp.json() == {"status": "saved", "org_id": 1, "cam_id": 101, "count": 1}


def test_save_multiple_workstations_at_once(client):
    payload = {"org_id": 1, "cam_id": 101, "workstations": [
        {"name": "Desk-A", "x1": 0.0, "y1": 0.0, "x2": 0.3, "y2": 0.5},
        {"name": "Desk-B", "x1": 0.4, "y1": 0.0, "x2": 0.7, "y2": 0.5},
        {"name": "Desk-C", "x1": 0.7, "y1": 0.0, "x2": 1.0, "y2": 0.5},
    ]}
    resp = client.post("/workstations/save", json=payload)
    assert resp.json()["count"] == 3


@pytest.mark.parametrize("field,bad_value", [
    ("x1", -0.1), ("x1", 1.1), ("y1", -0.5), ("y2", 1.5), ("x2", 2.0), ("y1", -0.01),
])
def test_save_rejects_out_of_range_coordinates(client, field, bad_value):
    ws = dict(VALID_SAVE["workstations"][0])
    ws[field] = bad_value
    resp = client.post("/workstations/save", json={"org_id": 1, "cam_id": 101, "workstations": [ws]})
    assert resp.status_code == 422


def test_save_rejects_missing_name(client):
    ws = {"x1": 0.1, "y1": 0.1, "x2": 0.5, "y2": 0.5}
    resp = client.post("/workstations/save", json={"org_id": 1, "cam_id": 101, "workstations": [ws]})
    assert resp.status_code == 422


def test_check_reports_no_workstations_initially(client):
    resp = client.get("/workstations/check", params={"org_id": 1, "cam_id": 101})
    assert resp.json() == {"has_workstations": False, "count": 0, "workstations": []}


def test_check_reflects_saved_workstations(client, saved_workstation):
    resp = client.get("/workstations/check", params={"org_id": 1, "cam_id": 101})
    body = resp.json()
    assert body["has_workstations"] is True
    assert body["count"] == 1
    assert body["workstations"][0]["name"] == "Desk-A"


def test_check_is_scoped_per_camera(client, saved_workstation):
    resp = client.get("/workstations/check", params={"org_id": 1, "cam_id": 999})
    assert resp.json()["has_workstations"] is False


def test_check_is_scoped_per_org(client, saved_workstation):
    resp = client.get("/workstations/check", params={"org_id": 2, "cam_id": 101})
    assert resp.json()["has_workstations"] is False


def test_save_upserts_existing_workstation_by_name(client, saved_workstation):
    updated = {"org_id": 1, "cam_id": 101, "workstations": [
        {"name": "Desk-A", "x1": 0.5, "y1": 0.5, "x2": 0.9, "y2": 0.9},
    ]}
    client.post("/workstations/save", json=updated)
    check = client.get("/workstations/check", params={"org_id": 1, "cam_id": 101}).json()
    assert check["count"] == 1
    assert check["workstations"][0]["x1"] == 0.5


def test_delete_single_named_workstation(client, saved_workstation):
    resp = client.request("DELETE", "/workstations/delete", json={"org_id": 1, "cam_id": 101, "name": "Desk-A"})
    assert resp.status_code == 200
    assert resp.json()["deleted_count"] == 1


def test_delete_unknown_name_returns_404(client, saved_workstation):
    resp = client.request("DELETE", "/workstations/delete", json={"org_id": 1, "cam_id": 101, "name": "Nope"})
    assert resp.status_code == 404


def test_delete_all_workstations_for_camera_when_name_omitted(client):
    payload = {"org_id": 1, "cam_id": 101, "workstations": [
        {"name": "Desk-A", "x1": 0, "y1": 0, "x2": 0.5, "y2": 0.5},
        {"name": "Desk-B", "x1": 0.5, "y1": 0.5, "x2": 1, "y2": 1},
    ]}
    client.post("/workstations/save", json=payload)
    resp = client.request("DELETE", "/workstations/delete", json={"org_id": 1, "cam_id": 101})
    assert resp.json()["deleted_count"] == 2


def test_delete_with_no_workstations_returns_404(client):
    resp = client.request("DELETE", "/workstations/delete", json={"org_id": 1, "cam_id": 101})
    assert resp.status_code == 404


def test_assign_requires_existing_employee(client, saved_workstation):
    resp = client.post("/workstations/assign", json={
        "org_id": 1, "cam_id": 101, "workstation_name": "Desk-A",
        "employee_id": "EMP-9999", "effective_from": "2026-09-08",
    })
    assert resp.status_code == 404


def test_assign_requires_existing_workstation(client, enrolled_employee):
    resp = client.post("/workstations/assign", json={
        "org_id": 1, "cam_id": 101, "workstation_name": "Desk-Z",
        "employee_id": "EMP-1001", "effective_from": "2026-09-08",
    })
    assert resp.status_code == 404


def test_assign_success(client, saved_workstation, enrolled_employee):
    resp = client.post("/workstations/assign", json={
        "org_id": 1, "cam_id": 101, "workstation_name": "Desk-A",
        "employee_id": "EMP-1001", "effective_from": "2026-09-08",
    })
    assert resp.status_code == 200
    assert resp.json()["employee_id"] == "EMP-1001"


def test_identity_status_reflects_assignment(client, saved_workstation, enrolled_employee):
    client.post("/workstations/assign", json={
        "org_id": 1, "cam_id": 101, "workstation_name": "Desk-A",
        "employee_id": "EMP-1001", "effective_from": "2026-09-08",
    })
    resp = client.get("/workstations/identity_status", params={"org_id": 1, "cam_id": 101})
    ws = resp.json()["workstations"][0]
    assert ws["assigned_employee_id"] == "EMP-1001"


def test_identity_status_empty_camera_returns_empty_list(client):
    resp = client.get("/workstations/identity_status", params={"org_id": 1, "cam_id": 555})
    assert resp.json()["workstations"] == []


@pytest.mark.parametrize("n_workstations", [1, 2, 5, 10])
def test_save_and_check_various_workstation_counts(client, n_workstations):
    workstations = [
        {"name": f"Desk-{i}", "x1": 0.0, "y1": 0.0, "x2": 0.1, "y2": 0.1}
        for i in range(n_workstations)
    ]
    client.post("/workstations/save", json={"org_id": 1, "cam_id": 101, "workstations": workstations})
    resp = client.get("/workstations/check", params={"org_id": 1, "cam_id": 101})
    assert resp.json()["count"] == n_workstations
