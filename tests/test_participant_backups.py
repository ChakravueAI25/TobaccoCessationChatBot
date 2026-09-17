"""Account restore, and the boundary that keeps it out of the research dataset.

Two things are being protected here and they pull in opposite directions, which is why the tests
are worth reading before changing the routes:

1. A participant who reinstalls must get their data back.
2. Nobody else must be able to get it, and it must never reach an export.

The second is the one a refactor breaks silently. A `GET /backup/{id}` behind the device key would
satisfy (1) perfectly and hand every participant's conversation to anyone holding the APK.
"""
from datetime import datetime, timedelta, timezone

from api.app.config import get_settings

GOOD_PASSWORD = "Restore@2026"


def register(client, headers, username="abhi_22", password=GOOD_PASSWORD):
    return client.post(
        "/api/v1/participants/register",
        json={"username": username, "password": password, "display_name": "Abhinay"},
        headers=headers,
    )


def sample_payload():
    return {
        "profile": {"display_name": "Abhinay", "goal_stage": "quit", "tobacco_type": "cigarettes"},
        "memories": [{"key": "interest:cars", "value": "interest:cars"}],
        "messages": [
            {"id": "m1", "role": "user", "text": "hey", "timestamp": 1756600000000},
            {"id": "m2", "role": "assistant", "text": "Hello.", "timestamp": 1756600001000},
        ],
    }


def upload(client, headers, participant_id, *, when=None, payload=None):
    return client.put(
        "/api/v1/backup",
        json={
            "participant_id": participant_id,
            "schema_version": "1",
            "device_updated_at": (when or datetime.now(timezone.utc)).isoformat(),
            "payload": payload or sample_payload(),
        },
        headers=headers,
    )


# ------------------------------------------------------------------ the feature


def test_a_reinstall_gets_the_participants_data_back(client, auth_headers):
    """The whole point: uninstall, reinstall, log in, and it is all there."""
    participant_id = register(client, auth_headers).json()["participant_id"]
    assert upload(client, auth_headers, participant_id).status_code == 200

    # A fresh install has no local data, so it asks for the backup as it logs in.
    restored = client.post(
        "/api/v1/participants/login",
        json={"username": "abhi_22", "password": GOOD_PASSWORD, "include_backup": True},
        headers=auth_headers,
    )

    assert restored.status_code == 200
    backup = restored.json()["backup"]
    assert backup is not None
    assert backup["payload"] == sample_payload()
    assert backup["schema_version"] == "1"


def test_an_ordinary_login_does_not_carry_the_conversation(client, auth_headers):
    """`include_backup` is off by default.

    A participant signing in on a device that already has their data does not need it sent again,
    and shipping a conversation across the wire for no reason is the kind of thing that is only
    noticed when it is already habit.
    """
    participant_id = register(client, auth_headers).json()["participant_id"]
    upload(client, auth_headers, participant_id)

    response = client.post(
        "/api/v1/participants/login",
        json={"username": "abhi_22", "password": GOOD_PASSWORD},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["backup"] is None


def test_a_first_signup_has_nothing_to_restore_and_says_so(client, auth_headers):
    """Null, not an error. The app reads it as "nothing stored yet" and carries on."""
    register(client, auth_headers)

    response = client.post(
        "/api/v1/participants/login",
        json={"username": "abhi_22", "password": GOOD_PASSWORD, "include_backup": True},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["backup"] is None


# ------------------------------------------------------------------ the boundary


def test_the_device_key_alone_can_never_read_a_backup(client, auth_headers):
    """The security design, stated as a test.

    The device API key is compiled into the APK and is therefore public to anyone holding the
    app. There must be **no route** that returns a participant's data for that key alone - the
    only way in is username and password.
    """
    participant_id = register(client, auth_headers).json()["participant_id"]
    upload(client, auth_headers, participant_id)

    # Every shape someone would try.
    for path in (
        f"/api/v1/backup/{participant_id}",
        "/api/v1/backup",
        f"/api/v1/participants/{participant_id}/backup",
    ):
        response = client.get(path, headers=auth_headers)
        assert response.status_code in (404, 405), f"{path} answered a GET with the device key"


def test_a_wrong_password_returns_no_backup(client, auth_headers):
    participant_id = register(client, auth_headers).json()["participant_id"]
    upload(client, auth_headers, participant_id)

    response = client.post(
        "/api/v1/participants/login",
        json={"username": "abhi_22", "password": "not the password", "include_backup": True},
        headers=auth_headers,
    )

    assert response.status_code == 401
    assert "backup" not in response.text


def test_uploading_needs_the_api_key(client, auth_headers):
    participant_id = register(client, auth_headers).json()["participant_id"]
    response = client.put(
        "/api/v1/backup",
        json={
            "participant_id": participant_id,
            "schema_version": "1",
            "device_updated_at": datetime.now(timezone.utc).isoformat(),
            "payload": sample_payload(),
        },
    )
    assert response.status_code in (401, 403)


def test_backups_never_reach_an_export(client, auth_headers):
    """The research dataset must not contain any of this.

    `services/export.py` names `Event`, `Participant` and `SyncBatch` explicitly rather than
    enumerating tables, which is what makes this safe - but "the code happens to be written that
    way" is not a guarantee, so this asserts the outcome.
    """
    participant_id = register(client, auth_headers).json()["participant_id"]
    upload(client, auth_headers, participant_id)

    # Every read the research side offers, not just one of them.
    for path in ("/api/v1/events.csv", "/api/v1/events", "/api/v1/participants", "/api/v1/stats"):
        response = client.get(path, headers=auth_headers)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        body = response.text
        # The distinctive strings from the conversation and the profile.
        for secret in ("Hello.", "interest:cars", "cigarettes"):
            assert secret not in body, f"{secret!r} leaked into {path}"


def test_an_unknown_participant_cannot_create_one(client, auth_headers):
    """Uploading must not enrol.

    Registration is already the one open security finding; a second route that creates
    participant rows would widen it without anyone deciding to.
    """
    response = upload(client, auth_headers, "not-a-real-participant")
    assert response.status_code == 404


# ------------------------------------------------------------------ behaviour under stress


def test_the_newer_device_wins_and_the_older_one_does_not_clobber_it(client, auth_headers):
    """Two phones on one account.

    Login returns the same participant_id on any device, so this is reachable. Ordering by the
    *device's* clock rather than arrival time means the loser is whichever was actually older,
    not whichever happened to sync second.
    """
    participant_id = register(client, auth_headers).json()["participant_id"]
    newer = datetime.now(timezone.utc)
    older = newer - timedelta(hours=3)

    upload(client, auth_headers, participant_id, when=newer, payload={"marker": "newer"})
    stale = upload(client, auth_headers, participant_id, when=older, payload={"marker": "older"})

    assert stale.status_code == 200
    assert stale.json()["stored"] is False

    restored = client.post(
        "/api/v1/participants/login",
        json={"username": "abhi_22", "password": GOOD_PASSWORD, "include_backup": True},
        headers=auth_headers,
    ).json()
    assert restored["backup"]["payload"] == {"marker": "newer"}


def test_uploading_twice_replaces_rather_than_accumulating(client, auth_headers):
    """One row per participant.

    History would multiply the most sensitive data in the database, and nothing asked for it -
    this is a restore point, not an audit trail.
    """
    participant_id = register(client, auth_headers).json()["participant_id"]
    first = datetime.now(timezone.utc) - timedelta(minutes=5)

    upload(client, auth_headers, participant_id, when=first, payload={"marker": "first"})
    upload(client, auth_headers, participant_id, payload={"marker": "second"})

    restored = client.post(
        "/api/v1/participants/login",
        json={"username": "abhi_22", "password": GOOD_PASSWORD, "include_backup": True},
        headers=auth_headers,
    ).json()
    assert restored["backup"]["payload"] == {"marker": "second"}


def test_an_oversized_payload_is_refused(client, auth_headers):
    """A device must not be able to fill the disk."""
    participant_id = register(client, auth_headers).json()["participant_id"]
    huge = {"messages": [{"text": "x" * 1024} for _ in range(6000)]}

    response = upload(client, auth_headers, participant_id, payload=huge)
    assert response.status_code == 413


def test_syncing_research_events_still_needs_no_participant_login(client, auth_headers):
    """Freeze item 2 is untouched by any of this.

    The device caches an id and uploads with the device key forever. Adding a route that *does*
    need a password must not have made the ordinary path need one too.
    """
    participant_id = register(client, auth_headers).json()["participant_id"]
    response = client.post(
        "/api/v1/sync",
        json={
            "participant_id": participant_id,
            "batch_id": "batch-1",
            "app_version": "1.0.0",
            "schema_version": "1",
            "records": [
                {
                    "event_id": "e1",
                    "participant_id": participant_id,
                    "event_type": "session_started",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "schema_version": "1",
                    "payload": '{"a":1}',
                }
            ],
        },
        headers=auth_headers,
    )
    assert response.status_code == 200


# ------------------------------------------------------------------ deletion


def test_deleting_removes_the_backup_so_a_re_login_cannot_restore_it(client, auth_headers):
    """The bug this route exists to prevent.

    Clearing the phone alone would leave the profile and the conversation on the server, and the
    next sign-in would put them straight back. A deletion that undoes itself is worse than none,
    because the participant believes it worked.
    """
    participant_id = register(client, auth_headers).json()["participant_id"]
    upload(client, auth_headers, participant_id)

    deleted = client.request(
        "DELETE",
        "/api/v1/backup",
        params={"participant_id": participant_id},
        headers=auth_headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    restored = client.post(
        "/api/v1/participants/login",
        json={"username": "abhi_22", "password": GOOD_PASSWORD, "include_backup": True},
        headers=auth_headers,
    )
    assert restored.status_code == 200
    assert restored.json()["backup"] is None, "the backup came back after a deletion request"


def test_deleting_a_backup_leaves_the_research_events_alone(client, auth_headers):
    """Research data is the study's, not the participant's own copy.

    It is pseudonymous, and removing rows mid-study would silently change results that other
    participants' data is part of.
    """
    participant_id = register(client, auth_headers).json()["participant_id"]
    client.post(
        "/api/v1/sync",
        json={
            "participant_id": participant_id,
            "batch_id": "batch-keep",
            "app_version": "1.0.0",
            "schema_version": "1",
            "records": [
                {
                    "event_id": "keep-1",
                    "participant_id": participant_id,
                    "event_type": "craving_reported",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "schema_version": "1",
                    "payload": '{"trigger":"stress"}',
                }
            ],
        },
        headers=auth_headers,
    )
    upload(client, auth_headers, participant_id)

    client.request(
        "DELETE", "/api/v1/backup", params={"participant_id": participant_id}, headers=auth_headers
    )

    events = client.get("/api/v1/events", headers=auth_headers).json()
    assert any(e["event_id"] == "keep-1" for e in events["items"]), "a research event was removed"


def test_deleting_twice_is_not_an_error(client, auth_headers):
    """The caller asked for it to not exist, and it does not."""
    participant_id = register(client, auth_headers).json()["participant_id"]
    upload(client, auth_headers, participant_id)

    first = client.request(
        "DELETE", "/api/v1/backup", params={"participant_id": participant_id}, headers=auth_headers
    )
    second = client.request(
        "DELETE", "/api/v1/backup", params={"participant_id": participant_id}, headers=auth_headers
    )

    assert first.json()["deleted"] is True
    assert second.status_code == 200
    assert second.json()["deleted"] is False
