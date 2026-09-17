"""Is the study data actually arriving? Reads PostgreSQL directly.

Deliberately does NOT go through the API, so it answers the question even when the backend is
switched off - which, for a server that only runs during office hours, is most of the time.

    uv run python scripts/show_data.py           # summary
    uv run python scripts/show_data.py --events  # plus the last 20 events

The columns that matter during a study are the per-participant ones: `events` says whether
someone is using the app at all, and `last seen` says whether they have stopped. A participant
whose last event is four days old is the thing you want to notice early, not at the end.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

# Importable when run as `python scripts/show_data.py` from the repository root, matching
# scripts/manage_users.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from api.app.database import engine  # noqa: E402


def _ago(when) -> str:
    if when is None:
        return "never"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} hours ago"
    return f"{int(seconds // 86400)} days ago"


def main() -> int:
    try:
        connection = engine.connect()
    except Exception as error:  # noqa: BLE001 - printing the cause is the whole point
        print(f"Cannot reach PostgreSQL: {type(error).__name__}")
        print(str(error).splitlines()[0][:180])
        print("\nIs the postgresql-x64-17 service running? Is DATABASE_URL right in .env?")
        print("A password with '@' in it must be percent-encoded as %40.")
        return 1

    with connection:
        print(f"database : {engine.url.host}:{engine.url.port}/{engine.url.database}\n")

        print("TOTALS")
        for table, label in (
            ("participant_accounts", "signed-up accounts"),
            ("participants", "devices seen"),
            ("events", "events collected"),
            ("sync_batches", "uploads received"),
        ):
            print(f"  {label:20} {connection.execute(text(f'SELECT count(*) FROM {table}')).scalar()}")

        rows = connection.execute(text("""
            SELECT p.participant_id, count(e.event_id) AS events, max(e.received_at) AS last_seen
            FROM participants p LEFT JOIN events e ON e.participant_id = p.participant_id
            GROUP BY p.participant_id ORDER BY last_seen DESC NULLS LAST
        """)).all()
        print(f"\nPER PARTICIPANT ({len(rows)})")
        print(f"  {'participant':30} {'events':>7}  last seen")
        for row in rows:
            print(f"  {row.participant_id[:30]:30} {row.events:>7}  {_ago(row.last_seen)}")

        by_type = connection.execute(text(
            "SELECT event_type, count(*) FROM events GROUP BY 1 ORDER BY 2 DESC"
        )).all()
        print("\nEVENT TYPES")
        for event_type, count in by_type or [("(none yet)", 0)]:
            print(f"  {event_type:28} {count}")

        newest = connection.execute(text("SELECT max(received_at) FROM events")).scalar()
        print(f"\nMost recent event: {_ago(newest)}")

        if "--events" in sys.argv:
            print("\nLAST 20 EVENTS")
            recent = connection.execute(text("""
                SELECT participant_id, event_type, payload::text, received_at
                FROM events ORDER BY received_at DESC LIMIT 20
            """)).all()
            for row in recent:
                print(f"  {str(row.received_at)[:19]}  {row.participant_id[:20]:20} "
                      f"{row.event_type[:22]:22} {(row.payload or '')[:60]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
