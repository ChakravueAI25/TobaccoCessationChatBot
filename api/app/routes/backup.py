"""Account restore: the participant's own data, kept apart from the research dataset.

A participant who reinstalls should get their profile, their remembered answers and their
conversation back. This is the server half of that.

## The asymmetry, which is the whole security design

**Writing needs the device API key. Reading needs the participant's password.**

That is deliberate and not an oversight. The device key is extractable from the APK - it is
`local.properties` compiled in - so anything guarded only by it is guarded by nothing against
someone who has the app. Research events are already written with it, and a bad actor writing
junk events is a data-quality problem.

A *read* is a different thing entirely. `GET /backup/{participant_id}` behind the device key would
let anyone with the APK download any participant's conversation, given only an id. That is a
disclosure of the most sensitive data in the study, and it is why restore lives on the login
route (`participants.py`) behind username and password rather than here.

Writing is left on the device key for consistency with `/sync`: the device has no participant
credential cached - spec 04 freeze item 2 forbids storing one - so requiring a password to upload
would mean the backup could only ever be written at the moment of login, which is the one moment
it has nothing new to say.

## What this is not

Not research. Nothing here is exported, aggregated or analysed, and `services/export.py` does not
read the table. The consent gate governs research collection; this is a service feature, and the
privacy screen says so in the participant's own words.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import rate_limit, require_api_key
from ..models import Participant, ParticipantBackup

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key), Depends(rate_limit)])

# Enough for a four-week study's conversation with room to spare, and small enough that a
# misbehaving device cannot fill the disk. Measured against a full month of the heaviest
# realistic use, which came to well under a megabyte.
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024


class BackupUpload(BaseModel):
    participant_id: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=32)
    # The device's clock when it assembled this.
    device_updated_at: datetime
    # Opaque to the server. The app owns this shape; the server stores and returns it and never
    # reads inside it, which is what keeps this route out of the research path by construction.
    payload: dict


class BackupAck(BaseModel):
    stored: bool
    # Echoed so the device can tell a store from a no-op without a second call.
    device_updated_at: datetime


@router.put("/backup", response_model=BackupAck)
def upload_backup(upload: BackupUpload, db: Session = Depends(get_db)) -> BackupAck:
    encoded = json.dumps(upload.payload, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Backup payload too large",
        )

    # The participant row must exist. Creating one here would let this route enrol participants,
    # which is `/participants/register`'s job and is already the one open security finding.
    if db.get(Participant, upload.participant_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown participant",
        )

    incoming = _as_utc(upload.device_updated_at)
    existing = db.get(ParticipantBackup, upload.participant_id)

    if existing is None:
        db.add(
            ParticipantBackup(
                participant_id=upload.participant_id,
                schema_version=upload.schema_version,
                payload=upload.payload,
                device_updated_at=incoming,
            )
        )
        db.commit()
        return BackupAck(stored=True, device_updated_at=incoming)

    # Last writer wins, by the *device's* clock. Two phones signed into one account is a real
    # possibility (login returns the same participant_id), and without this the loser would be
    # whichever happened to sync second rather than whichever was actually newer.
    if _as_utc(existing.device_updated_at) > incoming:
        return BackupAck(stored=False, device_updated_at=_as_utc(existing.device_updated_at))

    existing.schema_version = upload.schema_version
    existing.payload = upload.payload
    existing.device_updated_at = incoming
    db.commit()
    return BackupAck(stored=True, device_updated_at=incoming)


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; PostgreSQL hands back aware ones.

    Comparing the two raises, and the tests run on SQLite while production runs on PostgreSQL -
    which is exactly the shape of the three defects connecting PostgreSQL found the first time.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class BackupDeleted(BaseModel):
    deleted: bool


@router.delete("/backup", response_model=BackupDeleted)
def delete_backup(participant_id: str, db: Session = Depends(get_db)) -> BackupDeleted:
    """Removes the participant's stored copy of their own account.

    **This is required for local deletion to mean anything.** Before the backup existed, "delete
    my data" cleared the phone and that was the whole story. Now a copy lives here, so clearing
    the phone alone would leave the profile and the conversation on the server - and the next
    sign-in would put them straight back. A deletion that undoes itself is worse than none,
    because the participant believes it worked.

    Research events are deliberately **not** touched. They are pseudonymous, they are the study's
    dataset rather than the participant's own copy, and removing rows mid-study would silently
    change results that other participants' data is part of. The consent record is not touched
    either - it is the evidence the participant asked, and erasing it would destroy the audit
    trail that the request happened at all.

    Guarded by the device API key, like the upload. That is consistent rather than lax: an upload
    already replaces the stored document wholesale, so anyone able to destroy a backup this way
    could already do it by uploading an empty one. The asymmetry that matters is on *reads*,
    which need the participant's password and have no route here at all.
    """
    existing = db.get(ParticipantBackup, participant_id)
    if existing is None:
        # Already gone. Reported as success: the caller asked for it to not exist, and it does
        # not - a 404 here would make the app show a failure for the outcome it wanted.
        return BackupDeleted(deleted=False)

    db.delete(existing)
    db.commit()
    return BackupDeleted(deleted=True)
