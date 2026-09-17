"""Participant sign-in credentials.

A separate table from `participants`, not extra columns on it: spec 18 keeps participant
identity apart from research data, and `participants` is joined to every event. A password hash
on that row would be dragged into every research query and every export.

Dropping this one table leaves the events a valid pseudonymous dataset with no route back to a
person, which is exactly what spec 18 §68's de-identification step wants to be possible.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_participant_accounts"
down_revision = "0002_operator_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "participant_accounts",
        sa.Column("username", sa.String(32), primary_key=True),
        # Unique: one account per participant record, so a login can never fan out to two
        # identities in the dataset.
        sa.Column(
            "participant_id",
            sa.String(128),
            sa.ForeignKey("participants.participant_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("display_name", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("password_changed_at", sa.DateTime(timezone=True)),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_participant_accounts_participant_id", "participant_accounts", ["participant_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_participant_accounts_participant_id", table_name="participant_accounts")
    op.drop_table("participant_accounts")
