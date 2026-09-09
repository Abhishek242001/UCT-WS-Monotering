"""
Security tests, split deliberately into two groups:

1. Tests that run against the current mock implementation and pass today
   (auth lockout, session token handling, basic injection resistance).
2. Tests marked `xfail`/`skip` that encode security requirements from the
   documentation (Section 11-style controls: bot mitigation, encryption,
   RBAC, liveness/anti-spoofing) which the mock intentionally does not yet
   implement. These exist as the acceptance checklist for the real
   implementation -- per the project's stated goal of having a running
   300+ test suite to verify against as development proceeds -- and should
   be un-skipped one by one as each control is actually built.
"""
import sqlite3
import string

import pytest


# ---------------------------------------------------------------------------
# Group 1 -- pass today, against the mock implementation
# ---------------------------------------------------------------------------

def test_session_token_is_sufficiently_long(admin_token):
    assert len(admin_token) >= 32


def test_session_token_is_url_safe(admin_token):
    allowed = set(string.ascii_letters + string.digits + "-_")
    assert set(admin_token).issubset(allowed)


def test_repeated_logins_never_reuse_tokens(client):
    creds = {"username": "hr_admin1", "password": "correct-horse-battery-staple"}
    tokens = {client.post("/admin/login", json=creds).json()["session_token"] for _ in range(10)}
    assert len(tokens) == 10


def test_account_lockout_blocks_further_attempts(client):
    for _ in range(5):
        client.post("/admin/login", json={"username": "hr_admin1", "password": "wrong"})
    resp = client.post("/admin/login", json={"username": "hr_admin1", "password": "correct-horse-battery-staple"})
    assert resp.status_code == 429


def test_login_error_message_is_generic_not_field_specific(client):
    resp = client.post("/admin/login", json={"username": "hr_admin1", "password": "wrong"})
    detail = resp.json()["detail"].lower()
    assert "password" not in detail.split()[0:2]  # doesn't lead with "password is wrong"
    assert "invalid" in detail


def test_logout_actually_invalidates_the_session(client, admin_token):
    client.post("/admin/logout", json={"session_token": admin_token})
    resp = client.get("/admin/session", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.json()["valid"] is False


def test_expired_looking_malformed_bearer_header_does_not_crash(client):
    resp = client.get("/admin/session", headers={"Authorization": "NotBearer garbage"})
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


def test_missing_authorization_header_handled_gracefully(client):
    resp = client.get("/admin/session")
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


# --- injection resistance (parameterized queries) ---------------------------

SQLI_PAYLOADS = [
    "'; DROP TABLE employees; --",
    "' OR '1'='1",
    "EMP-1' UNION SELECT * FROM admin_login_audit --",
    "Robert'); DROP TABLE workstations;--",
]


@pytest.mark.parametrize("payload", SQLI_PAYLOADS)
def test_sql_injection_payload_stored_literally_not_executed(db, payload):
    db.execute("INSERT INTO employees (employee_id, org_id, name) VALUES (?, 1, ?)", (payload, "Test"))
    # The table must still exist and contain exactly the literal string --
    # proof the parameterized query was not vulnerable to the payload.
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "employees" in tables
    row = db.execute("SELECT employee_id FROM employees WHERE employee_id=?", (payload,)).fetchone()
    assert row is not None and row[0] == payload


@pytest.mark.parametrize("payload", SQLI_PAYLOADS)
def test_sql_injection_payload_via_api_does_not_break_enrollment(client, payload):
    resp = client.post("/employees/enroll", json={
        "org_id": 1, "employee_id": payload, "name": "Test",
        "captures": [{"view": "front", "depth_m": 1.0, "image_base64": "AAAA"}],
    })
    assert resp.status_code == 201
    listing = client.get("/employees/list", params={"org_id": 1}).json()["employees"]
    assert any(e["employee_id"] == payload for e in listing)


@pytest.mark.parametrize("payload", ["../../etc/passwd", "..\\..\\windows\\system32", "/etc/shadow"])
def test_path_traversal_style_names_stored_safely_not_executed(client, payload):
    resp = client.post("/employees/enroll", json={
        "org_id": 1, "employee_id": payload, "name": "Test",
        "captures": [{"view": "front", "depth_m": 1.0, "image_base64": "AAAA"}],
    })
    # Must not crash the service, and must store the identifier verbatim
    # rather than silently rewriting or partially interpreting it.
    assert resp.status_code == 201
    assert resp.json()["employee_id"] == payload


def test_cors_does_not_reflect_arbitrary_origin_by_default(client):
    resp = client.get("/", headers={"Origin": "https://evil.example.com"})
    assert resp.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_dataset_upload_rejects_oversized_payload():
    # Covered functionally in test_api_dataset_calibration.py; re-asserted
    # here as an explicit security control (zip-bomb / DoS mitigation).
    from src.mock_app import MAX_UPLOAD_BYTES
    assert MAX_UPLOAD_BYTES > 0
    assert MAX_UPLOAD_BYTES <= 500 * 1024 * 1024  # sane upper bound, not unlimited


# ---------------------------------------------------------------------------
# Group 2 -- acceptance criteria for the real implementation (not yet built)
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="Requires per-IP rate limiting middleware -- not yet implemented in the mock.")
def test_per_ip_rate_limiting_on_login_endpoint():
    ...


@pytest.mark.skip(reason="Requires CAPTCHA/challenge integration after N suspicious attempts -- pending implementation.")
def test_captcha_challenge_triggered_after_suspicious_pattern():
    ...


@pytest.mark.skip(reason="Requires TLS-only enforcement at the deployment layer -- not testable against the in-process mock.")
def test_http_requests_are_redirected_or_rejected_in_production():
    ...


@pytest.mark.skip(reason="Requires SameSite=None; Secure cookie config wired to environment -- pending real session-cookie implementation.")
def test_session_cookie_has_secure_and_samesite_none_in_cloud_environment():
    ...


@pytest.mark.skip(reason="Requires SameSite=Lax / non-secure fallback for local HTTP development -- pending implementation.")
def test_session_cookie_relaxes_secure_flag_on_localhost():
    ...


@pytest.mark.skip(reason="Requires RBAC roles beyond the single HR_ADMIN role currently modeled.")
def test_viewer_role_cannot_call_admin_only_endpoints():
    ...


@pytest.mark.skip(reason="Requires encryption-at-rest for face embeddings (pgvector + column/volume encryption) -- pending production DB setup.")
def test_face_embeddings_are_encrypted_at_rest():
    ...


@pytest.mark.skip(reason="Requires liveness/anti-spoofing detection in the real face-recognition pipeline -- not modeled in the mock.")
def test_photo_replay_attack_is_rejected_by_liveness_check():
    ...


@pytest.mark.skip(reason="Requires video-replay liveness detection -- not modeled in the mock.")
def test_video_replay_attack_is_rejected_by_liveness_check():
    ...


@pytest.mark.skip(reason="Requires a dependency vulnerability scan step (e.g. pip-audit / npm audit) wired into CI.")
def test_no_known_critical_vulnerabilities_in_dependencies():
    ...


@pytest.mark.skip(reason="Requires secrets-manager integration -- pending production configuration; verify no secrets are hardcoded in source.")
def test_no_hardcoded_secrets_in_source_tree():
    ...


@pytest.mark.skip(reason="Requires an audit-log completeness check across all privileged endpoints once RBAC roles beyond HR_ADMIN exist.")
def test_every_privileged_action_is_audit_logged():
    ...


@pytest.mark.skip(reason="Requires request body size limits at the ASGI/reverse-proxy layer -- not configured in the mock.")
def test_oversized_json_body_is_rejected():
    ...


@pytest.mark.skip(reason="Requires max-length validation on free-text fields (e.g. attendance_exceptions.reason) -- pending schema hardening.")
def test_free_text_fields_enforce_a_maximum_length():
    ...


@pytest.mark.skip(reason="Requires a Web Application Firewall / managed proxy in front of the deployed API -- infrastructure-level control.")
def test_waf_blocks_known_bad_request_patterns():
    ...
