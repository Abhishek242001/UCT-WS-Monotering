"""
One test per row of the "Scenario handling" table (Section 6.2 of the
project documentation), so the acceptance criteria stay explicitly traceable
to the documented scenario list rather than only living implicitly inside
test_logic_attendance_state_machine.py.
"""
from datetime import time as dtime, datetime, timezone

import pytest

from src.logic import (
    next_attendance_status, AttendanceStatus, BreakWindow, classify_day, DayClassification,
)

LUNCH = BreakWindow(start=dtime(13, 0), end=dtime(13, 45))


def test_scenario_1_department_move_keeps_presence_timer_running():
    status = next_attendance_status(
        AttendanceStatus.PRESENT, detected_now=True, current_time=dtime(11, 0),
        break_windows=[], gap_seconds=0, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.PRESENT


def test_scenario_2_scheduled_break_pauses_regardless_of_detection():
    status = next_attendance_status(
        AttendanceStatus.PRESENT, detected_now=False, current_time=dtime(13, 20),
        break_windows=[LUNCH], gap_seconds=1200, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.ON_BREAK


def test_scenario_3_short_blind_spot_gap_is_absorbed_by_grace_period():
    status = next_attendance_status(
        AttendanceStatus.PRESENT, detected_now=False, current_time=dtime(10, 0),
        break_windows=[], gap_seconds=500, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.PRESENT


def test_scenario_4_off_site_work_is_not_auto_inferred_requires_exception(client):
    # The state machine alone has no way to distinguish "off-site" from
    # "genuinely gone" -- that distinction requires the exception API.
    resp = client.post("/attendance/exception", json={
        "org_id": 1, "employee_id": "EMP-1001", "date": "2026-09-06",
        "type": "FIELD_WORK", "reason": "Client site visit", "admin_user": "hr_admin1",
    })
    assert resp.status_code == 201


def test_scenario_5_outage_suspends_automatic_signout(client):
    resp = client.post("/system/outage", json={
        "org_id": 1, "cam_id": 101, "start_time": "2026-09-06T02:00:00Z", "reason": "Network maintenance",
    })
    assert resp.status_code == 201
    # A recorded outage exists; production logic must check this table
    # before auto-signing anyone out on the affected camera (documented
    # requirement -- enforcement itself lives in the real scheduler, not
    # in this mock).


def test_scenario_6_late_early_overtime_derived_by_comparison_to_shift(client):
    shift_id = client.post("/shifts", json={
        "org_id": 1, "shift_name": "Morning", "start_time": "09:00", "end_time": "18:00",
    }).json()["shift_id"]
    assert shift_id.startswith("SHIFT-")
    # Comparison logic itself is a simple time comparison, covered by the
    # classify_day / net-present-seconds unit tests; this test asserts the
    # shift record needed for that comparison can be created and retrieved.
    shifts = client.get("/shifts", params={"org_id": 1}).json()["shifts"]
    assert any(s["shift_id"] == shift_id for s in shifts)


@pytest.mark.parametrize("seconds,expected", [
    (8 * 3600, DayClassification.FULL),
    (5 * 3600, DayClassification.HALF),
    (30 * 60, DayClassification.ABSENT),
])
def test_scenario_7_half_full_absent_classification(seconds, expected):
    assert classify_day(seconds, full_day_threshold_seconds=7 * 3600, half_day_threshold_seconds=4 * 3600) == expected


def test_scenario_8_multiple_departures_keep_one_summary_record(client, enrolled_employee):
    for ts in ["2026-09-06T07:00:00", "2026-09-06T11:00:00", "2026-09-06T14:00:00"]:
        client.post("/attendance/_test_record_detection", json={"org_id": 1, "employee_id": "EMP-1001", "timestamp": ts})
    resp = client.get("/attendance/report", params={"org_id": 1, "employee_id": "EMP-1001"})
    days = resp.json()["days"]
    assert len(days) == 1  # one summary row for the day, not one per detection


def test_scenario_9_weekends_holidays_tracked_in_calendar(client):
    resp = client.post("/org-calendar", json={"org_id": 1, "date": "2026-10-02", "label": "Holiday", "type": "HOLIDAY"})
    assert resp.status_code == 201
    calendar = client.get("/org-calendar", params={"org_id": 1, "year": 2026}).json()["calendar"]
    assert any(c["date"] == "2026-10-02" for c in calendar)


def test_scenario_10_false_negative_correction_via_exception(client):
    resp = client.post("/attendance/exception", json={
        "org_id": 1, "employee_id": "EMP-1001", "date": "2026-09-06",
        "type": "CORRECTION", "reason": "Employee present but not detected due to poor lighting",
        "admin_user": "hr_admin1",
    })
    assert resp.status_code == 201
    assert resp.json()["exception_id"].startswith("EXC-")


def test_scenario_11_tailgating_multiple_people_tracked_independently(client):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for emp_id in ("EMP-A", "EMP-B"):
        client.post("/employees/enroll", json={
            "org_id": 1, "employee_id": emp_id, "name": emp_id,
            "captures": [{"view": "front", "depth_m": 1.0, "image_base64": "AAAA"}],
        })
        client.post("/attendance/_test_record_detection", json={
            "org_id": 1, "employee_id": emp_id, "timestamp": f"{today}T09:00:00",
        })
    for emp_id in ("EMP-A", "EMP-B"):
        resp = client.get("/attendance/today", params={"org_id": 1, "employee_id": emp_id})
        assert resp.status_code == 200
        assert resp.json()["status"] == "PRESENT"
