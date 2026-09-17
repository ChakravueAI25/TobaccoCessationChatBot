from datetime import datetime, timezone
import json

from api.app.database import engine
from api.app.config import Settings
from api.app.database import get_db
from api.app.main import app
from api.app.schemas import SyncRequest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError


def record(event_id="event-1"):
    timestamp = datetime.now(timezone.utc).isoformat()
    return {"event_id": event_id, "participant_id": "synthetic-participant", "session_id": "synthetic-session",
            "event_type": "app_opened", "timestamp": timestamp, "payload": json.dumps({"synthetic": True}),
            "schema_version": "1", "created_at": timestamp}


def request(batch_id="batch-1", records=None):
    return {"batch_id": batch_id, "participant_id": "synthetic-participant", "app_version": "1.0.0", "schema_version": "1", "records": records or [record()]}


def test_configuration_loading(monkeypatch):
    monkeypatch.setenv("API_PORT", "8123")
    assert Settings().api_port == 8123


def test_database_connection_configuration():
    assert engine.url.drivername == "postgresql+psycopg"


def _fixed_model_state(monkeypatch, state: str) -> None:
    """Pins what /health sees, instead of letting it make a real network call.

    `.env` sets LLAMA_SERVER_URL, so without this the endpoint probes 127.0.0.1:8081 for real and
    the assertion depends on whether a GPU process happens to be running on the developer's
    machine. That is a test that passes in the morning and fails after lunch.
    """
    import api.app.routes.health as health_module

    async def probe() -> str:
        return state

    monkeypatch.setattr(health_module, "probe_model", probe)


def test_health(client, monkeypatch):
    # `model` is on /health deliberately. Reporting only the database meant a failed model task
    # left this service looking perfectly healthy while every chat turn fell back to
    # deterministic text - invisible from outside, and it stayed invisible for a day of testing.
    _fixed_model_state(monkeypatch, "ok")

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "model": "ok"}


def test_health_treats_no_configured_model_as_healthy(client, monkeypatch):
    """Not a fault. Spec 04 freeze item 2 requires this service to be useful with no inference
    server at all, and the app treats an absent model as fallback-only rather than as an error."""
    _fixed_model_state(monkeypatch, "not_configured")

    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["model"] == "not_configured"


def test_health_reports_degraded_when_the_model_is_unreachable(client, monkeypatch):
    """A configured model that cannot be reached must make the whole stack read degraded, or the
    green tick is a lie the operator acts on."""
    _fixed_model_state(monkeypatch, "unreachable")

    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["model"] == "unreachable"
    # The database is still fine, and saying so is the point of keeping them separate fields.
    assert body["database"] == "ok"


def test_health_distinguishes_a_model_that_is_still_loading(client, monkeypatch):
    """llama-server binds the port before the weights are read and answers 503 until they are.
    That is a different operational state from a process that is not running."""
    _fixed_model_state(monkeypatch, "loading")

    body = client.get("/health").json()
    assert body["model"] == "loading"
    assert body["status"] == "degraded"


def test_authentication_boundary(client):
    assert client.post("/api/v1/sync", json=request()).status_code == 401


def test_valid_sync_and_duplicate_event(client, auth_headers):
    first = client.post("/api/v1/sync", json=request(), headers=auth_headers)
    second = client.post("/api/v1/sync", json=request("batch-2"), headers=auth_headers)
    assert first.status_code == 200
    assert first.json()["accepted_count"] == 1
    assert second.status_code == 200
    assert second.json()["duplicate_count"] == 1
    assert second.json()["accepted_count"] == 0


def test_invalid_request_and_invalid_event(client, auth_headers):
    missing = client.post("/api/v1/sync", json={"batch_id": "b"}, headers=auth_headers)
    invalid_event = client.post("/api/v1/sync", json=request(records=[record("")]), headers=auth_headers)
    invalid_timestamp = client.post("/api/v1/sync", json=request("bad-time", records=[record() | {"event_id": "bad-time", "timestamp": "bad"}]), headers=auth_headers)
    invalid_schema = client.post("/api/v1/sync", json=request("bad-schema", records=[record() | {"event_id": "bad-schema", "schema_version": "2"}]), headers=auth_headers)
    invalid_payload = client.post("/api/v1/sync", json=request("bad-payload", records=[record() | {"event_id": "bad-payload", "payload": "not-json"}]), headers=auth_headers)
    missing_participant = client.post("/api/v1/sync", json=request("missing-participant") | {"participant_id": ""}, headers=auth_headers)
    assert missing.status_code == 422
    assert invalid_event.status_code == 422
    assert invalid_timestamp.status_code == 422
    assert invalid_schema.status_code == 422
    assert invalid_payload.status_code == 422
    assert missing_participant.status_code == 422


def test_transaction_rollback_and_multiple_records(client, auth_headers):
    duplicate_ids = [record("same"), record("same")]
    failed = client.post("/api/v1/sync", json=request("rollback-batch", duplicate_ids), headers=auth_headers)
    accepted = client.post("/api/v1/sync", json=request("multi-batch", [record("one"), record("two")]), headers=auth_headers)
    assert failed.status_code == 422
    assert accepted.status_code == 200
    assert accepted.json()["accepted_count"] == 2


def test_schema_request_is_structured():
    parsed = SyncRequest.model_validate(request())
    assert json.loads(parsed.records[0].payload)["synthetic"] is True
    assert parsed.records[0].session_id == "synthetic-session"
    assert parsed.records[0].created_at == parsed.records[0].timestamp


def _sync_with_session_raising(error, auth_headers, batch_id):
    """Posts one batch against a session whose first read raises `error`."""

    class FailedSession:
        def get(self, *_args):
            raise error

        def rollback(self):
            pass

    def failing_db():
        yield FailedSession()

    app.dependency_overrides[get_db] = failing_db
    try:
        return TestClient(app, raise_server_exceptions=False).post(
            "/api/v1/sync", json=request(batch_id), headers=auth_headers
        )
    finally:
        app.dependency_overrides.clear()


def test_an_unavailable_database_returns_a_retryable_503(auth_headers):
    response = _sync_with_session_raising(
        OperationalError("SELECT 1", {}, Exception("synthetic connection failure")),
        auth_headers,
        "db-unavailable",
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Sync batch could not be stored"}


def test_a_server_bug_is_not_disguised_as_a_retryable_503(auth_headers):
    """The failure mode this whole route is shaped around.

    The app reads 5xx as retryable and `ResearchSyncManager` always retries the *head* of its
    queue, so a bug that reliably 503s on one record used to mean that participant never
    uploaded anything again -- silently, with the app showing nothing wrong. A bug must surface
    as a 500 with a traceback in the log instead of hiding behind the same status the app is
    told to keep trying.
    """
    response = _sync_with_session_raising(
        RuntimeError("synthetic bug in the route"), auth_headers, "server-bug"
    )
    assert response.status_code == 500


def test_export_generation(tmp_path):
    from scripts.export_test import export_events
    from sqlalchemy import create_engine, text

    database = create_engine(f"sqlite:///{tmp_path / 'export.db'}")
    with database.begin() as connection:
        connection.execute(text("CREATE TABLE events (event_id TEXT, participant_id TEXT, event_type TEXT, event_timestamp TEXT, received_at TEXT, schema_version TEXT, payload TEXT)"))
        connection.execute(text("INSERT INTO events VALUES ('e1', 'synthetic', 'test', 'now', 'now', '1', '{}')"))
    output = export_events(str(database.url), tmp_path / "events.json")
    assert json.loads(output.read_text())[0]["event_id"] == "e1"

def test_an_oversized_payload_is_refused_rather_than_buffered(client, auth_headers):
    """`payload` is the one field that used to be unbounded.

    The device API key is compiled into the APK, so anyone holding the app can post 1000 records
    of arbitrary size and make the server buffer and parse the lot. The app's own payloads are
    flat maps of short strings, so this cannot fire in normal use.
    """
    oversized = record("event-oversized")
    oversized["payload"] = json.dumps({"blob": "x" * 9000})
    response = client.post("/api/v1/sync", json=request("oversized", [oversized]), headers=auth_headers)
    assert response.status_code == 422

    ordinary = client.post("/api/v1/sync", json=request("ordinary-size", [record("event-ordinary")]), headers=auth_headers)
    assert ordinary.status_code == 200


def test_a_batch_is_deduplicated_without_a_query_per_record(client, auth_headers):
    """Regression guard for the N+1: the counts must survive the set-based rewrite.

    Half the batch is already stored, so accepted and duplicate both have to be non-zero -- a
    rewrite that lost the pre-existing set would report every record as accepted.
    """
    first = [record(f"event-n1-{index}") for index in range(4)]
    assert client.post("/api/v1/sync", json=request("n1-first", first), headers=auth_headers).json()["accepted_count"] == 4

    overlapping = first[2:] + [record(f"event-n1-{index}") for index in range(4, 8)]
    body = client.post("/api/v1/sync", json=request("n1-second", overlapping), headers=auth_headers).json()
    assert body["accepted_count"] == 4
    assert body["duplicate_count"] == 2


def test_development_shortcuts_are_refused_outside_development():
    """Findings 7 and 9: the two settings that fail silently.

    `auth_enabled=False` makes `require_api_key` return early, so it disables the device key
    *and* the operator session check together. Neither this nor the shipped default API key
    shows up at runtime -- the server starts, /health says ok, and the dataset is public.
    """
    from api.app.config import SHIPPED_DEFAULT_API_KEY, verify_deployment_is_safe

    def check(**overrides):
        base = {"environment": "production", "auth_enabled": True, "development_api_key": "a-real-secret"}
        return verify_deployment_is_safe(Settings(**(base | overrides)))

    assert check() is None, "a properly configured production deployment must start"

    for overrides in ({"auth_enabled": False}, {"development_api_key": SHIPPED_DEFAULT_API_KEY}):
        try:
            check(**overrides)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"{overrides} must refuse to start outside development")

    # Development is exactly where both are allowed, which is why the guard keys on it.
    assert verify_deployment_is_safe(
        Settings(environment="development", auth_enabled=False, development_api_key=SHIPPED_DEFAULT_API_KEY)
    ) is None
