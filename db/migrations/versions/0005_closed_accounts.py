"""Closing an account blocks the username instead of freeing it.

Asked for directly: once a participant requests deletion they must sign up again with a **new**
username, not walk back into the old name. The previous behaviour deleted the row, which freed
the username - verified end to end, where a closed `kaushik` re-registered as `kaushik` and got
a working account back. That is the opposite of what deletion is for.

So a closure is now a tombstone rather than a delete:

    username        kept  <- this is what blocks re-use; it is the primary key
    participant_id  NULL  <- severs the only join between a person and the research dataset
    display_name    NULL  <- the profile detail goes
    password_hash   sentinel, and `closed_at` set so no login path even reaches a comparison

Keeping the row does **not** weaken spec 18 s68's de-identification step. That step is the
removal of the username-to-participant_id join, and nulling `participant_id` removes exactly
that. What is left behind is a username with nothing attached to it: no id, no display name, no
usable credential. It cannot be signed into and it cannot be linked to an event.

`participant_id` therefore has to become nullable, and its UNIQUE constraint has to tolerate
NULLs - which PostgreSQL already does, treating each NULL as distinct, so any number of closed
accounts can coexist.

The block lasts "until the database is cleared", which is `scripts/reset_participants.py`
truncating the table. That is deliberate: within a study round a name is gone for good, and a
fresh round starts from nothing.
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_closed_accounts"
down_revision = "0004_participant_backups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "participant_accounts",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Nullable so a closure can sever the identity join while the username row survives.
    op.alter_column(
        "participant_accounts",
        "participant_id",
        existing_type=sa.String(length=128),
        nullable=True,
    )


def downgrade() -> None:
    # A closed account has no participant_id, so it cannot satisfy a NOT NULL column. Removing
    # those rows is the only honest downgrade - and it restores the pre-0005 behaviour exactly,
    # where a closed account had no row at all.
    op.execute("DELETE FROM participant_accounts WHERE participant_id IS NULL")
    op.alter_column(
        "participant_accounts",
        "participant_id",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.drop_column("participant_accounts", "closed_at")
