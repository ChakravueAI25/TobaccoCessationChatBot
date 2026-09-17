"""Account restore data, kept apart from the research dataset.

A participant who reinstalls the app should get their profile, their remembered answers and their
conversation back. None of that is research data, and this table exists so the two never mix.

**Why a separate table rather than columns elsewhere.** `participants` is joined to every event,
so anything on that row is dragged into every research query and every export. The same reasoning
gave `participant_accounts` its own table in 0003. This is the third thing that must stay off the
research path, and it is the most sensitive of them: it holds conversation text.

The separation is not just tidiness. Spec 18 §68 wants de-identification to be possible by
dropping the tables that join a person to the data. After this migration that set is
`participant_accounts` and `participant_backups`; drop both and the events remain a valid
pseudonymous dataset with no route back to a person.

**Nothing in `services/export.py` reads this table.** That module names `Event`, `Participant` and
`SyncBatch` explicitly rather than enumerating tables, so a new table cannot appear in an export by
accident - and `test_backups_never_reach_an_export` fails if that ever changes.
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_participant_backups"
down_revision = "0003_participant_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "participant_backups",
        # One row per participant, replaced in place. History is not kept: this is a restore
        # point, not an audit trail, and keeping every version of a conversation would multiply
        # the most sensitive data in the database for no stated purpose.
        sa.Column(
            "participant_id",
            sa.String(128),
            sa.ForeignKey("participants.participant_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # The app's own schema version for the payload shape. Stored so a future app can tell
        # whether it understands a backup written by an older one, rather than guessing.
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        # The device's clock when it built the backup. Used for last-writer-wins across two
        # devices, and deliberately separate from `updated_at`, which is the server's clock -
        # a phone with a wrong clock must not be able to look like the newest writer forever.
        sa.Column("device_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("participant_backups")
