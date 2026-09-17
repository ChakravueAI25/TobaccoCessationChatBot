"""Tests for the research read and export endpoints.

Spec 23 §14 acceptance criteria covered here: duplicate events are not inserted twice,
multiple devices synchronise, PostgreSQL holds validated research records, and exports can be
generated without corrupting the source.
"""
from datetime import datetime, timedelta, timezone
import csv
import io
import json
import zipfile

import pytest


def record(event_id="event-1", participant="participant-a", event_type="app_opened", when=None):
    timestamp = (when or datetime.now(timezone.utc)).isoformat()
    return {
        "event_id": event_id,
        "participant_id": participant,
        "session_id": "session-1",
        "event_type": event_type,
        "timestamp": timestamp,
        "payload": json.dumps({"synthetic": True}),
        "schema_version": "1",
        "created_at": timestamp,
    }


def batch(batch_id, participant="participant-a", records=None):
    return {
        "batch_id": batch_id,
        "participant_id": participant,
        "app_version": "1.0.0",
        "schema_version": "1",
        "records": records or [record()],
    }


@pytest.fixture()
def seeded(client, auth_headers):
    """Two participants, four events, one duplicate attempt."""
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    client.post(
        "/api/v1/sync",
        json=batch(
            "batch-a1",
            "participant-a",
            [
                record("a-1", "participant-a", "app_opened", base),
                record("a-2", "participant-a", "conversation_turn", base + timedelta(minutes=5)),
            ],
        ),
        headers=auth_headers,
    )
    client.post(
        "/api/v1/sync",
        json=batch(
            "batch-b1",
            "participant-b",
            [
                record("b-1", "participant-b", "app_opened", base + timedelta(minutes=10)),
                record("b-2", "participant-b", "safety_event", base + timedelta(minutes=20)),
            ],
        ),
        headers=auth_headers,
    )
    return client


def test_stats_counts_everything_that_synced(seeded, auth_headers):
    body = seeded.get("/api/v1/stats", headers=auth_headers).json()
    assert body["participants"] == 2
    assert body["events"] == 4
    assert body["batches"] == 2
    assert body["events_by_type"]["app_opened"] == 2
    assert body["latest_event_at"] is not None


def test_participants_view_reports_per_device_counts(seeded, auth_headers):
    rows = seeded.get("/api/v1/participants", headers=auth_headers).json()
    by_id = {row["participant_id"]: row for row in rows}
    assert set(by_id) == {"participant-a", "participant-b"}
    assert by_id["participant-a"]["event_count"] == 2
    assert by_id["participant-a"]["batch_count"] == 1
    assert by_id["participant-b"]["event_count"] == 2


def test_events_are_paged_newest_first(seeded, auth_headers):
    first = seeded.get("/api/v1/events?limit=2", headers=auth_headers).json()
    assert len(first["items"]) == 2
    assert first["has_more"] is True
    # b-2 is the most recent event in the fixture.
    assert first["items"][0]["event_id"] == "b-2"

    second = seeded.get("/api/v1/events?limit=2&offset=2", headers=auth_headers).json()
    assert second["has_more"] is False
    assert {item["event_id"] for item in second["items"]} == {"a-1", "a-2"}


def test_events_filter_by_participant_and_type(seeded, auth_headers):
    only_a = seeded.get("/api/v1/events?participant_id=participant-a", headers=auth_headers).json()
    assert {item["participant_id"] for item in only_a["items"]} == {"participant-a"}

    safety = seeded.get("/api/v1/events?event_type=safety_event", headers=auth_headers).json()
    assert [item["event_id"] for item in safety["items"]] == ["b-2"]


def test_research_endpoints_require_the_api_key(seeded):
    for path in ("/api/v1/stats", "/api/v1/participants", "/api/v1/events"):
        assert seeded.get(path).status_code == 401
    assert seeded.post("/api/v1/exports", json={"format": "csv"}).status_code == 401


def test_csv_export_contains_every_event(seeded, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORT_DIRECTORY", str(tmp_path))
    from api.app.config import get_settings

    get_settings.cache_clear()
    try:
        response = seeded.post("/api/v1/exports", json={"format": "csv"}, headers=auth_headers)
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "completed"
        assert body["row_count"] == 4

        written = tmp_path / body["file_path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        text = written.read_text(encoding="utf-8")
        assert "event_id,participant_id" in text
        for event_id in ("a-1", "a-2", "b-1", "b-2"):
            assert event_id in text
    finally:
        get_settings.cache_clear()


def test_json_and_xlsx_exports_are_wellformed(seeded, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORT_DIRECTORY", str(tmp_path))
    from api.app.config import get_settings

    get_settings.cache_clear()
    try:
        as_json = seeded.post("/api/v1/exports", json={"format": "json"}, headers=auth_headers).json()
        parsed = json.loads((tmp_path / as_json["file_path"].replace("\\", "/").rsplit("/", 1)[-1]).read_text("utf-8"))
        assert len(parsed) == 4
        assert parsed[0]["event_id"] == "a-1"

        as_xlsx = seeded.post("/api/v1/exports", json={"format": "xlsx"}, headers=auth_headers).json()
        path = tmp_path / as_xlsx["file_path"].replace("\\", "/").rsplit("/", 1)[-1]
        # A real .xlsx must be a zip carrying these four parts, or Excel refuses to open it.
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/worksheets/sheet1.xml"} <= names
            sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "a-1" in sheet and "event_id" in sheet
    finally:
        get_settings.cache_clear()


def test_export_scope_filters_rows(seeded, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORT_DIRECTORY", str(tmp_path))
    from api.app.config import get_settings

    get_settings.cache_clear()
    try:
        body = seeded.post(
            "/api/v1/exports",
            json={"format": "json", "participant_id": "participant-b"},
            headers=auth_headers,
        ).json()
        assert body["row_count"] == 2
    finally:
        get_settings.cache_clear()


def test_export_rejects_an_unknown_format(seeded, auth_headers):
    response = seeded.post("/api/v1/exports", json={"format": "parquet"}, headers=auth_headers)
    assert response.status_code == 422


def test_export_does_not_modify_the_source(seeded, auth_headers, tmp_path, monkeypatch):
    """Spec 23 §14: exports must not corrupt the source data."""
    monkeypatch.setenv("EXPORT_DIRECTORY", str(tmp_path))
    from api.app.config import get_settings

    get_settings.cache_clear()
    try:
        before = seeded.get("/api/v1/stats", headers=auth_headers).json()
        seeded.post("/api/v1/exports", json={"format": "csv"}, headers=auth_headers)
        seeded.post("/api/v1/exports", json={"format": "json"}, headers=auth_headers)
        after = seeded.get("/api/v1/stats", headers=auth_headers).json()
        assert before == after
    finally:
        get_settings.cache_clear()


def test_export_history_is_listed(seeded, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORT_DIRECTORY", str(tmp_path))
    from api.app.config import get_settings

    get_settings.cache_clear()
    try:
        seeded.post("/api/v1/exports", json={"format": "csv"}, headers=auth_headers)
        runs = seeded.get("/api/v1/exports", headers=auth_headers).json()
        assert len(runs) == 1
        assert runs[0]["format"] == "csv"
        assert runs[0]["status"] == "completed"
    finally:
        get_settings.cache_clear()


def test_download_rejects_a_path_outside_the_export_directory(seeded, auth_headers, tmp_path, monkeypatch):
    """An export_runs row with a doctored path must not become an arbitrary file read."""
    monkeypatch.setenv("EXPORT_DIRECTORY", str(tmp_path / "exports"))
    from api.app.config import get_settings

    get_settings.cache_clear()
    try:
        created = seeded.post("/api/v1/exports", json={"format": "csv"}, headers=auth_headers).json()

        outside = tmp_path / "secret.txt"
        outside.write_text("not for download", encoding="utf-8")

        from api.app.database import get_db
        from api.app.main import app
        from api.app.models import ExportRun

        db = next(app.dependency_overrides[get_db]())
        run = db.get(ExportRun, created["export_id"])
        run.file_path = str(outside)
        db.commit()

        response = seeded.get(f"/api/v1/exports/{created['export_id']}/download", headers=auth_headers)
        assert response.status_code == 403
    finally:
        get_settings.cache_clear()


def test_quick_csv_download_streams_without_recording_a_run(seeded, auth_headers):
    response = seeded.get("/api/v1/events.csv", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "a-1" in response.text
    assert seeded.get("/api/v1/exports", headers=auth_headers).json() == []

    # payload is stored decoded, so every export path has to serialise it back to JSON. Without
    # that, csv.DictWriter writes Python's repr of the dict -- {'synthetic': True}, single
    # quotes and a capitalised True -- which no downstream tool can parse.
    row = next(csv.DictReader(io.StringIO(response.text)))
    assert json.loads(row["payload"]) == {"synthetic": True}


def test_the_quick_csv_refuses_past_its_row_cap_rather_than_truncating(seeded, auth_headers, monkeypatch):
    """Finding 5. The quick-look CSV read the whole table into one response body.

    It refuses instead of truncating because a short CSV that looks complete is the failure
    that ends up in an analysis with nobody able to tell. `POST /exports` stays uncapped for
    the same reason from the other direction: it is the one that has to be complete.
    """
    from api.app.routes import research

    monkeypatch.setattr(research, "QUICK_LOOK_ROW_LIMIT", 2)

    response = seeded.get("/api/v1/events.csv", headers=auth_headers)
    assert response.status_code == 413
    assert "POST /api/v1/exports" in response.json()["detail"]

    # Under the cap it still just works, and a filter that narrows the scope is the documented
    # way back under it.
    narrowed = seeded.get("/api/v1/events.csv?participant_id=participant-b", headers=auth_headers)
    assert narrowed.status_code == 200

    # The full export is deliberately not capped -- it must never come back short.
    created = seeded.post("/api/v1/exports", json={"format": "csv"}, headers=auth_headers)
    assert created.status_code == 201
    assert created.json()["row_count"] > 2


def test_dashboard_is_served_and_carries_no_data(client):
    """The page must be loadable without a key, and must not embed the key or any records."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Quit Smoke research data" in response.text
    assert "change-me-development-key" not in response.text


def test_dashboard_escapes_every_device_supplied_field(client):
    """Stored XSS guard.

    participant_id, app_version, event_type, session_id and event_id all arrive from a device
    over /sync, so they are attacker-controlled by anyone holding the API key — which is
    extractable from the APK. The dashboard renders them into innerHTML, so `cell()` has to
    escape or an event_type is script execution in a signed-in researcher's browser.
    """
    page = client.get("/").text

    # The helper every table row goes through must escape, not interpolate raw.
    assert "escapeHtml(value)" in page, "cell() is interpolating device data unescaped"
    assert 'String(value) + "</td>"' not in page


def test_the_research_routes_are_rate_limited_and_health_is_not(client, auth_headers):
    """Finding 6: only the login routes were throttled.

    The device API key is compiled into the APK and extractable, so "authenticated as a device"
    is "anyone holding the app" — and /sync, /events, /exports and /events.csv had no ceiling
    at all.
    """
    from api.app.dependencies import RATE_LIMIT_REQUESTS

    for _ in range(RATE_LIMIT_REQUESTS):
        assert client.get("/api/v1/stats", headers=auth_headers).status_code == 200

    limited = client.get("/api/v1/stats", headers=auth_headers)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"

    # /health is deliberately outside the limit: it is how the operator finds out the server is
    # up, and it must answer when everything else is refusing.
    assert client.get("/health").status_code == 200


def test_the_rate_limit_cannot_be_reached_by_the_app_or_the_dashboard(client, auth_headers):
    """The ceiling is sized to be unreachable in normal use, which is the whole point.

    Throttling a device draining a backlog would slow the study's data collection to stop an
    attack nobody is mounting, so this is the assertion that stops the limit being "tuned"
    down to something the app can actually hit. The device syncs once every five minutes and
    the dashboard issues four calls per manual refresh.
    """
    from api.app.dependencies import RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS

    device_requests_per_window = RATE_LIMIT_WINDOW_SECONDS / (5 * 60)
    assert RATE_LIMIT_REQUESTS > device_requests_per_window * 100

    dashboard_requests_per_refresh = 4
    assert RATE_LIMIT_REQUESTS > dashboard_requests_per_refresh * 20
