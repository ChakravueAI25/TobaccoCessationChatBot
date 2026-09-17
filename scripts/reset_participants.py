"""Clears every participant row so the study can be tested from a clean slate.

Deliberately truncates rather than dropping: the schema and the migration history stay intact,
so `alembic upgrade head` is not needed afterwards and the next run starts on the same schema
version the app expects.

    uv run python scripts/reset_participants.py          # shows what it WOULD remove
    uv run python scripts/reset_participants.py --yes     # actually removes it

**--yes is required, and it was added the hard way.** This script used to truncate the moment it
was invoked, with no dry run and no confirmation. Somebody ran it expecting to be shown a summary
and destroyed a participant account that was mid-test. The `Clear all participant data` VS Code
task points here, so a single click was one keystroke away from the same thing.

It is a DEVELOPMENT reset. Once real participants are enrolled their events are the study, and the
narrower supported operations are `DELETE /api/v1/backup` for one participant's own copy and
`DELETE /api/v1/participants/account` for one credential - neither touches anybody else.
"""
import sys

sys.path.insert(0, ".")
from sqlalchemy import text  # noqa: E402

from api.app.database import engine  # noqa: E402

TABLES = [
    "events",
    # Named rather than left to the CASCADE from `participants`. It would be cleared either way,
    # but a reset script that does not list the table holding conversation text is a script
    # nobody can audit by reading it.
    "participant_backups",
    "sync_batches",
    "questionnaires",
    # Spec 21 §67 security evidence, kept during normal operation and deliberately NOT removed
    # by a participant's own deletion request - but a reset between test rounds is a different
    # thing, and leaving stale lockout rows behind makes the next round's first login flaky.
    "login_attempts",
    # Clearing this is what releases usernames blocked by a closed account. Within a study round
    # a deleted name stays blocked on purpose; a fresh round starts from nothing.
    "participant_accounts",
    "participants",
]

confirmed = "--yes" in sys.argv[1:]

with engine.begin() as conn:
    before = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES}
    usernames = [
        row[0]
        for row in conn.execute(text("SELECT username FROM participant_accounts ORDER BY username"))
    ]

print("This will permanently delete:" if confirmed else "This WOULD permanently delete:")
for table in TABLES:
    print(f"  {table:24} {before[table]:>6} rows")
if usernames:
    print("  accounts: " + ", ".join(usernames))

if not any(before.values()):
    print("\nNothing to do - already empty.")
    sys.exit(0)

if not confirmed:
    print("\nNothing has been changed. Re-run with --yes to actually do it.")
    sys.exit(1)

with engine.begin() as conn:
    # One statement so foreign keys never see a half-empty database, and RESTART IDENTITY so
    # the next participant does not inherit a serial in the thousands.
    conn.execute(text("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"))
    after = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES}

print()
for table in TABLES:
    print(f"  cleared {table:24} {before[table]:>6} -> {after[table]}")
print("operator accounts left alone - they are not participant data")
