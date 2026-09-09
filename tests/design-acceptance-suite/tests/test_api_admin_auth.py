import pytest


VALID_CREDS = {"username": "hr_admin1", "password": "correct-horse-battery-staple"}


def test_login_success(client):
    resp = client.post("/admin/login", json=VALID_CREDS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["role"] == "HR_ADMIN"
    assert len(body["session_token"]) > 20


def test_login_wrong_password_rejected(client):
    resp = client.post("/admin/login", json={"username": "hr_admin1", "password": "wrong"})
    assert resp.status_code == 401


def test_login_unknown_username_rejected(client):
    resp = client.post("/admin/login", json={"username": "nobody", "password": "whatever"})
    assert resp.status_code == 401


def test_login_unknown_user_error_message_does_not_reveal_which_field_was_wrong(client):
    # Prevents username enumeration -- same error for "user doesn't exist"
    # and "wrong password for a real user".
    resp_bad_user = client.post("/admin/login", json={"username": "nobody", "password": "x"})
    resp_bad_pass = client.post("/admin/login", json={"username": "hr_admin1", "password": "x"})
    assert resp_bad_user.json()["detail"] == resp_bad_pass.json()["detail"]


def test_session_valid_after_login(client, admin_token):
    resp = client.get("/admin/session", headers={"Authorization": f"Bearer {admin_token}"})
    body = resp.json()
    assert body["valid"] is True
    assert body["username"] == "hr_admin1"


def test_session_invalid_without_token(client):
    resp = client.get("/admin/session")
    assert resp.json()["valid"] is False


def test_session_invalid_with_garbage_token(client):
    resp = client.get("/admin/session", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.json()["valid"] is False


def test_logout_invalidates_session(client, admin_token):
    client.post("/admin/logout", json={"session_token": admin_token})
    resp = client.get("/admin/session", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.json()["valid"] is False


def test_logout_unknown_token_does_not_error(client):
    resp = client.post("/admin/logout", json={"session_token": "never-issued"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "logged_out"


def test_repeated_logins_issue_different_tokens(client):
    t1 = client.post("/admin/login", json=VALID_CREDS).json()["session_token"]
    t2 = client.post("/admin/login", json=VALID_CREDS).json()["session_token"]
    assert t1 != t2


def test_account_lockout_after_repeated_failed_logins(client):
    for _ in range(5):
        resp = client.post("/admin/login", json={"username": "hr_admin1", "password": "wrong"})
        assert resp.status_code == 401
    # 6th attempt, even with the CORRECT password, should now be locked out.
    resp = client.post("/admin/login", json=VALID_CREDS)
    assert resp.status_code == 429


def test_lockout_is_per_username(client):
    for _ in range(5):
        client.post("/admin/login", json={"username": "hr_admin1", "password": "wrong"})
    # A different (nonexistent) username is unaffected by hr_admin1's lockout.
    resp = client.post("/admin/login", json={"username": "someone_else", "password": "wrong"})
    assert resp.status_code == 401  # not 429 -- not locked, just wrong creds


def test_successful_login_resets_failed_attempt_counter(client):
    for _ in range(3):
        client.post("/admin/login", json={"username": "hr_admin1", "password": "wrong"})
    good = client.post("/admin/login", json=VALID_CREDS)
    assert good.status_code == 200
    # Should now be able to fail up to 4 more times before locking again.
    for _ in range(4):
        resp = client.post("/admin/login", json={"username": "hr_admin1", "password": "wrong"})
        assert resp.status_code == 401


@pytest.mark.parametrize("missing_field", ["username", "password"])
def test_login_missing_field_rejected(client, missing_field):
    payload = dict(VALID_CREDS)
    del payload[missing_field]
    resp = client.post("/admin/login", json=payload)
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_password", ["", " ", "wrong123", "Correct-Horse-Battery-Staple"])
def test_login_various_wrong_passwords(client, bad_password):
    resp = client.post("/admin/login", json={"username": "hr_admin1", "password": bad_password})
    assert resp.status_code == 401
