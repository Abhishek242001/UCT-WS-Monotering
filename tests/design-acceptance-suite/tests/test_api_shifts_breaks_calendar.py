import pytest


VALID_SHIFT = {"org_id": 1, "shift_name": "Morning Shift", "start_time": "09:00", "end_time": "18:00"}


def test_create_shift_success(client):
    resp = client.post("/shifts", json=VALID_SHIFT)
    assert resp.status_code == 201
    assert resp.json()["shift_id"].startswith("SHIFT-")


def test_list_shifts_empty_initially(client):
    resp = client.get("/shifts", params={"org_id": 1})
    assert resp.json() == {"shifts": []}


def test_list_shifts_reflects_created_shifts(client):
    client.post("/shifts", json=VALID_SHIFT)
    client.post("/shifts", json=dict(VALID_SHIFT, shift_name="Evening Shift", start_time="18:00", end_time="03:00"))
    resp = client.get("/shifts", params={"org_id": 1})
    assert len(resp.json()["shifts"]) == 2


def test_list_shifts_scoped_by_org(client):
    client.post("/shifts", json=VALID_SHIFT)
    resp = client.get("/shifts", params={"org_id": 2})
    assert resp.json()["shifts"] == []


@pytest.mark.parametrize("missing_field", ["org_id", "shift_name", "start_time", "end_time"])
def test_create_shift_missing_field_rejected(client, missing_field):
    payload = dict(VALID_SHIFT)
    del payload[missing_field]
    resp = client.post("/shifts", json=payload)
    assert resp.status_code == 422


def _make_shift(client):
    return client.post("/shifts", json=VALID_SHIFT).json()["shift_id"]


def test_create_break_requires_existing_shift(client):
    resp = client.post("/breaks", json={
        "org_id": 1, "shift_id": "SHIFT-nope", "break_name": "Lunch",
        "start_time": "13:00", "end_time": "13:45",
    })
    assert resp.status_code == 404


def test_create_break_success(client):
    shift_id = _make_shift(client)
    resp = client.post("/breaks", json={
        "org_id": 1, "shift_id": shift_id, "break_name": "Lunch",
        "start_time": "13:00", "end_time": "13:45",
    })
    assert resp.status_code == 201
    assert resp.json()["break_id"].startswith("BRK-")


def test_list_breaks_for_shift(client):
    shift_id = _make_shift(client)
    client.post("/breaks", json={"org_id": 1, "shift_id": shift_id, "break_name": "Lunch",
                                  "start_time": "13:00", "end_time": "13:45"})
    client.post("/breaks", json={"org_id": 1, "shift_id": shift_id, "break_name": "Tea",
                                  "start_time": "16:00", "end_time": "16:15"})
    resp = client.get("/breaks", params={"org_id": 1, "shift_id": shift_id})
    assert len(resp.json()["breaks"]) == 2


def test_breaks_isolated_per_shift(client):
    shift1 = _make_shift(client)
    shift2 = client.post("/shifts", json=dict(VALID_SHIFT, shift_name="Night")).json()["shift_id"]
    client.post("/breaks", json={"org_id": 1, "shift_id": shift1, "break_name": "Lunch",
                                  "start_time": "13:00", "end_time": "13:45"})
    resp = client.get("/breaks", params={"org_id": 1, "shift_id": shift2})
    assert resp.json()["breaks"] == []


def test_shift_assignment_requires_existing_employee(client):
    shift_id = _make_shift(client)
    resp = client.post("/employees/EMP-9999/shift-assignment",
                        json={"org_id": 1, "shift_id": shift_id, "effective_from": "2026-09-08"})
    assert resp.status_code == 404


def test_shift_assignment_requires_existing_shift(client, enrolled_employee):
    resp = client.post("/employees/EMP-1001/shift-assignment",
                        json={"org_id": 1, "shift_id": "SHIFT-nope", "effective_from": "2026-09-08"})
    assert resp.status_code == 404


def test_shift_assignment_success(client, enrolled_employee):
    shift_id = _make_shift(client)
    resp = client.post("/employees/EMP-1001/shift-assignment",
                        json={"org_id": 1, "shift_id": shift_id, "effective_from": "2026-09-08"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "assigned"


VALID_CALENDAR = {"org_id": 1, "date": "2026-10-02", "label": "National Holiday", "type": "HOLIDAY"}


def test_add_calendar_entry_success(client):
    resp = client.post("/org-calendar", json=VALID_CALENDAR)
    assert resp.status_code == 201


@pytest.mark.parametrize("cal_type", ["HOLIDAY", "WEEKLY_OFF"])
def test_calendar_accepts_valid_types(client, cal_type):
    resp = client.post("/org-calendar", json=dict(VALID_CALENDAR, type=cal_type))
    assert resp.status_code == 201


@pytest.mark.parametrize("bad_type", ["FESTIVAL", "OPTIONAL", ""])
def test_calendar_rejects_invalid_types(client, bad_type):
    resp = client.post("/org-calendar", json=dict(VALID_CALENDAR, type=bad_type))
    assert resp.status_code == 422


def test_get_calendar_filters_by_year(client):
    client.post("/org-calendar", json=VALID_CALENDAR)  # 2026
    client.post("/org-calendar", json=dict(VALID_CALENDAR, date="2027-01-01"))
    resp = client.get("/org-calendar", params={"org_id": 1, "year": 2026})
    assert len(resp.json()["calendar"]) == 1


def test_get_calendar_scoped_by_org(client):
    client.post("/org-calendar", json=VALID_CALENDAR)
    resp = client.get("/org-calendar", params={"org_id": 2, "year": 2026})
    assert resp.json()["calendar"] == []


@pytest.mark.parametrize("n_entries", [0, 1, 5, 12])
def test_calendar_various_entry_counts(client, n_entries):
    for i in range(n_entries):
        client.post("/org-calendar", json=dict(VALID_CALENDAR, date=f"2026-{(i % 12) + 1:02d}-01", label=f"Day {i}"))
    resp = client.get("/org-calendar", params={"org_id": 1, "year": 2026})
    assert len(resp.json()["calendar"]) == n_entries
