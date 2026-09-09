import sqlite3
import pytest


# --- workstations: UNIQUE(org_id, cam_id, name) -----------------------------

def test_workstation_unique_constraint_enforced(db):
    db.execute("INSERT INTO workstations (org_id, cam_id, name, x1, y1, x2, y2) VALUES (1,101,'Desk-A',0,0,1,1)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO workstations (org_id, cam_id, name, x1, y1, x2, y2) VALUES (1,101,'Desk-A',0,0,1,1)")


def test_workstation_same_name_different_camera_allowed(db):
    db.execute("INSERT INTO workstations (org_id, cam_id, name, x1, y1, x2, y2) VALUES (1,101,'Desk-A',0,0,1,1)")
    db.execute("INSERT INTO workstations (org_id, cam_id, name, x1, y1, x2, y2) VALUES (1,102,'Desk-A',0,0,1,1)")
    count = db.execute("SELECT COUNT(*) FROM workstations").fetchone()[0]
    assert count == 2


def test_workstation_same_name_different_org_allowed(db):
    db.execute("INSERT INTO workstations (org_id, cam_id, name, x1, y1, x2, y2) VALUES (1,101,'Desk-A',0,0,1,1)")
    db.execute("INSERT INTO workstations (org_id, cam_id, name, x1, y1, x2, y2) VALUES (2,101,'Desk-A',0,0,1,1)")
    count = db.execute("SELECT COUNT(*) FROM workstations").fetchone()[0]
    assert count == 2


# --- workstation_daily_analytics: UNIQUE(org_id, cam_id, workstation_name, analytics_date) --

def test_daily_analytics_unique_constraint(db):
    db.execute("INSERT INTO workstation_daily_analytics (org_id, cam_id, workstation_name, analytics_date) VALUES (1,101,'Desk-A','2026-09-06')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO workstation_daily_analytics (org_id, cam_id, workstation_name, analytics_date) VALUES (1,101,'Desk-A','2026-09-06')")


def test_daily_analytics_different_date_allowed(db):
    db.execute("INSERT INTO workstation_daily_analytics (org_id, cam_id, workstation_name, analytics_date) VALUES (1,101,'Desk-A','2026-09-06')")
    db.execute("INSERT INTO workstation_daily_analytics (org_id, cam_id, workstation_name, analytics_date) VALUES (1,101,'Desk-A','2026-09-07')")
    count = db.execute("SELECT COUNT(*) FROM workstation_daily_analytics").fetchone()[0]
    assert count == 2


# --- employees: primary key, soft delete -----------------------------------

def test_employee_id_is_primary_key(db):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'B')")


def test_employee_active_defaults_true(db):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    active = db.execute("SELECT active FROM employees WHERE employee_id='EMP-1'").fetchone()[0]
    assert active == 1


def test_employee_soft_delete_preserves_row(db):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    db.execute("UPDATE employees SET active=0 WHERE employee_id='EMP-1'")
    row = db.execute("SELECT active FROM employees WHERE employee_id='EMP-1'").fetchone()
    assert row is not None
    assert row[0] == 0


# --- employee_face_gallery: FK + UNIQUE(employee_id, view) + view CHECK -----

def test_gallery_requires_existing_employee(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO employee_face_gallery (employee_id, view) VALUES ('EMP-NOPE', 'front')")


@pytest.mark.parametrize("view", ["front", "left", "right", "top"])
def test_gallery_accepts_valid_views(db, view):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    db.execute("INSERT INTO employee_face_gallery (employee_id, view) VALUES ('EMP-1', ?)", (view,))
    count = db.execute("SELECT COUNT(*) FROM employee_face_gallery").fetchone()[0]
    assert count == 1


@pytest.mark.parametrize("bad_view", ["back", "diagonal", "profile", ""])
def test_gallery_rejects_invalid_view(db, bad_view):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO employee_face_gallery (employee_id, view) VALUES ('EMP-1', ?)", (bad_view,))


def test_gallery_unique_view_per_employee(db):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    db.execute("INSERT INTO employee_face_gallery (employee_id, view) VALUES ('EMP-1', 'front')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO employee_face_gallery (employee_id, view) VALUES ('EMP-1', 'front')")


def test_gallery_cascade_delete_on_employee_removal(db):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    db.execute("INSERT INTO employee_face_gallery (employee_id, view) VALUES ('EMP-1', 'front')")
    db.execute("DELETE FROM employees WHERE employee_id='EMP-1'")
    count = db.execute("SELECT COUNT(*) FROM employee_face_gallery").fetchone()[0]
    assert count == 0


# --- workstation_assignments: history, not overwritten ----------------------

def test_assignment_requires_existing_employee(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("""INSERT INTO workstation_assignments
            (org_id, cam_id, workstation_name, employee_id, effective_from)
            VALUES (1, 101, 'Desk-A', 'EMP-NOPE', '2026-09-08')""")


def test_assignment_history_keeps_multiple_rows(db):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-2', 1, 'B')")
    db.execute("""INSERT INTO workstation_assignments (org_id, cam_id, workstation_name, employee_id, effective_from, effective_to)
                  VALUES (1, 101, 'Desk-A', 'EMP-1', '2026-01-01', '2026-06-01')""")
    db.execute("""INSERT INTO workstation_assignments (org_id, cam_id, workstation_name, employee_id, effective_from)
                  VALUES (1, 101, 'Desk-A', 'EMP-2', '2026-06-01')""")
    count = db.execute("SELECT COUNT(*) FROM workstation_assignments WHERE workstation_name='Desk-A'").fetchone()[0]
    assert count == 2


# --- workstation_identity_events: event_type CHECK constraint --------------

@pytest.mark.parametrize("event_type", ["MATCH", "MISMATCH", "UNKNOWN", "VACANT"])
def test_identity_event_accepts_valid_types(db, event_type):
    db.execute("INSERT INTO workstation_identity_events (org_id, cam_id, workstation_name, event_type) VALUES (1,101,'Desk-A',?)", (event_type,))
    count = db.execute("SELECT COUNT(*) FROM workstation_identity_events").fetchone()[0]
    assert count == 1


@pytest.mark.parametrize("bad_type", ["PRESENT", "ABSENT", "match", ""])
def test_identity_event_rejects_invalid_types(db, bad_type):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO workstation_identity_events (org_id, cam_id, workstation_name, event_type) VALUES (1,101,'Desk-A',?)", (bad_type,))


# --- employee_attendance: UNIQUE(org_id, employee_id, date) + status CHECK --

def test_attendance_unique_per_employee_per_day(db):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    db.execute("INSERT INTO employee_attendance (org_id, employee_id, date, status) VALUES (1,'EMP-1','2026-09-06','PRESENT')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO employee_attendance (org_id, employee_id, date, status) VALUES (1,'EMP-1','2026-09-06','SIGNED_OUT')")


def test_attendance_different_day_allowed(db):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    db.execute("INSERT INTO employee_attendance (org_id, employee_id, date, status) VALUES (1,'EMP-1','2026-09-06','PRESENT')")
    db.execute("INSERT INTO employee_attendance (org_id, employee_id, date, status) VALUES (1,'EMP-1','2026-09-07','PRESENT')")
    count = db.execute("SELECT COUNT(*) FROM employee_attendance").fetchone()[0]
    assert count == 2


@pytest.mark.parametrize("status", ["PRESENT", "ON_BREAK", "SIGNED_OUT"])
def test_attendance_status_accepts_valid_values(db, status):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    db.execute("INSERT INTO employee_attendance (org_id, employee_id, date, status) VALUES (1,'EMP-1','2026-09-06',?)", (status,))
    count = db.execute("SELECT COUNT(*) FROM employee_attendance").fetchone()[0]
    assert count == 1


@pytest.mark.parametrize("bad_status", ["ACTIVE", "AWAY", "logged_in", ""])
def test_attendance_status_rejects_invalid_values(db, bad_status):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO employee_attendance (org_id, employee_id, date, status) VALUES (1,'EMP-1','2026-09-06',?)", (bad_status,))


# --- shifts / break_schedules: FK relationship ------------------------------

def test_break_requires_existing_shift(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO break_schedules (break_id, org_id, shift_id, break_name, start_time, end_time) VALUES ('BRK-1',1,'SHIFT-NOPE','Lunch','13:00','13:45')")


def test_break_succeeds_with_existing_shift(db):
    db.execute("INSERT INTO shifts (shift_id, org_id, shift_name, start_time, end_time) VALUES ('SHIFT-1',1,'Morning','09:00','18:00')")
    db.execute("INSERT INTO break_schedules (break_id, org_id, shift_id, break_name, start_time, end_time) VALUES ('BRK-1',1,'SHIFT-1','Lunch','13:00','13:45')")
    count = db.execute("SELECT COUNT(*) FROM break_schedules").fetchone()[0]
    assert count == 1


# --- org_calendar: UNIQUE(org_id, date) + type CHECK ------------------------

def test_calendar_unique_per_org_per_date(db):
    db.execute("INSERT INTO org_calendar (org_id, date, label, type) VALUES (1,'2026-10-02','Holiday','HOLIDAY')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO org_calendar (org_id, date, label, type) VALUES (1,'2026-10-02','Another','HOLIDAY')")


def test_calendar_same_date_different_org_allowed(db):
    db.execute("INSERT INTO org_calendar (org_id, date, label, type) VALUES (1,'2026-10-02','Holiday','HOLIDAY')")
    db.execute("INSERT INTO org_calendar (org_id, date, label, type) VALUES (2,'2026-10-02','Holiday','HOLIDAY')")
    count = db.execute("SELECT COUNT(*) FROM org_calendar").fetchone()[0]
    assert count == 2


@pytest.mark.parametrize("bad_type", ["FESTIVAL", "OPTIONAL_LEAVE", ""])
def test_calendar_rejects_invalid_type(db, bad_type):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO org_calendar (org_id, date, label, type) VALUES (1,'2026-10-02','X',?)", (bad_type,))


# --- attendance_exceptions: type CHECK + admin_user NOT NULL ----------------

@pytest.mark.parametrize("exc_type", ["FIELD_WORK", "LEAVE", "WFH", "CORRECTION"])
def test_exception_accepts_valid_types(db, exc_type):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    db.execute("INSERT INTO attendance_exceptions (exception_id, org_id, employee_id, date, type, admin_user) VALUES ('EXC-1',1,'EMP-1','2026-09-06',?, 'hr_admin1')", (exc_type,))
    count = db.execute("SELECT COUNT(*) FROM attendance_exceptions").fetchone()[0]
    assert count == 1


def test_exception_requires_admin_user(db):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES ('EMP-1', 1, 'A')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO attendance_exceptions (exception_id, org_id, employee_id, date, type, admin_user) VALUES ('EXC-1',1,'EMP-1','2026-09-06','LEAVE', NULL)")


# --- foreign keys are actually enforced (sanity check on PRAGMA) ------------

def test_foreign_keys_pragma_is_on(db):
    result = db.execute("PRAGMA foreign_keys").fetchone()[0]
    assert result == 1


def test_all_seventeen_tables_exist(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "workstations", "workstation_daily_analytics", "workstation_vacancy_logs",
        "employees", "employee_face_gallery", "workstation_assignments",
        "workstation_identity_events", "workstation_identity_daily_analytics",
        "admin_login_audit", "employee_attendance", "attendance_segments",
        "shifts", "break_schedules", "employee_shift_assignment",
        "org_calendar", "attendance_exceptions", "system_health_events",
    }
    assert expected.issubset(tables)
    assert len(expected) == 17
