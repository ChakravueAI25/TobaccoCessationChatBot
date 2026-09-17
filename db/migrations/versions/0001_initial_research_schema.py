"""Create the central Quit Smoke research schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("participants", sa.Column("participant_id", sa.String(128), primary_key=True), sa.Column("study_identifier", sa.String(128), nullable=False, unique=True), sa.Column("app_version", sa.String(64)), sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.create_table("sync_batches", sa.Column("batch_id", sa.String(128), primary_key=True), sa.Column("participant_id", sa.String(128), sa.ForeignKey("participants.participant_id"), nullable=False), sa.Column("app_version", sa.String(64), nullable=False), sa.Column("schema_version", sa.String(32), nullable=False), sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("record_count", sa.Integer, nullable=False), sa.Column("accepted_count", sa.Integer, nullable=False), sa.Column("duplicate_count", sa.Integer, nullable=False), sa.Column("rejected_count", sa.Integer, nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("error_summary", sa.Text))
    op.create_table("events", sa.Column("event_id", sa.String(128), primary_key=True), sa.Column("participant_id", sa.String(128), sa.ForeignKey("participants.participant_id"), nullable=False), sa.Column("batch_id", sa.String(128), sa.ForeignKey("sync_batches.batch_id"), nullable=False), sa.Column("event_type", sa.String(128), nullable=False), sa.Column("session_id", sa.String(128)), sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("schema_version", sa.String(32), nullable=False), sa.Column("payload", sa.JSON, nullable=False))
    for table in ("sessions", "coping_outcomes", "questionnaires", "progress", "model_usage", "fallback_usage"):
        op.create_table(table, sa.Column("id", sa.Integer, primary_key=True), sa.Column("participant_id", sa.String(128), sa.ForeignKey("participants.participant_id"), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("data", sa.JSON, nullable=False))
    op.create_table("messages", sa.Column("id", sa.Integer, primary_key=True), sa.Column("participant_id", sa.String(128), sa.ForeignKey("participants.participant_id"), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("data", sa.JSON, nullable=False), sa.Column("role", sa.String(32), nullable=False), sa.Column("content", sa.Text))
    op.create_table("app_versions", sa.Column("version", sa.String(64), primary_key=True), sa.Column("schema_version", sa.String(32), nullable=False), sa.Column("released_at", sa.DateTime(timezone=True)))
    op.create_table("export_runs", sa.Column("export_id", sa.String(128), primary_key=True), sa.Column("requested_scope", sa.JSON, nullable=False), sa.Column("format", sa.String(8), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("file_path", sa.Text), sa.Column("schema_version", sa.String(32), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))


def downgrade() -> None:
    for table in ("export_runs", "app_versions", "messages", "fallback_usage", "model_usage", "progress", "questionnaires", "coping_outcomes", "sessions", "events", "sync_batches", "participants"):
        op.drop_table(table)