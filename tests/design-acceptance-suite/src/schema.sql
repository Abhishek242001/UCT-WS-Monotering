-- Test-environment schema (SQLite). Mirrors the production PostgreSQL
-- schema documented in Section 7 of the project documentation, with
-- Postgres-specific types (SERIAL, vector(512), TIMESTAMP defaults)
-- substituted for SQLite equivalents. Production deploys against real
-- PostgreSQL + pgvector; this file exists solely so unique/foreign-key
-- constraint behaviour can be verified quickly in the test suite.

PRAGMA foreign_keys = ON;

-- 7.1 Video & Workstation Configuration -------------------------------------

CREATE TABLE workstations (
    workstation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    cam_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    x1 REAL NOT NULL, y1 REAL NOT NULL, x2 REAL NOT NULL, y2 REAL NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (org_id, cam_id, name)
);

CREATE TABLE workstation_daily_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL, cam_id INTEGER NOT NULL, workstation_name TEXT NOT NULL,
    analytics_date TEXT NOT NULL,
    active_seconds REAL, vacant_seconds REAL, utilization_percent REAL,
    missing_count INTEGER, missing_duration REAL,
    first_seen_time TEXT, last_present_time TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (org_id, cam_id, workstation_name, analytics_date)
);

CREATE TABLE workstation_vacancy_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL, cam_id INTEGER NOT NULL, workstation_name TEXT NOT NULL,
    analytics_date TEXT NOT NULL,
    start_time TEXT NOT NULL, end_time TEXT, duration_seconds REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 7.2 Employee Identity & Assignment -----------------------------------------

CREATE TABLE employees (
    employee_id TEXT PRIMARY KEY,
    org_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE employee_face_gallery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id TEXT NOT NULL REFERENCES employees(employee_id) ON DELETE CASCADE,
    view TEXT NOT NULL CHECK (view IN ('front', 'left', 'right', 'top')),
    embedding BLOB,
    reference_depth_m REAL, reference_width_px REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (employee_id, view)
);

CREATE TABLE workstation_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL, cam_id INTEGER NOT NULL, workstation_name TEXT NOT NULL,
    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
    effective_from TEXT NOT NULL,
    effective_to TEXT
);

CREATE TABLE workstation_identity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL, cam_id INTEGER NOT NULL, workstation_name TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('MATCH', 'MISMATCH', 'UNKNOWN', 'VACANT')),
    assigned_employee_id TEXT, detected_employee_id TEXT,
    similarity REAL, snr REAL, distance_m REAL,
    start_time TEXT, end_time TEXT, duration_seconds REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE workstation_identity_daily_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL, cam_id INTEGER NOT NULL, workstation_name TEXT NOT NULL,
    analytics_date TEXT NOT NULL,
    matched_seconds REAL, mismatched_seconds REAL, unknown_seconds REAL, vacant_seconds REAL,
    utilization_percent REAL,
    UNIQUE (org_id, cam_id, workstation_name, analytics_date)
);

-- 7.3 Attendance, Shifts & Administration ------------------------------------

CREATE TABLE admin_login_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('LOGIN', 'LOGOUT')),
    ip_address TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE employee_attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL, employee_id TEXT NOT NULL REFERENCES employees(employee_id),
    date TEXT NOT NULL,
    sign_in_time TEXT, sign_out_time TEXT,
    last_seen_at TEXT, last_seen_cam_id INTEGER, last_seen_workstation TEXT,
    status TEXT NOT NULL CHECK (status IN ('PRESENT', 'ON_BREAK', 'SIGNED_OUT')),
    net_present_seconds INTEGER,
    day_classification TEXT CHECK (day_classification IN ('FULL', 'HALF', 'ABSENT') OR day_classification IS NULL),
    UNIQUE (org_id, employee_id, date)
);

CREATE TABLE attendance_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL, employee_id TEXT NOT NULL REFERENCES employees(employee_id),
    date TEXT NOT NULL,
    cam_id INTEGER, department_or_workstation TEXT,
    start_time TEXT, end_time TEXT, duration_seconds REAL
);

CREATE TABLE shifts (
    shift_id TEXT PRIMARY KEY,
    org_id INTEGER NOT NULL,
    shift_name TEXT NOT NULL,
    start_time TEXT NOT NULL, end_time TEXT NOT NULL
);

CREATE TABLE break_schedules (
    break_id TEXT PRIMARY KEY,
    org_id INTEGER NOT NULL, shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    break_name TEXT NOT NULL,
    start_time TEXT NOT NULL, end_time TEXT NOT NULL
);

CREATE TABLE employee_shift_assignment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    effective_from TEXT NOT NULL, effective_to TEXT
);

CREATE TABLE org_calendar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    label TEXT,
    type TEXT NOT NULL CHECK (type IN ('HOLIDAY', 'WEEKLY_OFF')),
    UNIQUE (org_id, date)
);

CREATE TABLE attendance_exceptions (
    exception_id TEXT PRIMARY KEY,
    org_id INTEGER NOT NULL, employee_id TEXT NOT NULL REFERENCES employees(employee_id),
    date TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('FIELD_WORK', 'LEAVE', 'WFH', 'CORRECTION')),
    reason TEXT,
    admin_user TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE system_health_events (
    outage_id TEXT PRIMARY KEY,
    org_id INTEGER NOT NULL, cam_id INTEGER NOT NULL,
    start_time TEXT NOT NULL, end_time TEXT,
    reason TEXT
);
