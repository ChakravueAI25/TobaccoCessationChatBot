"""Operator login: hashing, policy, rate limiting, sessions.

Spec 21 §67 asks for security testing of this boundary. The tests that matter most are the
negative ones — a password endpoint is only as good as what it refuses — so most of this file is
about failure: wrong passwords, username enumeration, throttling, expiry, revocation, and the
device key staying separate from the human one.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from api.app.models import AuthSession, LoginAttempt, User
from api.app.routes import auth as auth_route
from api.app.security import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    token_fingerprint,
    validate_password,
    validate_username,
    verify_password,
)

GOOD_PASSWORD = "Str0ng!Passw0rd"


@pytest.fixture()
def operator(client):
    """Creates an operator account directly in the test database."""
    from api.app.database import get_db
    from api.app.main import app

    session_factory = app.dependency_overrides[get_db]

    def make(username="researcher", password=GOOD_PASSWORD, disabled=False):
        generator = session_factory()
        db = next(generator)
        try:
            db.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    display_name="Test Operator",
                    role="operator",
                    disabled=disabled,
                )
            )
            db.commit()
        finally:
            generator.close()
        return username, password

    return make


def login(client, username, password):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


# ------------------------------------------------------------------ hashing


def test_a_password_is_never_stored_in_recoverable_form():
    stored = hash_password(GOOD_PASSWORD)

    assert GOOD_PASSWORD not in stored
    # Guards against the single-pass-hash mistake: a bare sha256/md5/sha1 of the password must
    # not appear anywhere in the record.
    for algorithm in (hashlib.sha256, hashlib.sha1, hashlib.md5):
        assert algorithm(GOOD_PASSWORD.encode()).hexdigest() not in stored
    assert stored.startswith("scrypt$")


def test_the_same_password_hashes_differently_every_time():
    # Distinct salts, so a stolen database cannot be attacked with one rainbow table and two
    # operators who chose the same password are not visibly identical.
    assert hash_password(GOOD_PASSWORD) != hash_password(GOOD_PASSWORD)


def test_verify_accepts_the_right_password_and_rejects_everything_else():
    stored = hash_password(GOOD_PASSWORD)

    assert verify_password(GOOD_PASSWORD, stored)
    assert not verify_password(GOOD_PASSWORD + "x", stored)
    assert not verify_password(GOOD_PASSWORD.lower(), stored)
    assert not verify_password("", stored)


def test_a_corrupted_hash_fails_the_login_instead_of_crashing():
    # A 500 here would confirm the account exists and take the endpoint down with it.
    for broken in ["", "not-a-hash", "scrypt$bad", "scrypt$x$8$1$aaaa$bbbb", "bcrypt$1$2$3$4$5"]:
        assert not verify_password(GOOD_PASSWORD, broken)


def test_needs_rehash_flags_a_foreign_or_weaker_record():
    assert not needs_rehash(hash_password(GOOD_PASSWORD))
    assert needs_rehash("scrypt$16384$8$1$c2FsdA==$a2V5")
    assert needs_rehash("bcrypt$whatever")
    assert needs_rehash("garbage")


# ------------------------------------------------------------------- policy


def test_the_policy_accepts_a_four_class_password():
    validate_password("Abhi@2004", username="abhi_22")
    validate_password(GOOD_PASSWORD, username="researcher")


@pytest.mark.parametrize(
    ("password", "missing"),
    [
        ("Ab@1", "length"),
        ("alllower@1", "upper case"),
        ("ALLUPPER@1", "lower case"),
        ("NoDigits@Here", "digit"),
        ("NoSymbols1Here", "symbol"),
    ],
)
def test_the_policy_rejects_a_password_missing_a_class(password, missing):
    with pytest.raises(PasswordPolicyError):
        validate_password(password)


def test_the_policy_rejects_a_common_password():
    with pytest.raises(PasswordPolicyError):
        validate_password("changeme")


def test_the_policy_rejects_a_password_containing_the_username():
    with pytest.raises(PasswordPolicyError):
        validate_password("Abhi_22@2004", username="abhi_22")


def test_the_denylist_is_not_the_defence_against_guessing():
    """`Password123!` satisfies every character rule and is not on the short denylist.

    Recorded deliberately so this file does not imply the denylist covers more than it does:
    the actual protection against guessing is the rate limit, not a list of bad passwords.
    """
    validate_password("Password123!")


def test_an_unbounded_password_is_refused():
    # scrypt hashes the whole input, so a megabyte password is a free way to make every login
    # attempt cost a megabyte of work.
    with pytest.raises(PasswordPolicyError):
        validate_password("Aa1@" + "x" * 500)


def test_usernames_are_constrained():
    validate_username("abhi_22")
    validate_username("a.b-c")
    for bad in ["ab", "Abhi_22", "has space", "a" * 33, "semi;colon", ""]:
        with pytest.raises(PasswordPolicyError):
            validate_username(bad)


# -------------------------------------------------------------------- login


def test_a_correct_password_returns_a_session_and_sets_a_cookie(client, operator):
    username, password = operator()

    response = login(client, username, password)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["username"] == username
    assert body["token"]
    cookie = client.cookies.get("qs_session")
    assert cookie == body["token"]


def test_the_session_cookie_is_httponly_and_samesite_strict(client, operator):
    username, password = operator()

    response = login(client, username, password)

    header = response.headers["set-cookie"].lower()
    # HttpOnly stops page scripts reading it; SameSite=Strict stops another site riding it.
    assert "httponly" in header
    assert "samesite=strict" in header


def test_a_wrong_password_and_an_unknown_user_are_indistinguishable(client, operator):
    username, _ = operator()

    wrong_password = login(client, username, "Wr0ng!Password")
    unknown_user = login(client, "nobody", "Wr0ng!Password")

    # Different responses here are a username oracle: an attacker learns which accounts exist
    # before spending any effort on passwords.
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json()["detail"] == unknown_user.json()["detail"]


def test_a_disabled_account_cannot_sign_in_and_says_nothing_about_being_disabled(client, operator):
    username, password = operator(username="retired", disabled=True)

    response = login(client, username, password)

    assert response.status_code == 401
    assert "disabled" not in response.json()["detail"].lower()


def test_the_username_is_matched_case_insensitively(client, operator):
    username, password = operator(username="abhi_22")

    assert login(client, "ABHI_22", password).status_code == 200


def test_every_attempt_is_recorded_for_the_audit_trail(client, operator):
    username, password = operator()
    login(client, username, "Wr0ng!Password")
    login(client, username, password)

    from api.app.database import get_db
    from api.app.main import app

    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        attempts = db.query(LoginAttempt).order_by(LoginAttempt.id).all()
        # Kept after success too: "three failures then a success" is the pattern worth seeing.
        assert [a.succeeded for a in attempts] == [False, True]
    finally:
        generator.close()


# --------------------------------------------------------------- rate limit


def test_repeated_failures_lock_the_account_out_with_a_retry_after(client, operator):
    username, password = operator()

    for _ in range(auth_route.MAX_FAILURES_PER_USERNAME):
        assert login(client, username, "Wr0ng!Password").status_code == 401

    blocked = login(client, username, "Wr0ng!Password")
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_the_lockout_holds_even_for_the_correct_password(client, operator):
    username, password = operator()
    for _ in range(auth_route.MAX_FAILURES_PER_USERNAME):
        login(client, username, "Wr0ng!Password")

    # Otherwise the throttle is only an inconvenience: an attacker who guesses right on attempt
    # six is let straight in.
    assert login(client, username, password).status_code == 429


def test_a_stale_failure_outside_the_window_does_not_count(client, operator, monkeypatch):
    username, password = operator()
    for _ in range(auth_route.MAX_FAILURES_PER_USERNAME):
        login(client, username, "Wr0ng!Password")
    assert login(client, username, password).status_code == 429

    # Fifteen minutes later the window has rolled past those failures. Simulated by moving the
    # clock the route reads, rather than sleeping.
    later = datetime.now(timezone.utc) + auth_route.FAILURE_WINDOW + timedelta(seconds=1)
    monkeypatch.setattr(auth_route, "_now", lambda: later)

    assert login(client, username, password).status_code == 200


def test_the_throttle_runs_before_the_password_is_hashed(client, operator, monkeypatch):
    username, _ = operator()
    for _ in range(auth_route.MAX_FAILURES_PER_USERNAME):
        login(client, username, "Wr0ng!Password")

    calls = []
    monkeypatch.setattr(
        auth_route, "verify_password", lambda *a, **k: calls.append(1) or False
    )

    assert login(client, username, "Wr0ng!Password").status_code == 429
    # scrypt costs ~half a second. Hashing before the throttle check turns the defence into the
    # denial of service it exists to prevent.
    assert calls == [], "a throttled request must not reach the hash"


# ---------------------------------------------------------------- sessions


def test_me_requires_a_session_and_returns_the_signed_in_operator(client, operator):
    username, password = operator()
    assert client.get("/api/v1/auth/me").status_code == 401

    login(client, username, password)

    body = client.get("/api/v1/auth/me").json()
    assert body["username"] == username


def test_the_device_api_key_cannot_impersonate_an_operator(client, operator, auth_headers):
    # The key identifies a phone. Everything using current_user needs to know who acted, and a
    # shared device secret cannot answer that.
    assert client.get("/api/v1/auth/me", headers=auth_headers).status_code == 401


def test_a_bearer_token_works_where_the_cookie_does(client, operator):
    username, password = operator()
    token = login(client, username, password).json()["token"]
    client.cookies.clear()

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


def test_logout_revokes_the_session_immediately(client, operator):
    username, password = operator()
    login(client, username, password)
    assert client.get("/api/v1/auth/me").status_code == 200

    assert client.post("/api/v1/auth/logout").status_code == 204

    assert client.get("/api/v1/auth/me").status_code == 401


def test_logout_without_a_session_is_still_a_success(client):
    # Idempotent, and not a way to probe whether a token is valid.
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_the_token_itself_is_not_stored(client, operator):
    username, password = operator()
    token = login(client, username, password).json()["token"]

    from api.app.database import get_db
    from api.app.main import app

    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        rows = db.query(AuthSession).all()
        assert len(rows) == 1
        # A dump of this table must not be replayable as a signed-in browser.
        assert rows[0].token_fingerprint != token
        assert rows[0].token_fingerprint == token_fingerprint(token)
    finally:
        generator.close()


def test_an_expired_session_is_rejected(client, operator):
    username, password = operator()
    token = login(client, username, password).json()["token"]

    from api.app.database import get_db
    from api.app.main import app

    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        record = db.get(AuthSession, token_fingerprint(token))
        record.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        generator.close()

    assert client.get("/api/v1/auth/me").status_code == 401


def test_disabling_an_account_invalidates_its_live_session(client, operator):
    username, password = operator()
    login(client, username, password)

    from api.app.database import get_db
    from api.app.main import app

    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        db.get(User, username).disabled = True
        db.commit()
    finally:
        generator.close()

    # Removing someone's access has to take effect now, not in twelve hours.
    assert client.get("/api/v1/auth/me").status_code == 401


# ---------------------------------------------------------- change password


def test_changing_a_password_requires_the_current_one(client, operator):
    username, password = operator()
    login(client, username, password)

    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "Wr0ng!Password", "new_password": "An0ther!Pass"},
    )

    assert response.status_code == 401


def test_a_new_password_must_satisfy_the_policy(client, operator):
    username, password = operator()
    login(client, username, password)

    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": password, "new_password": "weak"},
    )

    assert response.status_code == 422
    assert "at least" in response.json()["detail"]


def test_changing_a_password_signs_every_session_out(client, operator):
    username, password = operator()
    login(client, username, password)

    assert client.post(
        "/api/v1/auth/change-password",
        json={"current_password": password, "new_password": "An0ther!Pass"},
    ).status_code == 204

    # A password change is what someone does when they think a credential leaked; leaving old
    # sessions alive makes it pointless.
    assert client.get("/api/v1/auth/me").status_code == 401
    assert login(client, username, "An0ther!Pass").status_code == 200


# ------------------------------------------------- research routes accept both


def test_research_routes_accept_a_logged_in_operator(client, operator):
    username, password = operator()
    login(client, username, password)

    # No X-API-Key anywhere: the researcher's own session is enough.
    assert client.get("/api/v1/stats").status_code == 200
    assert client.get("/api/v1/participants").status_code == 200


def test_research_routes_still_accept_the_device_api_key(client, auth_headers):
    # The Android app and any existing script must keep working: spec 04 freeze item 2 forbids
    # putting an interactive login in front of a phone.
    assert client.get("/api/v1/stats", headers=auth_headers).status_code == 200


def test_research_routes_reject_an_unauthenticated_caller(client):
    assert client.get("/api/v1/stats").status_code == 401
    assert client.get("/api/v1/events").status_code == 401
    assert client.post("/api/v1/exports", json={"format": "csv"}).status_code == 401


def test_a_wrong_api_key_is_rejected(client):
    assert client.get("/api/v1/stats", headers={"X-API-Key": "not-the-key"}).status_code == 401
