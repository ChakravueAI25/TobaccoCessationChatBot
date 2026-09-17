"""Operator login.

Replaces "paste the shared API key into the dashboard" with real accounts. The device API key
stays for `POST /api/v1/sync`: spec 04 freeze item 2 requires the Android app to work with this
server off, so a participant's phone must never need an interactive login.

The rate limit is the reason this file is more than twenty lines. A password endpoint with no
throttle is an offline attack that does not even need the database — so failures are counted per
username *and* per client address, and both are checked before the password is even hashed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import current_user, session_cookie_name
from ..models import AuthSession, LoginAttempt, User
from ..security import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    new_session_token,
    token_fingerprint,
    validate_password,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth")

# --- rate limit ------------------------------------------------------------------
#
# Two windows, because they stop different attacks. The per-username limit stops someone
# guessing one researcher's password; the per-address limit stops someone spraying one guess
# across many usernames from the same machine. The address limit is looser because a whole
# study team can legitimately share an office NAT address.

MAX_FAILURES_PER_USERNAME = 5
MAX_FAILURES_PER_ADDRESS = 20
FAILURE_WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)

SESSION_LIFETIME = timedelta(hours=12)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    username: str
    display_name: str | None
    role: str
    expires_at: datetime
    # Returned as well as set as a cookie: the dashboard uses the cookie, and a script or a
    # Bruno collection needs the bearer form.
    token: str


class MeResponse(BaseModel):
    username: str
    display_name: str | None
    role: str
    last_login_at: datetime | None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _client_address(request: Request) -> str:
    """Best-effort client identity for the rate limit.

    `X-Forwarded-For` is honoured because the study may put this behind a reverse proxy, and is
    *only* a rate-limit key — never an authorisation input — so a spoofed value costs the
    attacker their own bucket rather than buying them access.
    """
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


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    username = payload.username.strip().lower()
    address = _client_address(request)

    # Checked before the password is hashed. scrypt costs ~540 ms, so hashing first would turn
    # the throttle itself into the denial of service it exists to prevent.
    if _recent_failures(db, username=username, address=None) >= MAX_FAILURES_PER_USERNAME or (
        _recent_failures(db, username=None, address=address) >= MAX_FAILURES_PER_ADDRESS
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again in 15 minutes.",
            headers={"Retry-After": str(int(LOCKOUT.total_seconds()))},
        )

    user = db.get(User, username)

    # One message and one status for every failure — wrong username, wrong password, disabled
    # account. Distinguishing them tells an attacker which usernames exist.
    if user is None or user.disabled or not verify_password(payload.password, user.password_hash):
        _record_attempt(db, username, address, succeeded=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    if needs_rehash(user.password_hash):
        # Silent upgrade on the one occasion the plaintext is available.
        user.password_hash = hash_password(payload.password)

    token = new_session_token()
    expires_at = _now() + SESSION_LIFETIME
    db.add(
        AuthSession(
            token_fingerprint=token_fingerprint(token),
            username=user.username,
            created_at=_now(),
            expires_at=expires_at,
            last_seen_at=_now(),
            client_address=address,
        )
    )
    user.last_login_at = _now()
    _record_attempt(db, username, address, succeeded=True)

    # HttpOnly so page scripts cannot read it, SameSite=Strict so another site cannot ride it.
    # `secure` is deliberately not set: the development deployment is plain HTTP on a LAN
    # (spec 23 §2) and a Secure cookie would simply never be stored, making login fail with no
    # visible reason. Set it when the study runs over HTTPS.
    response.set_cookie(
        key=session_cookie_name(),
        value=token,
        httponly=True,
        samesite="strict",
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
    )

    return LoginResponse(
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        expires_at=expires_at,
        token=token,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> Response:
    """Revokes the presented session, if there is one.

    Always 204, even with no session: logout is idempotent and must not become a way to probe
    whether a token is valid.
    """
    token = request.cookies.get(session_cookie_name()) or _bearer(request)
    if token:
        record = db.get(AuthSession, token_fingerprint(token))
        if record is not None and record.revoked_at is None:
            record.revoked_at = _now()
            db.commit()

    response.delete_cookie(key=session_cookie_name(), path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(current_user)) -> MeResponse:
    return MeResponse(
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        last_login_at=user.last_login_at,
    )


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )
    try:
        validate_password(payload.new_password, username=user.username)
    except PasswordPolicyError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    user.password_hash = hash_password(payload.new_password)
    user.password_changed_at = _now()

    # Every other session is revoked: a password change is what someone does when they think a
    # credential leaked, and leaving old sessions alive makes it useless.
    db.execute(
        AuthSession.__table__.update()
        .where(AuthSession.username == user.username, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None
