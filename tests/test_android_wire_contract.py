"""The exact bytes the Android transport sends.

Both sides of this contract were written by reading the other side's source, and until the
uploader was wired into the app nothing had ever sent a byte across it. The three ways it can
break are all silent from the phone's point of view — a 422 looks the same as a server fault:

* ``payload`` sent as a nested object instead of a JSON string,
* ``schema_version`` sent as the number ``1`` instead of the string ``"1"``,
* a timestamp the hand-rolled Android formatter produces that Pydantic will not parse.

The body below is pinned by
``shared/src/androidHostTest/.../HttpResearchSyncTransportTest.kt``
(``theEncodedBodyMatchesTheBackendSyncRequestSchema``), which asserts the same field values
against a real socket. Change one side and both tests fail.
"""

import json

# Captured from HttpResearchSyncTransport.encode. Kept as a literal on purpose: a fixture built
# from the Pydantic models would agree with itself and prove nothing about the Kotlin encoder.
ANDROID_BATCH = {
    "batch_id": "batch-evt-1-1",
    "participant_id": "p-abc",
    "app_version": "1.0",
    "schema_version": "1",
    "records": [
        {
            "event_id": "evt-1",
            "participant_id": "p-abc",
            "event_type": "craving_reported",
            "timestamp": "2026-08-28T04:05:06.007+00:00",
            "session_id": "s-1",
            "payload": '{"intensity":"6"}',
            "schema_version": "1",
            "created_at": "2026-08-28T04:05:06.007+00:00",
        }
    ],
}


def test_android_transport_wire_format_is_accepted(client, auth_headers):
    response = client.post("/api/v1/sync", json=ANDROID_BATCH, headers=auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted_count"] == 1
    assert body["duplicate_count"] == 0
    assert body["rejected_count"] == 0
    # The transport reads batch_id back out of the response, so it has to be there.
    assert body["batch_id"] == "batch-evt-1-1"


def test_the_stored_event_matches_what_the_phone_sent(client, auth_headers):
    client.post("/api/v1/sync", json=ANDROID_BATCH, headers=auth_headers)

    events = client.get("/api/v1/events", headers=auth_headers).json()["items"]
    assert len(events) == 1
    stored = events[0]

    assert stored["event_id"] == "evt-1"
    assert stored["participant_id"] == "p-abc"
    assert stored["session_id"] == "s-1"
    assert stored["event_type"] == "craving_reported"
    # The Android timestamp survived the round trip rather than being reinterpreted as local
    # time — an off-by-hours shift here would silently corrupt every temporal analysis.
    assert stored["event_timestamp"].startswith("2026-08-28T04:05:06")
    # payload travels as a JSON *string* and is stored decoded, so a research query can reach
    # into it (`payload->>'intensity'`) rather than parsing text out of a column.
    #
    # This asserts on `stored`, not on ANDROID_BATCH. The previous version parsed the input
    # constant and compared it to itself, so it passed for the whole project while the column
    # actually held a double-encoded JSON string -- PostgreSQL's `json_typeof` reported
    # `string`, and every `payload->>...` query returned NULL.
    assert stored["payload"] == {"intensity": "6"}


def test_the_android_null_session_form_is_accepted(client, auth_headers):
    """The craving path records events with no session, so this is the common case."""
    batch = json.loads(json.dumps(ANDROID_BATCH))
    batch["batch_id"] = "batch-no-session"
    batch["records"][0]["event_id"] = "evt-no-session"
    batch["records"][0]["session_id"] = None

    response = client.post("/api/v1/sync", json=batch, headers=auth_headers)

    assert response.status_code == 200, response.text
    assert response.json()["accepted_count"] == 1


def test_a_payload_with_quotes_and_newlines_is_accepted(client, auth_headers):
    """A participant message can contain anything the keyboard produces."""
    batch = json.loads(json.dumps(ANDROID_BATCH))
    batch["batch_id"] = "batch-awkward"
    batch["records"][0]["event_id"] = "evt-awkward"
    batch["records"][0]["payload"] = json.dumps({"message": 'he said "no"\nthen left'})

    response = client.post("/api/v1/sync", json=batch, headers=auth_headers)

    assert response.status_code == 200, response.text
    assert response.json()["accepted_count"] == 1


def test_schema_version_as_a_number_is_rejected(client, auth_headers):
    """Guards the trap the Kotlin side documents: schema_version is a string, not an int."""
    batch = json.loads(json.dumps(ANDROID_BATCH))
    batch["schema_version"] = 1

    response = client.post("/api/v1/sync", json=batch, headers=auth_headers)

    assert response.status_code == 422


def test_a_nested_object_payload_is_rejected(client, auth_headers):
    """The other trap: payload must be JSON *text*, not an object."""
    batch = json.loads(json.dumps(ANDROID_BATCH))
    batch["records"][0]["payload"] = {"intensity": "6"}

    response = client.post("/api/v1/sync", json=batch, headers=auth_headers)

    assert response.status_code == 422


def test_resending_the_same_batch_reports_duplicates_not_a_second_insert(client, auth_headers):
    """Spec 04 freeze item 9. The phone reuses batch_id on retry precisely so this holds."""
    first = client.post("/api/v1/sync", json=ANDROID_BATCH, headers=auth_headers).json()
    second = client.post("/api/v1/sync", json=ANDROID_BATCH, headers=auth_headers).json()

    assert first["accepted_count"] == 1
    assert second["accepted_count"] == first["accepted_count"]
    assert second["duplicate_count"] == first["duplicate_count"]

    events = client.get("/api/v1/events", headers=auth_headers).json()
    assert len(events["items"]) == 1, "a retried batch must not double-insert"
