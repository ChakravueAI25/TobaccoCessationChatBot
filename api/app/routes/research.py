"""Read and export endpoints for the research dataset.

Spec 23 §6 lists "provide controlled export and backup operations" as a backend
responsibility, and §11 fixes the formats. These are the endpoints the research team uses to
see what has synchronised and to pull the study dataset out.

Every route here is read-only against the research tables. The only writes are rows in
`export_runs`, which record what was exported and when.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..dependencies import rate_limit, require_api_key
from ..models import Event, ExportRun
from ..schemas import (
    EventPage,
    EventRecord,
    ExportRequest,
    ExportRunResponse,
    ParticipantSummary,
    StatsResponse,
)
from ..services import export as export_service

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key), Depends(rate_limit)])


@router.get("/stats", response_model=StatsResponse)
def stats(db: Session = Depends(get_db)) -> StatsResponse:
    return StatsResponse(**export_service.dataset_stats(db))


@router.get("/participants", response_model=list[ParticipantSummary])
def participants(db: Session = Depends(get_db)) -> list[ParticipantSummary]:
    return [ParticipantSummary(**row) for row in export_service.participant_summary(db)]


@router.get("/events", response_model=EventPage)
def events(
    db: Session = Depends(get_db),
    participant_id: str | None = None,
    event_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> EventPage:
    """Newest first — the research team reads this to check a device is syncing."""
    statement = select(Event).order_by(Event.event_timestamp.desc(), Event.event_id.desc())
    if participant_id:
        statement = statement.where(Event.participant_id == participant_id)
    if event_type:
        statement = statement.where(Event.event_type == event_type)

    # One extra row tells us whether another page exists without a second COUNT query.
    rows = list(db.scalars(statement.offset(offset).limit(limit + 1)))
    has_more = len(rows) > limit

    return EventPage(
        items=[
            EventRecord(
                event_id=event.event_id,
                participant_id=event.participant_id,
                session_id=event.session_id,
                batch_id=event.batch_id,
                event_type=event.event_type,
                event_timestamp=event.event_timestamp,
                created_at=event.created_at,
                received_at=event.received_at,
                schema_version=event.schema_version,
                payload=event.payload,
            )
            for event in rows[:limit]
        ],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.post("/exports", response_model=ExportRunResponse, status_code=status.HTTP_201_CREATED)
def create_export(
    request: ExportRequest,
    db: Session = Depends(get_db),
) -> ExportRunResponse:
    """Writes an export file and records the run."""
    settings = get_settings()
    scope = export_service.ExportScope(
        participant_id=request.participant_id,
        event_type=request.event_type,
        since=request.since,
        until=request.until,
    )

    try:
        path, row_count = export_service.run_export(
            db=db,
            export_directory=Path(settings.export_directory),
            export_format=request.format,
            scope=scope,
        )
    except OSError as error:
        # Disk full or a bad EXPORT_DIRECTORY. Recorded as failed rather than raised as a
        # 500, so the run is visible in the export history instead of vanishing.
        run = ExportRun(
            export_id=str(uuid.uuid4()),
            requested_scope=scope.as_dict(),
            format=request.format,
            status="failed",
            file_path=None,
            schema_version=SCHEMA_VERSION,
        )
        db.add(run)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail=f"Export could not be written: {error}",
        ) from None

    run = ExportRun(
        export_id=str(uuid.uuid4()),
        requested_scope=scope.as_dict() | {"row_count": row_count},
        format=request.format,
        status="completed",
        file_path=str(path),
        schema_version=SCHEMA_VERSION,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    return _to_response(run, row_count)


@router.get("/exports", response_model=list[ExportRunResponse])
def list_exports(db: Session = Depends(get_db)) -> list[ExportRunResponse]:
    runs = db.scalars(select(ExportRun).order_by(ExportRun.created_at.desc())).all()
    return [_to_response(run, run.requested_scope.get("row_count")) for run in runs]


@router.get("/exports/{export_id}/download")
def download_export(export_id: str, db: Session = Depends(get_db)) -> FileResponse:
    run = db.get(ExportRun, export_id)
    if run is None or not run.file_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found")

    path = Path(run.file_path)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Export file is no longer on disk",
        )

    # Confine the download to the configured export directory. Without this an export_runs
    # row with a doctored file_path would turn this route into an arbitrary file read.
    export_root = Path(get_settings().export_directory).resolve()
    if export_root not in path.resolve().parents:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Export path not permitted")

    return FileResponse(path=path, filename=path.name, media_type=_MEDIA_TYPES[run.format])


# What "a quick look" is allowed to cost. Generous enough that a study of this size never
# reaches it, small enough that the whole table can never be read into one response body.
QUICK_LOOK_ROW_LIMIT = 50_000


@router.get("/events.csv")
def events_csv(
    db: Session = Depends(get_db),
    participant_id: str | None = None,
    event_type: str | None = None,
) -> Response:
    """Direct CSV download with no export run recorded — for a quick look, not for the study.

    Refuses rather than truncates. A short CSV that looks complete is the failure that ends up
    in an analysis, so past the cap this points at `POST /exports`, which is uncapped precisely
    because it is the one that has to be complete.
    """
    scope = export_service.ExportScope(participant_id=participant_id, event_type=event_type)

    # One past the cap, so reaching it is detectable rather than indistinguishable from a
    # dataset that happens to be exactly that size.
    rows = export_service.event_rows(db, scope, limit=QUICK_LOOK_ROW_LIMIT + 1)
    if len(rows) > QUICK_LOOK_ROW_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"More than {QUICK_LOOK_ROW_LIMIT} rows match. Narrow the filters, or use "
                "POST /api/v1/exports for the full dataset."
            ),
        )
    body = export_service.csv_bytes(rows, export_service.EVENT_COLUMNS)
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="events.csv"'},
    )


def _to_response(run: ExportRun, row_count: int | None) -> ExportRunResponse:
    return ExportRunResponse(
        export_id=run.export_id,
        format=run.format,
        status=run.status,
        file_path=run.file_path,
        row_count=row_count,
        requested_scope=run.requested_scope,
        created_at=run.created_at,
    )


SCHEMA_VERSION = "1"

_MEDIA_TYPES = {
    "csv": "text/csv",
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
