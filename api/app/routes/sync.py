import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import rate_limit, require_api_key
from ..models import Event, Participant, SyncBatch
from ..schemas import SyncRequest, SyncResponse

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key), Depends(rate_limit)])


@router.post("/sync", response_model=SyncResponse)
def sync(request: SyncRequest, db: Session = Depends(get_db)) -> SyncResponse:
    try:
        participant = db.get(Participant, request.participant_id)
        if participant is None:
            participant = Participant(participant_id=request.participant_id, study_identifier=request.participant_id, app_version=request.app_version)
            db.add(participant)
            # Flushed now, not left to commit. `events` and `sync_batches` both carry a foreign
            # key to this row, but the models declare no `relationship()`, so SQLAlchemy has no
            # mapper-level dependency to sort on and flushes in alphabetical mapper order --
            # Event before Participant. PostgreSQL then rejects the event for a parent that has
            # not been written yet.
            #
            # This was invisible for the entire project because SQLite does not enforce foreign
            # keys unless `PRAGMA foreign_keys=ON`, which the test suite never sets. Every green
            # test to date inserted events with no parent row and did not care.
            db.flush()
        elif participant.study_identifier != request.participant_id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid participant")
        else:
            # Refresh what the participants view reports. `last_seen_at` has onupdate=now(),
            # but that only fires when a column actually changes — without touching the row a
            # device that syncs daily would show its first-seen time forever, which is exactly
            # the column the research team uses to spot a device that has stopped reporting.
            participant.app_version = request.app_version
            participant.last_seen_at = func.now()

        existing_batch = db.get(SyncBatch, request.batch_id)
        if existing_batch is not None:
            if existing_batch.participant_id != request.participant_id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="batch_id belongs to another participant")
            return SyncResponse(batch_id=existing_batch.batch_id, status=existing_batch.status,
                                accepted_count=existing_batch.accepted_count, duplicate_count=existing_batch.duplicate_count,
                                rejected_count=existing_batch.rejected_count)

        event_ids = [record.event_id for record in request.records]
        if len(event_ids) != len(set(event_ids)):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Duplicate event_id in request")
        if any(record.participant_id != request.participant_id for record in request.records):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Record participant does not match batch participant")

        # One query for the whole batch, not two per record. The schema allows 1000 records, so
        # the previous `db.get` per record twice over was 2000 round trips — slow enough on a
        # laptop Postgres to trip the app's 20 s read timeout, which the app then reads as
        # *unreachable* and retries, producing the same 2000 queries again.
        already_stored = set(db.scalars(select(Event.event_id).where(Event.event_id.in_(event_ids))))
        batch = SyncBatch(batch_id=request.batch_id, participant_id=request.participant_id, app_version=request.app_version,
                          schema_version=request.schema_version, record_count=len(request.records), accepted_count=0,
                          duplicate_count=len(already_stored), rejected_count=0, status="received")
        db.add(batch)
        # Same reason: `events.batch_id` references this row, and Event sorts before SyncBatch.
        db.flush()

        for record in request.records:
            if record.event_id not in already_stored:
                db.add(Event(event_id=record.event_id, participant_id=request.participant_id, batch_id=request.batch_id,
                             event_type=record.event_type, session_id=record.session_id, event_timestamp=record.timestamp,
                             created_at=record.created_at, schema_version=record.schema_version,
                             # Decoded, not the raw text. The column is JSON, so handing it a
                             # str stored a JSON *string* containing JSON -- `json_typeof` said
                             # `string`, and `payload->>'trigger'` returned NULL for every
                             # research query anyone would actually write. Pydantic has already
                             # proved this parses to an object.
                             payload=json.loads(record.payload)))
                batch.accepted_count += 1
        batch.status = "accepted"
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except (OperationalError, IntegrityError):
        # Only the database being unreachable, locked, or racing another batch on the same
        # event_id. Both clear on their own, and 503 is the one status the app is right to
        # retry indefinitely.
        #
        # A broad `except Exception` used to live here, which turned every server-side bug into
        # the same 503. The app reads 5xx as retryable and always retries the *head* of its
        # queue, so one unstorable record silently stopped that participant uploading anything
        # ever again. Anything not listed above is a bug: let it surface as a 500 with a
        # traceback in the log, where it can be seen and fixed.
        db.rollback()
        raise HTTPException(status_code=503, detail="Sync batch could not be stored") from None
    return SyncResponse(batch_id=request.batch_id, status="accepted", accepted_count=batch.accepted_count,
                        duplicate_count=batch.duplicate_count, rejected_count=0)