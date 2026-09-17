from datetime import datetime
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResearchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=128)
    participant_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    session_id: str | None = Field(default=None, max_length=128)
    # Bounded like every other field. The app only ever writes a flat map of short strings
    # (spec 16 §58 forbids anything derived from message text), so this is ~40x headroom;
    # it is here because the device API key is extractable from the APK and an unbounded
    # field × 1000 records is a body the server buffers and parses in full.
    payload: str = Field(min_length=2, max_length=8192)
    schema_version: str = Field(min_length=1, max_length=32)
    created_at: datetime

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "1":
            raise ValueError("unsupported schema_version")
        return value

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: str) -> str:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("payload must be valid JSON text") from error
        if not isinstance(decoded, dict):
            raise ValueError("payload must encode a JSON object")
        return value


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str = Field(min_length=1, max_length=128)
    participant_id: str = Field(min_length=1, max_length=128)
    app_version: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(min_length=1, max_length=32)
    records: list[ResearchRecord] = Field(min_length=1, max_length=1000)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "1":
            raise ValueError("unsupported schema_version")
        return value


class SyncResponse(BaseModel):
    batch_id: str
    status: str
    accepted_count: int
    duplicate_count: int
    rejected_count: int


class HealthResponse(BaseModel):
    status: str
    database: str
    #: "ok", "unreachable", "degraded", or "not_configured".
    #:
    #: On /health deliberately, not only on /generate/health. A green backend used to say nothing
    #: about the model, so a failed model task left an API that looked perfectly healthy while
    #: every chat turn silently fell back - which is exactly how a broken model went unnoticed
    #: for a day of testing.
    model: str = "not_configured"

# --------------------------------------------------------------- research read/export


class ParticipantSummary(BaseModel):
    """One row of the participants view, with the counts the research team asks for."""

    participant_id: str
    study_identifier: str
    app_version: str | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    event_count: int
    batch_count: int
    last_event_at: datetime | None


class EventRecord(BaseModel):
    event_id: str
    participant_id: str
    session_id: str | None
    batch_id: str
    event_type: str
    event_timestamp: datetime
    created_at: datetime
    received_at: datetime | None
    schema_version: str
    payload: Any


class EventPage(BaseModel):
    items: list[EventRecord]
    limit: int
    offset: int
    has_more: bool


class StatsResponse(BaseModel):
    participants: int
    events: int
    batches: int
    events_by_type: dict[str, int]
    latest_event_at: datetime | None
    latest_sync_at: datetime | None


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str = Field(default="csv")
    participant_id: str | None = Field(default=None, max_length=128)
    event_type: str | None = Field(default=None, max_length=128)
    since: datetime | None = None
    until: datetime | None = None

    @field_validator("format")
    @classmethod
    def validate_format(cls, value: str) -> str:
        allowed = ("csv", "json", "xlsx")
        if value not in allowed:
            raise ValueError(f"format must be one of {allowed}")
        return value


class ExportRunResponse(BaseModel):
    export_id: str
    format: str
    status: str
    file_path: str | None
    row_count: int | None
    requested_scope: dict[str, Any]
    created_at: datetime


# --------------------------------------------------------------------- model inference


class GenerateRequest(BaseModel):
    """A finished prompt from the app.

    The app builds this: spec 10 §41 keeps every decision - safety, case, intervention, coping -
    on the device, and the model only words a conclusion already reached. Nothing here describes
    a behavioural case, and this endpoint must never learn what one is.
    """

    model_config = ConfigDict(extra="forbid")
    # Bounded like every other field. The context is 2048 tokens and the app's prompt runs to
    # roughly 450, so this is ample; it exists because the device API key is extractable from
    # the APK and an unbounded field is a body the server buffers and parses in full.
    prompt: str = Field(min_length=1, max_length=32_000)
    max_tokens: int = Field(default=120, ge=1, le=512)
    temperature: float = Field(default=0.75, ge=0.0, le=2.0)
    top_p: float = Field(default=0.8, ge=0.0, le=1.0)
    top_k: int = Field(default=20, ge=0, le=200)
    stop_sequences: list[str] = Field(default_factory=list, max_length=8)


class GenerateResponse(BaseModel):
    text: str
    generation_ms: int
    token_count: int | None = None
    model: str | None = None
