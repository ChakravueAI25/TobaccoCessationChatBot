from datetime import datetime

from sqlalchemy import Boolean, JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class Participant(Base):
    __tablename__ = "participants"
    participant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    study_identifier: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    app_version: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SyncBatch(Base):
    __tablename__ = "sync_batches"
    batch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    participant_id: Mapped[str] = mapped_column(ForeignKey("participants.participant_id"), index=True)
    app_version: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    record_count: Mapped[int] = mapped_column(Integer)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32))
    error_summary: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    __tablename__ = "events"
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    participant_id: Mapped[str] = mapped_column(ForeignKey("participants.participant_id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("sync_batches.batch_id"), index=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    schema_version: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)


class SimpleResearchEntity(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    participant_id: Mapped[str] = mapped_column(ForeignKey("participants.participant_id"), index=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class SessionRecord(SimpleResearchEntity):
    __tablename__ = "sessions"


class Message(SimpleResearchEntity):
    __tablename__ = "messages"
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str | None] = mapped_column(Text)


class CopingOutcome(SimpleResearchEntity):
    __tablename__ = "coping_outcomes"


class Questionnaire(SimpleResearchEntity):
    __tablename__ = "questionnaires"


class Progress(SimpleResearchEntity):
    __tablename__ = "progress"


class ModelUsage(SimpleResearchEntity):
    __tablename__ = "model_usage"


class FallbackUsage(SimpleResearchEntity):
    __tablename__ = "fallback_usage"


class AppVersion(Base):
    __tablename__ = "app_versions"
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(32))
    released_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))


class ExportRun(Base):
    __tablename__ = "export_runs"
    export_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    requested_scope: Mapped[dict] = mapped_column(JSON)
    format: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32))
    file_path: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ------------------------------------------------------------------ operator accounts


class User(Base):
    """A human operator of this service — a researcher, not a study participant.

    Deliberately separate from `Participant`. Spec 18 keeps participant identity apart from
    research data, and a participant never signs in here: the Android app authenticates as a
    *device* with `X-API-Key` so that spec 04 freeze item 2 holds and the app keeps working with
    this server switched off.

    `password_hash` holds the self-describing scrypt record from `security.hash_password`, never
    a raw or single-pass-hashed password.
    """

    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(32), primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    # One role today. Present so a future read-only reviewer account does not need a migration
    # to exist, and so the column is in the audit trail from the start.
    role: Mapped[str] = mapped_column(String(32), default="operator")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ParticipantBackup(Base):
    """A participant's own data, so a reinstall does not lose it.

    **Not research data, and deliberately not reachable from the research path.** It holds the
    profile, the remembered onboarding answers and the conversation - the last of which is free
    text, which spec 16 §58 already forbids the analytics from touching. `services/export.py`
    names the tables it reads and this is not one of them.

    Together with `participant_accounts` this is the set that has to be dropped for spec 18 §68
    de-identification. Drop both and the events remain a valid pseudonymous dataset.

    One row per participant, replaced in place. No history: this is a restore point rather than an
    audit trail, and keeping every version of a conversation would multiply the most sensitive
    data here for no stated purpose.
    """

    __tablename__ = "participant_backups"
    participant_id: Mapped[str] = mapped_column(
        ForeignKey("participants.participant_id", ondelete="CASCADE"), primary_key=True
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    # The device's clock when it built this. Separate from `updated_at`, the server's clock, so a
    # phone with a wrong clock cannot look like the newest writer forever.
    device_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LoginAttempt(Base):
    """Every login attempt, successful or not.

    Two jobs. It is the input to the rate limit — attempts are counted per username and per
    client address inside a rolling window — and it is the audit trail for spec 21 §67 security
    testing, which needs evidence of what was tried.

    Rows are kept rather than deleted on success: "three failures then a success" is the pattern
    worth being able to see afterwards.
    """

    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Not a foreign key: an attempt against a username that does not exist is exactly the
    # attempt most worth recording, and an FK would reject it.
    username: Mapped[str] = mapped_column(String(64), index=True)
    client_address: Mapped[str] = mapped_column(String(64), index=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AuthSession(Base):
    """A live operator session.

    Stores `token_fingerprint` (SHA-256 of the token), never the token, so a database dump
    cannot be replayed as a signed-in browser. `revoked_at` makes logout immediate rather than
    advisory, which a self-contained JWT could not.
    """

    __tablename__ = "auth_sessions"
    token_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(ForeignKey("users.username"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_address: Mapped[str | None] = mapped_column(String(64))


class ParticipantAccount(Base):
    """A participant's sign-in credential.

    Deliberately a separate table from `Participant`, not extra columns on it. Spec 18 keeps
    participant identity apart from research data, and `Participant` is joined to every event —
    putting a password hash on that row would drag the credential into every research query and
    every export.

    The join between a username and the research dataset exists only here. Drop this table and
    the events remain a valid pseudonymous dataset with no way back to a person, which is what
    spec 18 §68's de-identification step is for.
    """

    __tablename__ = "participant_accounts"
    username: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Nullable because closing an account severs this join while keeping the username row, which
    # is what blocks the name from being taken again. See `closed_at`.
    participant_id: Mapped[str | None] = mapped_column(
        ForeignKey("participants.participant_id"), unique=True, index=True, nullable=True
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Set when the participant requested deletion. A closed row is a **tombstone**: the username
    #: stays taken so it cannot be signed up again, and everything that made it an account is
    #: gone - `participant_id` nulled, `display_name` nulled, `password_hash` replaced with a
    #: value no password can produce.
    #:
    #: Checked explicitly on the login path rather than relying on the unusable hash. Depending
    #: on a malformed string to fail a comparison works today and is invisible to the next person
    #: reading it; a named column says what was intended.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_closed(self) -> bool:
        return self.closed_at is not None
