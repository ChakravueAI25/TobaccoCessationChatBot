"""Participant accounts.

Separate from `auth.py` on purpose. That file signs in *researchers*, who administer the study;
this one signs in *participants*, who are its subjects. Mixing them would put one credential
store in front of both the dataset and the people in it.

The shape is dictated by spec 04 freeze item 2: **the Android app must work fully with this
server switched off.** So enrolment and sign-in need the server exactly once, and everything
after that is local:

    register / login  ->  server returns a participant_id  ->  device caches it
                                                            ->  app works offline forever after

The device never re-authenticates to send data. It syncs with `X-API-Key` as it always did, and
`participant_id` is just the study identifier on each record. That keeps a lost password or a
dead server from stranding someone mid-study.

Spec 18's identity separation is why `participant_id` is a random opaque value and not derived
from the username: the research dataset is keyed on the id, the credential lives here, and the
two are only joinable through this table.
"""

from __future__ import annotations

import secrets
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_api_key
from ..models import LoginAttempt, Participant, ParticipantAccount, ParticipantBackup
from ..security import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    validate_password,
    validate_username,
    verify_password,
)

# The device key still guards these: only a build of the app can reach them, which stops the
# endpoint being an open registration form on the study's LAN.
router = APIRouter(prefix="/api/v1/participants", dependencies=[Depends(require_api_key)])

MAX_FAILURES_PER_USERNAME = 5
MAX_FAILURES_PER_ADDRESS = 20
FAILURE_WINDOW = timedelta(minutes=15)

# Account-creation throttle, per device.
#
# The device key already stops anyone but a build of the app reaching /register, and the general
# `dependencies.rate_limit` is a coarse abuse ceiling for request *volume*. Neither stops the same
# device quietly creating account after account, which is what this caps: **one device, a couple
# of accounts.** The app sends its stable per-install id in `X-Device-Id` (the same pseudonymous,
# non-hardware id it already puts on research events, so this introduces no new identifier), and
# each device gets its own small quota.
#
# **Per device, not per address, on purpose.** On shared wifi or the ngrok tunnel every phone
# collapses to one IP, so an address-keyed limit either blocks legitimate enrolment or is too loose
# to mean anything. Keyed on the device id, each participant's phone is its own bucket — so the cap
# can be tight (a participant needs one account, or two if they delete and sign up again) without
# ever catching a room full of people enrolling on their own phones. A caller with no header — i.e.
# not the app — falls back to the client address so it is still bounded.
#
# Only *successful* creations count; a mistyped or taken username does not burn the quota. A
# reinstall generates a fresh device id, so this is a speed bump against casual multi-account
# creation, not an unbreakable identity lock — that is a study-issued enrolment code (see the
# Known-bad note in the backend CLAUDE.md).
#
# In-memory and single-process for the same reason `dependencies.rate_limit` is (spec 23 §2: one
# uvicorn process on a LAN). **The ceiling to know is that it resets on restart and does not span
# workers** — if this is ever run with `--workers`, move the count to the database (a
# registration_attempts table keyed on the device id) rather than tuning these numbers.
MAX_REGISTRATIONS_PER_DEVICE = 3
REGISTRATION_WINDOW_SECONDS = 60 * 60

_registration_times: dict[str, deque[float]] = defaultdict(deque)


def reset_registration_limits() -> None:
    """Clears the window. For tests, which would otherwise leak counts between them."""
    _registration_times.clear()


def _registration_key(request: Request) -> str:
    """One device, one bucket. The app's stable per-install id, or the client address when a
    non-app caller sends no header so it is still bounded."""
    device = request.headers.get("x-device-id")
    if device:
        return "device:" + device.strip()[:128]
    return "address:" + _client_address(request)


def _registration_limit_reached(key: str) -> bool:
    """Whether [key] has created its allowed accounts inside the window. Read-only — the
    successful creation is recorded separately by [_note_registration], so a refused or invalid
    attempt never consumes the quota."""
    now = monotonic()
    times = _registration_times[key]
    while times and times[0] <= now - REGISTRATION_WINDOW_SECONDS:
        times.popleft()
    return len(times) >= MAX_REGISTRATIONS_PER_DEVICE


def _note_registration(key: str) -> None:
    _registration_times[key].append(monotonic())


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)


class BackupRestore(BaseModel):
    """A participant's own data, returned only to the participant.

    Reading this needs the username and password - never the device API key. The key is
    extractable from the APK, so a read guarded by it would let anyone with the app download any
    participant's conversation given only an id. See `routes/backup.py` for the full reasoning.
    """

    schema_version: str
    device_updated_at: datetime
    payload: dict


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    # Asked for only by a device that has nothing stored locally - a fresh install or a
    # reinstall. Off by default so an ordinary login does not carry a conversation across the
    # wire for no reason.
    include_backup: bool = False


class ParticipantIdentity(BaseModel):
    """What the device caches and then uses offline for the rest of the study."""

    participant_id: str
    username: str
    display_name: str | None
    # Present only when `include_backup` was asked for and something is stored. Null on a first
    # sign-up, which the app reads as "nothing to restore" rather than as a failure.
    backup: BackupRestore | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _client_address(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _recent_failures(db: Session, *, username: str | None, address: str | None) -> int:
    since = _now() - FAILURE_WINDOW
    query = select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.succeeded.is_(False),
        LoginAttempt.attempted_at >= since,
    )
    if username is not None:
        query = query.where(LoginAttempt.username == username)
    if address is not None:
        query = query.where(LoginAttempt.client_address == address)
    return int(db.execute(query).scalar_one())


def _record_attempt(db: Session, username: str, address: str, succeeded: bool) -> None:
    db.add(
        LoginAttempt(
            username=username[:64],
            client_address=address,
            succeeded=succeeded,
            attempted_at=_now(),
        )
    )
    db.commit()


@router.post("/register", response_model=ParticipantIdentity, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ParticipantIdentity:
    username = payload.username.strip().lower()
    registration_key = _registration_key(request)

    # Before any work, and before touching the database: a throttled request must not reach the
    # ~540ms password hash, for the same reason the login throttle runs first.
    if _registration_limit_reached(registration_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many accounts have been created from this device recently. Try again later.",
            headers={"Retry-After": str(REGISTRATION_WINDOW_SECONDS)},
        )

    try:
        validate_username(username)
        validate_password(payload.password, username=username)
    except PasswordPolicyError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    existing = db.get(ParticipantAccount, username)
    if existing is not None:
        # Unlike login, this one does have to tell the truth: someone choosing a username needs
        # to know it is taken. The trade is deliberate and limited to registration.
        #
        # A closed account keeps its row for exactly this check. Saying so plainly is the point:
        # somebody who asked for deletion is meant to come back on a new name, and "already
        # taken" would read as a mistake they could retry their way out of.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "That username belonged to a deleted account and cannot be used again. "
                "Please choose a different one."
                if existing.is_closed
                else "That username is already taken."
            ),
        )

    # Opaque and unrelated to the username, so the research dataset carries no hint of identity
    # (spec 18 §40). token_urlsafe, not a counter — a sequential id would leak enrolment order
    # and cohort size.
    participant_id = "p-" + secrets.token_urlsafe(16)

    db.add(
        Participant(
            participant_id=participant_id,
            study_identifier=participant_id,
            app_version=None,
        )
    )
    db.add(
        ParticipantAccount(
            username=username,
            participant_id=participant_id,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name,
            created_at=_now(),
            password_changed_at=_now(),
        )
    )
    db.commit()

    # Only now, after a real account exists — so a failed or refused attempt above never counts
    # against the device.
    _note_registration(registration_key)

    return ParticipantIdentity(
        participant_id=participant_id,
        username=username,
        display_name=payload.display_name,
    )


@router.post("/login", response_model=ParticipantIdentity)
def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ParticipantIdentity:
    username = payload.username.strip().lower()
    address = _client_address(request)

    # Before the hash, for the same reason as the operator login: scrypt costs ~540 ms, so
    # hashing first would make the throttle the denial of service it exists to prevent.
    if _recent_failures(db, username=username, address=None) >= MAX_FAILURES_PER_USERNAME or (
        _recent_failures(db, username=None, address=address) >= MAX_FAILURES_PER_ADDRESS
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again in 15 minutes.",
            headers={"Retry-After": str(int(FAILURE_WINDOW.total_seconds()))},
        )

    account = db.get(ParticipantAccount, username)

    # A closed account is treated exactly like one that never existed - same status, same
    # wording, and a recorded failed attempt. Anything more specific ("this account was
    # deleted") would confirm to a stranger that the username was once real, and the person who
    # asked for the deletion already knows.
    if account is not None and account.is_closed:
        _record_attempt(db, username, address, succeeded=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    if account is None or not verify_password(payload.password, account.password_hash):
        _record_attempt(db, username, address, succeeded=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    if needs_rehash(account.password_hash):
        account.password_hash = hash_password(payload.password)

    account.last_login_at = _now()
    _record_attempt(db, username, address, succeeded=True)

    # The one place a participant's own data may be read back.
    #
    # It lives on this route rather than on `/backup` because this is the only request that
    # proves who is asking: the device key guarding `/backup` is compiled into the APK and
    # therefore guards nothing against somebody holding the app.
    backup = None
    if payload.include_backup:
        stored = db.get(ParticipantBackup, account.participant_id)
        if stored is not None:
            backup = BackupRestore(
                schema_version=stored.schema_version,
                device_updated_at=stored.device_updated_at,
                payload=stored.payload,
            )

    # No session token. The device does not authenticate again — it caches the id and syncs with
    # the device key, so it keeps working when this server is off (spec 04 freeze item 2).
    return ParticipantIdentity(
        participant_id=account.participant_id,
        username=account.username,
        display_name=account.display_name,
        backup=backup,
    )

class AccountClosed(BaseModel):
    participant_id: str
    account_removed: bool


#: Replaces the password hash of a closed account. Deliberately not a valid hash of anything -
#: there is no password that produces it, and it is obvious in a database dump what it means.
CLOSED_ACCOUNT_SENTINEL = "closed-account-no-password"


@router.delete("/account", response_model=AccountClosed)
def close_account(participant_id: str, db: Session = Depends(get_db)) -> AccountClosed:
    """Closes the account and **blocks the username**, so the participant must sign up anew.

    Asked for directly: once someone requests deletion they should be signed out and made to
    create a new account with a new username, rather than returning to the old one.

    The first version deleted the row, which *freed* the username. A live test proved the
    consequence - a closed `kaushik` registered as `kaushik` again and got a working account
    back, which is the opposite of what deletion is for. So a closure is a **tombstone**:

    ==================  ==========================================================
    username            kept - the primary key, and the thing that blocks re-use
    participant_id      NULL - severs the only join between a person and the data
    display_name        NULL - the profile detail is gone
    password_hash       replaced with a value no password can produce
    closed_at           set, and the login path refuses on it before comparing
    ==================  ==========================================================

    Keeping the row does not weaken spec 18 §68's de-identification step. That step *is* the
    removal of the username-to-participant_id join, and nulling `participant_id` removes exactly
    that. What survives is a username with nothing attached: no id, no display name, no usable
    credential. It cannot be signed into and it cannot be linked to an event.

    The block lasts until the table is cleared by `scripts/reset_participants.py`. Within a study
    round a name is gone for good; a fresh round starts from nothing.

    Deliberately **not** touched, for the same reasons as `DELETE /backup`:

    * `events` - pseudonymous, and the study's data rather than the participant's own copy.
      Removing rows mid-study would change results other participants are part of.
    * `participants` - joined to every event. Dropping it would orphan the dataset.

    Idempotent. A second call finds no account for that id - `participant_id` is NULL once
    closed - and reports `account_removed = false` rather than 404: the participant's phone may
    retry after a dropped connection, and an error there would leave them stuck on a screen
    telling them deletion failed when it had already succeeded.
    """
    account = db.execute(
        select(ParticipantAccount).where(ParticipantAccount.participant_id == participant_id)
    ).scalar_one_or_none()

    if account is None:
        return AccountClosed(participant_id=participant_id, account_removed=False)

    # `login_attempts` is deliberately left alone. Its own docstring says rows are kept rather
    # than deleted, because "three failures then a success" is the pattern spec 21 §67 security
    # testing needs to see afterwards - it is security evidence, not the participant's data.
    account.participant_id = None
    account.display_name = None
    # Not a hash of anything. `verify_password` splits on "$" and returns False when the shape is
    # wrong, so this can never match - but the login path refuses on `closed_at` before it gets
    # here, and this is the second lock rather than the first.
    account.password_hash = CLOSED_ACCOUNT_SENTINEL
    account.closed_at = _now()
    db.commit()
    return AccountClosed(participant_id=participant_id, account_removed=True)
