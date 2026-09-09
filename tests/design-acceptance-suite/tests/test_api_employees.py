import pytest


def make_enroll_payload(employee_id="EMP-2001", views=("front", "left", "right", "top")):
    return {
        "org_id": 1, "employee_id": employee_id, "name": "Jane Doe",
        "captures": [{"view": v, "depth_m": 1.0, "image_base64": "AAAA"} for v in views],
    }


def test_enroll_success(client):
    resp = client.post("/employees/enroll", json=make_enroll_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "enrolled"
    assert set(body["views_captured"]) == {"front", "left", "right", "top"}
    assert body["calibration_pending"] is True


def test_enroll_duplicate_employee_id_rejected(client):
    payload = make_enroll_payload()
    client.post("/employees/enroll", json=payload)
    resp = client.post("/employees/enroll", json=payload)
    assert resp.status_code == 409


@pytest.mark.parametrize("bad_view", ["side", "back", "diagonal", ""])
def test_enroll_rejects_invalid_view_names(client, bad_view):
    payload = make_enroll_payload(views=("front", bad_view))
    resp = client.post("/employees/enroll", json=payload)
    assert resp.status_code == 422


@pytest.mark.parametrize("views", [
    ("front",), ("front", "left"), ("front", "left", "right"),
    ("front", "left", "right", "top"),
])
def test_enroll_accepts_partial_or_complete_view_sets(client, views):
    # The API itself doesn't enforce all-4-views; that's a dataset-validation
    # concern (see test_api_dataset_calibration.py). Enrollment records
    # whatever valid views were sent.
    resp = client.post("/employees/enroll", json=make_enroll_payload(views=views))
    assert resp.status_code == 201
    assert set(resp.json()["views_captured"]) == set(views)


def test_list_employees_empty_initially(client):
    resp = client.get("/employees/list", params={"org_id": 1})
    assert resp.json() == {"employees": []}


def test_list_employees_after_enrollment(client, enrolled_employee):
    resp = client.get("/employees/list", params={"org_id": 1})
    employees = resp.json()["employees"]
    assert len(employees) == 1
    assert employees[0]["employee_id"] == "EMP-1001"
    assert employees[0]["active"] is True


def test_list_employees_scoped_by_org(client, enrolled_employee):
    resp = client.get("/employees/list", params={"org_id": 2})
    assert resp.json()["employees"] == []


@pytest.mark.parametrize("n", [1, 3, 10, 25])
def test_list_employees_various_counts(client, n):
    for i in range(n):
        client.post("/employees/enroll", json=make_enroll_payload(employee_id=f"EMP-{3000+i}"))
    resp = client.get("/employees/list", params={"org_id": 1})
    assert len(resp.json()["employees"]) == n


def test_delete_employee_success(client, enrolled_employee):
    resp = client.request("DELETE", "/employees/delete", json={"org_id": 1, "employee_id": "EMP-1001"})
    assert resp.status_code == 200
    assert resp.json()["gallery_entries_removed"] == 4


def test_delete_unknown_employee_returns_404(client):
    resp = client.request("DELETE", "/employees/delete", json={"org_id": 1, "employee_id": "EMP-9999"})
    assert resp.status_code == 404


def test_delete_removes_from_list(client, enrolled_employee):
    client.request("DELETE", "/employees/delete", json={"org_id": 1, "employee_id": "EMP-1001"})
    resp = client.get("/employees/list", params={"org_id": 1})
    assert resp.json()["employees"] == []


def test_delete_then_reenroll_same_id_succeeds(client, enrolled_employee):
    client.request("DELETE", "/employees/delete", json={"org_id": 1, "employee_id": "EMP-1001"})
    resp = client.post("/employees/enroll", json=make_enroll_payload(employee_id="EMP-1001"))
    assert resp.status_code == 201


@pytest.mark.parametrize("missing_field", ["org_id", "employee_id", "name", "captures"])
def test_enroll_missing_required_field_rejected(client, missing_field):
    payload = make_enroll_payload()
    del payload[missing_field]
    resp = client.post("/employees/enroll", json=payload)
    assert resp.status_code == 422


def test_enroll_with_empty_captures_list_is_accepted_but_captures_zero_views(client):
    resp = client.post("/employees/enroll", json=make_enroll_payload(views=()))
    assert resp.status_code == 201
    assert resp.json()["views_captured"] == []
