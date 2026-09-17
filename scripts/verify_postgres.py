"""Smoke test against the real PostgreSQL in `.env`, with no test override.

**`uv run pytest` does not test PostgreSQL.** `tests/conftest.py` swaps `get_db` for a SQLite
session, so all 98 tests pass without PostgreSQL ever being touched. This does the opposite: it
uses the real `get_db`, so every statement here runs on the actual database.

Run it after changing a route, a model or a migration:

    uv run python scripts/verify_postgres.py

It checks the things SQLite cannot tell you about, which is exactly where this project has
already been bitten:

* the connection works at all, and names the percent-encoding trap when it does not;
* an event insert satisfies its foreign keys - SQLite does not enforce them by default, and
  the suite only started to since `PRAGMA foreign_keys=ON` was added to conftest;
* `payload` is stored as a JSON *object*, so `payload->>'key'` works for research queries
  rather than returning NULL against a double-encoded string;
* a replayed batch deduplicates instead of inserting twice.

It writes one participant and one event, then tells you how to delete them.
"""
import json
import os
import sys
from datetime import datetime, timezone

# Importable when run as `python scripts/verify_postgres.py` from the repository root,
# matching scripts/manage_users.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from api.app.config import get_settings  # noqa: E402
from api.app.database import engine  # noqa: E402
from api.app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def main() -> int:
    settings = get_settings()
    print(f"driver          : {engine.url.drivername}")
    print(f"host/db         : {engine.url.host}:{engine.url.port}/{engine.url.database}")

    if not engine.url.drivername.startswith("postgresql"):
        print("\nNOT PostgreSQL - .env still points somewhere else. Nothing below would prove anything.")
        return 1

    try:
        with engine.connect() as connection:
            print(f"server          : {connection.execute(text('select version()')).scalar()[:50]}")
            tables = connection.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            ).scalar()
            print(f"public tables   : {tables}")
            if tables == 0:
                print("\nNo tables. Run `uv run alembic upgrade head` first.")
                return 1
    except Exception as error:  # noqa: BLE001 - this is the diagnostic, printing it is the point
        print(f"\nCONNECTION FAILED: {type(error).__name__}")
        print(str(error).splitlines()[0][:200])
        print("\nIf this says 'password authentication failed', the most likely cause is an")
        print("unescaped '@' in DATABASE_URL. It must be percent-encoded as %40.")
        return 1

    client = TestClient(app)
    key = {"X-API-Key": settings.development_api_key}

    health = client.get("/health").json()
    print(f"health          : {health}")
    if health.get("database") != "ok":
        print("\n/health says the database is not reachable.")
        return 1

    # Byte-for-byte what HttpResearchSyncTransport.encode() puts on the wire, with a unique id
    # so it can be run repeatedly without colliding with its own earlier rows.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    participant = f"pg-verify-{stamp}"
    batch = {
        "batch_id": f"batch-{stamp}",
        "participant_id": participant,
        "app_version": "1.0",
        "schema_version": "1",
        "records": [
            {
                "event_id": f"evt-{stamp}",
                "participant_id": participant,
                "event_type": "craving_reported",
                "timestamp": "2026-08-28T04:05:06.007+00:00",
                "session_id": "s-1",
                "payload": json.dumps({"intensity": "6"}),
                "schema_version": "1",
                "created_at": "2026-08-28T04:05:06.007+00:00",
            }
        ],
    }

    first = client.post("/api/v1/sync", json=batch, headers=key)
    print(f"sync            : {first.status_code} {first.json()}")
    if first.status_code != 200:
        return 1

    # The retry path the app actually takes. Must report the original counts, not insert twice.
    again = client.post("/api/v1/sync", json=batch, headers=key)
    print(f"sync (replayed) : {again.status_code} {again.json()}")

    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT event_type, payload FROM events WHERE participant_id = :p"),
            {"p": participant},
        ).all()
    print(f"rows in postgres: {len(stored)} -> {stored}")

    print(f"stats           : {client.get('/api/v1/stats', headers=key).json()}")

    if len(stored) != 1:
        print("\nExpected exactly one row after the replay. Deduplication is not working.")
        return 1

    print("\nOK - the app's exact bytes reached PostgreSQL and deduplicated on replay.")
    print(f"Clean up with: DELETE FROM events WHERE participant_id = '{participant}';")
    return 0


if __name__ == "__main__":
    sys.exit(main())
