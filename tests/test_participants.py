"""Participant registration and sign-in.

The property that matters most here is not a security one — it is spec 04 freeze item 2. A
participant signs in **once**; after that the device holds their `participant_id` and the app
works with this server switched off. So these tests check that login hands back an identity the
device can cache, and that nothing about syncing depends on having logged in.
"""

import pytest

from api.app.models import Participant, ParticipantAccount
from api.app.routes import participants as participants_route

GOOD_PASSWORD = "Abhi@2004"


def register(client, headers, username="abhi_22", password=GOOD_PASSWORD, display_name="Abhinay"):
    return client.post(
        "/api/v1/participants/register",
        json={"username": username, "password": password, "display_name": display_name},
        headers=headers,
    )


def login(client, headers, username="abhi_22", password=GOOD_PASSWORD):
    return client.post(
        "/api/v1/participants/login",
        json={"username": username, "password": password},
        headers=headers,
    )


# ------------------------------------------------------------------ registration


def test_registering_creates_an_account_and_a_participant_record(client, auth_headers):
    response = register(client, auth_headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["username"] == "abhi_22"
    assert body["display_name"] == "Abhinay"
    assert body["participant_id"].startswith("p-")


def test_the_participant_id_reveals_nothing_about_the_person(client, auth_headers):
    body = register(client, auth_headers).json()

    # Spec 18 §40: the research dataset is keyed on this, so it must not encode the username,
    # the display name, or the enrolment order.
    assert "abhi" not in body["participant_id"].lower()
    assert "abhinay" not in body["participant_id"].lower()
    assert len(body["participant_id"]) > 12


def test_two_participants_get_different_ids(client, auth_headers):
    first = register(client, auth_headers, username="one_aa").json()
    second = register(client, auth_headers, username="two_bb").json()

    assert first["participant_id"] != second["participant_id"]


def test_a_duplicate_username_is_refused_clearly(client, auth_headers):
    register(client, auth_headers)

    response = register(client, auth_headers)

    # The one place enumeration is acceptable: someone choosing a username has to be told it is
    # taken. Login stays deliberately vague; this does not.
    assert response.status_code == 409
    assert "taken" in response.json()["detail"].lower()


def test_registration_applies_the_password_policy(client, auth_headers):
    response = register(client, auth_headers, password="weak")

    assert response.status_code == 422
    assert "at least" in response.json()["detail"]


def test_registration_applies_the_username_rules(client, auth_headers):
    assert register(client, auth_headers, username="ab").status_code == 422
    assert register(client, auth_headers, username="Has Spaces").status_code == 422


def test_registration_needs_the_device_key(client):
    # Otherwise this is an open registration form for anyone on the study's network.
    response = client.post(
        "/api/v1/participants/register",
        json={"username": "sneaky", "password": GOOD_PASSWORD},
    )
    assert response.status_code == 401


def test_the_password_is_not_stored_recoverably(client, auth_headers):
    register(client, auth_headers)

    from api.app.database import get_db
    from api.app.main import app

    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        account = db.get(ParticipantAccount, "abhi_22")
        assert GOOD_PASSWORD not in account.password_hash
        assert account.password_hash.startswith("scrypt$")
    finally:
        generator.close()


def test_the_credential_lives_apart_from_the_research_record(client, auth_headers):
    body = register(client, auth_headers).json()

    from api.app.database import get_db
    from api.app.main import app

    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        participant = db.get(Participant, body["participant_id"])
        # Spec 18: `participants` is joined to every event, so nothing identifying may sit on it.
        # The username and hash live only on `participant_accounts`.
        assert not hasattr(participant, "password_hash")
        assert not hasattr(participant, "username")
    finally:
        generator.close()


def test_bulk_account_creation_from_one_device_is_throttled(client, auth_headers, monkeypatch):
    """A tight per-device ceiling on /register: one device may create a couple of accounts (sign
    up, or delete and sign up again), not account after account. Shrunk from 3 to 2 so the test
    makes two scrypt hashes rather than three."""
    monkeypatch.setattr(participants_route, "MAX_REGISTRATIONS_PER_DEVICE", 2)
    device = {**auth_headers, "X-Device-Id": "phone-A"}

    assert register(client, device, username="one_aa").status_code == 201
    # A refused attempt in between must not consume the quota — only real creations count.
    assert register(client, device, username="ab").status_code == 422
    assert register(client, device, username="two_bb").status_code == 201

    blocked = register(client, device, username="three_cc")
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_each_device_has_its_own_quota(client, auth_headers, monkeypatch):
    """The point of keying on the device, not the address: a room full of participants enrolling
    on their own phones share one IP but not one bucket, so one phone hitting the cap never blocks
    the next person's."""
    monkeypatch.setattr(participants_route, "MAX_REGISTRATIONS_PER_DEVICE", 1)

    phone_a = {**auth_headers, "X-Device-Id": "phone-A"}
    phone_b = {**auth_headers, "X-Device-Id": "phone-B"}

    assert register(client, phone_a, username="alice_a").status_code == 201
    assert register(client, phone_a, username="alice_2").status_code == 429  # A is spent
    # B is a different phone behind the same address, and is unaffected.
    assert register(client, phone_b, username="bob_bb").status_code == 201


# ------------------------------------------------------------------------ login


def test_login_returns_the_same_identity_registration_issued(client, auth_headers):
    registered = register(client, auth_headers).json()

    signed_in = login(client, auth_headers).json()

    # The device caches this and uses it forever after; a different id per login would split one
    # participant's data across two rows in the dataset.
    assert signed_in["participant_id"] == registered["participant_id"]


def test_login_is_case_insensitive_on_the_username(client, auth_headers):
    register(client, auth_headers)

    assert login(client, auth_headers, username="ABHI_22").status_code == 200


def test_a_wrong_password_and_an_unknown_user_are_indistinguishable(client, auth_headers):
    register(client, auth_headers)

    wrong = login(client, auth_headers, password="Wr0ng!Password")
    unknown = login(client, auth_headers, username="nobody", password="Wr0ng!Password")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_repeated_failures_are_throttled(client, auth_headers):
    register(client, auth_headers)

    for _ in range(participants_route.MAX_FAILURES_PER_USERNAME):
        login(client, auth_headers, password="Wr0ng!Password")

    blocked = login(client, auth_headers, password="Wr0ng!Password")
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    # And the throttle holds against the right password too, or it is only an inconvenience.
    assert login(client, auth_headers).status_code == 429


def test_the_throttle_runs_before_the_password_is_hashed(client, auth_headers, monkeypatch):
    register(client, auth_headers)
    for _ in range(participants_route.MAX_FAILURES_PER_USERNAME):
        login(client, auth_headers, password="Wr0ng!Password")

    calls = []
    monkeypatch.setattr(
        participants_route, "verify_password", lambda *a, **k: calls.append(1) or False
    )

    assert login(client, auth_headers, password="Wr0ng!Password").status_code == 429
    assert calls == [], "a throttled request must not reach the ~540ms hash"


# ------------------------------------------------- the offline guarantee


def test_syncing_never_requires_a_participant_login(client, auth_headers):
    """Spec 04 freeze item 2, stated as a test.

    A device that has never called /login can still post events. That is the whole reason login
    returns an id rather than a session: a dead server or a forgotten password must not be able
    to strand someone mid-study.
    """
    batch = {
        "batch_id": "batch-offline-1",
        "participant_id": "p-never-logged-in",
        "app_version": "1.0",
        "schema_version": "1",
        "records": [
            {
                "event_id": "evt-offline-1",
                "participant_id": "p-never-logged-in",
                "event_type": "craving_reported",
                "timestamp": "2026-08-28T04:05:06.007+00:00",
                "session_id": None,
                "payload": '{"intensity":"6"}',
                "schema_version": "1",
                "created_at": "2026-08-28T04:05:06.007+00:00",
            }
        ],
    }

    response = client.post("/api/v1/sync", json=batch, headers=auth_headers)

    assert response.status_code == 200, response.text
    assert response.json()["accepted_count"] == 1


def test_login_issues_no_session_cookie(client, auth_headers):
    register(client, auth_headers)

    response = login(client, auth_headers)

    # A session would imply the device has to keep re-authenticating, which is exactly what
    # freeze item 2 forbids. The identity is the whole answer.
    assert "set-cookie" not in {k.lower() for k in response.headers}
    assert "token" not in response.json()


def test_a_participant_login_is_not_an_operator_login(client, auth_headers):
    register(client, auth_headers)
    login(client, auth_headers)

    # Signing in as a study subject must not open the researcher dashboard.
    assert client.get("/api/v1/auth/me").status_code == 401


def close_account(client, auth_headers, participant_id):
    return client.delete(
        f"/api/v1/participants/account?participant_id={participant_id}",
        headers=auth_headers,
    )


def test_closing_an_account_stops_the_same_login_working(client, auth_headers):
    """Asked for directly: after requesting deletion the participant must not be able to return
    to the same account, and should have to create a new one."""
    created = register(client, auth_headers)
    assert created.status_code == 201
    participant_id = created.json()["participant_id"]

    # It works before.
    assert login(client, auth_headers).status_code == 200

    closed = close_account(client, auth_headers, participant_id)
    assert closed.status_code == 200
    assert closed.json()["account_removed"] is True

    # And not after. Clearing the phone could never achieve this - the credential lives here.
    assert login(client, auth_headers).status_code == 401


def test_closing_an_account_is_idempotent(client, auth_headers):
    """The phone retries after a dropped connection, and a 404 on the second attempt would tell
    the participant deletion failed when it had already succeeded."""
    participant_id = register(client, auth_headers).json()["participant_id"]

    assert close_account(client, auth_headers, participant_id).json()["account_removed"] is True
    second = close_account(client, auth_headers, participant_id)
    assert second.status_code == 200
    assert second.json()["account_removed"] is False

    unknown = close_account(client, auth_headers, "p-never-existed")
    assert unknown.status_code == 200
    assert unknown.json()["account_removed"] is False


def test_closing_an_account_leaves_the_research_dataset_intact(client, auth_headers):
    """The events are the study's data, not the participant's own copy. Removing them mid-study
    would change results other participants are part of."""
    participant_id = register(client, auth_headers).json()["participant_id"]

    client.post(
        "/api/v1/sync",
        json={
            "batch_id": "b-close-1",
            "participant_id": participant_id,
            "app_version": "1.0",
            "schema_version": "1",
            "records": [
                {
                    "event_id": "e-close-1",
                    "participant_id": participant_id,
                    "event_type": "craving_reported",
                    "timestamp": "2026-08-31T10:00:00Z",
                    "payload": "{}",
                    "schema_version": "1",
                    "created_at": "2026-08-31T10:00:00Z",
                }
            ],
        },
        headers=auth_headers,
    )

    close_account(client, auth_headers, participant_id)

    # The events survive: a second sync of the same event_id is reported as a duplicate,
    # which is only possible if the original row is still there.
    replay = client.post(
        "/api/v1/sync",
        json={
            "batch_id": "b-close-2",
            "participant_id": participant_id,
            "app_version": "1.0",
            "schema_version": "1",
            "records": [
                {
                    "event_id": "e-close-1",
                    "participant_id": participant_id,
                    "event_type": "craving_reported",
                    "timestamp": "2026-08-31T10:00:00Z",
                    "payload": "{}",
                    "schema_version": "1",
                    "created_at": "2026-08-31T10:00:00Z",
                }
            ],
        },
        headers=auth_headers,
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate_count"] == 1

    # But the way back to a person is gone.
    assert login(client, auth_headers).status_code == 401


def test_a_closed_username_can_never_be_registered_again(client, auth_headers):
    """The requirement, in the product owner's words: block the username so they have to sign up
    with a new one.

    This test previously asserted the opposite, and it passed - because closing the account
    DELETED the row and freed the name. A live end-to-end run caught it: a closed `kaushik`
    registered as `kaushik` again and got a working account back, which defeats the point of
    deletion. Closing is a tombstone now.
    """
    participant_id = register(client, auth_headers).json()["participant_id"]
    close_account(client, auth_headers, participant_id)

    again = register(client, auth_headers)
    assert again.status_code == 409, again.text
    # And the message has to say why, or it reads as a mistake they can retry out of.
    assert "deleted account" in again.json()["detail"]


def test_a_different_username_is_still_available_after_closing(client, auth_headers):
    """Blocking one name must not block signing up at all - creating a new account is exactly
    what the participant is being told to do."""
    participant_id = register(client, auth_headers).json()["participant_id"]
    close_account(client, auth_headers, participant_id)

    fresh = register(client, auth_headers, username="abhi_23")
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["participant_id"] != participant_id


def test_closing_severs_the_link_between_the_username_and_the_dataset(client, auth_headers):
    """Spec 18 §68's de-identification step is the removal of the username-to-participant_id
    join, and keeping the row must not quietly keep that join alive."""
    from api.app.models import ParticipantAccount

    participant_id = register(client, auth_headers).json()["participant_id"]
    close_account(client, auth_headers, participant_id)

    # client.sessions, not SessionLocal - the latter is the real PostgreSQL and ignores the
    # get_db override entirely.
    with client.sessions() as session:
        row = session.get(ParticipantAccount, "abhi_22")
        assert row is not None, "the username row must survive, or the name is free again"
        assert row.participant_id is None, "the join to the research dataset must be severed"
        assert row.display_name is None, "profile detail must be gone"
        assert row.closed_at is not None
        assert row.password_hash == "closed-account-no-password"


def test_a_closed_account_cannot_log_in_even_with_the_right_password(client, auth_headers):
    """The password hash is replaced, but the refusal is on `closed_at` - so this holds even if
    somebody later restores a real hash into the row."""
    participant_id = register(client, auth_headers).json()["participant_id"]
    close_account(client, auth_headers, participant_id)

    refused = login(client, auth_headers)
    assert refused.status_code == 401
    # Same wording as a username that never existed: saying "this was deleted" would confirm to
    # a stranger that the name was once real.
    assert refused.json()["detail"] == "Invalid username or password."
