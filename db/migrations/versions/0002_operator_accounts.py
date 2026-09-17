"""Operator accounts, login throttling and sessions.

Adds the three tables behind `POST /api/v1/auth/login`. Nothing in the research schema changes:
participants and events are untouched, because a participant never signs in here — the Android
app authenticates as a device with `X-API-Key` so that spec 04 freeze item 2 holds.

Also adds the indexes migration 0001 declared on the models but never created, which is why the
export and participant queries were doing sequential scans.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_operator_accounts"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("username", sa.String(32), primary_key=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("display_name", sa.String(128)),
        sa.Column("role", sa.String(32), nullable=False, server_default="operator"),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("password_changed_at", sa.DateTime(timezone=True)),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer, primary_key=True),
        # No foreign key on purpose: an attempt against a username that does not exist is the
        # attempt most worth recording, and an FK would reject exactly that row.
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("client_address", sa.String(64), nullable=False),
        sa.Column("succeeded", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("attempted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # The rate limit query filters on all three, and it runs before every password hash — so it
    # is the one query on this service that must stay fast under attack.
    op.create_index("ix_login_attempts_username", "login_attempts", ["username"])
    op.create_index("ix_login_attempts_client_address", "login_attempts", ["client_address"])
    op.create_index("ix_login_attempts_attempted_at", "login_attempts", ["attempted_at"])

    op.create_table(
        "auth_sessions",
        # The SHA-256 of the token, never the token: a database dump must not be replayable as
        # a signed-in browser.
        sa.Column("token_fingerprint", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(32), sa.ForeignKey("users.username"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("client_address", sa.String(64)),
    )
    op.create_index("ix_auth_sessions_username", "auth_sessions", ["username"])

    # Declared as index=True on the models in 0001 but never created there, so every
    # /events page and every export was a full scan. Fine at one participant, not at forty.
    op.create_index("ix_events_participant_id", "events", ["participant_id"])
    op.create_index("ix_events_batch_id", "events", ["batch_id"])
    op.create_index("ix_events_event_type", "events", ["event_type"])
    op.create_index("ix_events_session_id", "events", ["session_id"])
    op.create_index("ix_sync_batches_participant_id", "sync_batches", ["participant_id"])


def downgrade() -> None:
    op.drop_index("ix_sync_batches_participant_id", table_name="sync_batches")
    op.drop_index("ix_events_session_id", table_name="events")
    op.drop_index("ix_events_event_type", table_name="events")
    op.drop_index("ix_events_batch_id", table_name="events")
    op.drop_index("ix_events_participant_id", table_name="events")

    op.drop_index("ix_auth_sessions_username", table_name="auth_sessions")
    op.drop_table("auth_sessions")

    op.drop_index("ix_login_attempts_attempted_at", table_name="login_attempts")
    op.drop_index("ix_login_attempts_client_address", table_name="login_attempts")
    op.drop_index("ix_login_attempts_username", table_name="login_attempts")
    op.drop_table("login_attempts")

    op.drop_table("users")
