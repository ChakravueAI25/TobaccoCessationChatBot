"""Request authentication and rate limiting.

Two credentials, for two kinds of caller, and keeping them separate is a spec requirement
rather than a convenience:

* **A device** presents `X-API-Key`. That is how the Android app syncs, and it must stay
  non-interactive — spec 04 freeze item 2 requires the app to work with this server switched
  off, so a phone can never be asked to complete a login flow.
* **A human** presents a session cookie or `Authorization: Bearer`, obtained from
  `POST /api/v1/auth/login`. That is the dashboard, the exports and the research reads.

`require_api_key` accepts *either*, so the research endpoints work for both a logged-in
researcher and a script holding the device key. `current_user` accepts only a session, and is
used where an actual person must be identified.

`rate_limit` is separate from all of that and deliberately so: the login throttle in
`routes/auth.py` counts *failures* to make password guessing expensive, which is the wrong
shape for endpoints where every request is legitimate and the cost is volume.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from time import monotonic

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import AuthSession, User

SESSION_COOKIE = "qs_session"

# How stale `last_seen_at` may get before it is written again. Without this every authenticated
# request writes a row, which on the export endpoints means a commit per poll of the dashboard.
LAST_SEEN_REFRESH = timedelta(minutes=5)


def session_cookie_name() -> str:
    return SESSION_COOKIE


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _presented_token(request: Request) -> str | None:
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def _as_aware(value: datetime | None) -> datetime | None:
    """Treats a naive timestamp as UTC.

    SQLite drops the timezone on a `DateTime(timezone=True)` column, so a value read back is
    naive there and aware on PostgreSQL. Comparing a naive value to an aware one raises, which
    would turn every session check into a 500 on the test database.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _resolve_session(request: Request, db: Session) -> User | None:
    token = _presented_token(request)
    if not token:
        return None

    # Imported here rather than at module scope: security imports nothing from this module, but
    # keeping the dependency one-directional makes that obvious.
    from .security import token_fingerprint

    record = db.get(AuthSession, token_fingerprint(token))
    if record is None or record.revoked_at is not None:
        return None

    expires_at = _as_aware(record.expires_at)
    if expires_at is not None and expires_at <= _now():
        return None

    user = db.get(User, record.username)
    if user is None or user.disabled:
        return None

    last_seen = _as_aware(record.last_seen_at)
    if last_seen is None or _now() - last_seen > LAST_SEEN_REFRESH:
        record.last_seen_at = _now()
        db.commit()

    return user


def require_api_key(request: Request, db: Session = Depends(get_db)) -> None:
    """Accepts a device API key or a logged-in operator session.

    Kept as the name the existing routes already depend on, so adding accounts did not require
    touching every endpoint — and so the device sync path is unchanged.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return

    presented_key = request.headers.get("x-api-key")
    if presented_key is not None:
        import hmac

        # compare_digest, not ==: the key is a shared secret and a short-circuiting comparison
        # leaks its prefix.
        if hmac.compare_digest(presented_key, settings.development_api_key):
            return

    if _resolve_session(request, db) is not None:
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """The signed-in operator, or 401.

    Deliberately does not accept the device API key: a key identifies a phone, not a person, and
    everything using this needs to know who acted.
    """
    user = _resolve_session(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in required",
        )
    return user


# --------------------------------------------------------------------------- rate limiting

RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_REQUESTS = 120

# Keyed by socket peer, which is the completed TCP handshake's address and so cannot be forged
# the way an X-Forwarded-For header could. That also bounds the map: one entry per host that has
# actually connected, on a LAN, rather than one per address anybody claims.
_request_times: dict[str, deque[float]] = defaultdict(deque)


def reset_rate_limits() -> None:
    """Clears the window. For tests, which would otherwise leak counts between them."""
    _request_times.clear()


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request) -> None:
    """A per-address ceiling on the routes the login throttle does not cover.

    Sized so the app cannot reach it: the device syncs once every five minutes and the operator
    dashboard has no auto-refresh at all, so this is several hundred times either one's rate.
    That matters more than the ceiling being tight — throttling a device that is draining a
    backlog would slow the study's data collection to stop an attack nobody is mounting.

    In-process and in-memory on purpose: this is one uvicorn process on a laptop on a LAN
    (spec 23 §2), so a shared store would be another dependency and another thing to run for no
    gain here. **The ceiling to know about is that it resets on restart and does not span
    workers.** If this is ever run with `--workers` or behind a load balancer it stops being a
    limit and wants replacing rather than tuning.
    """
    now = monotonic()
    times = _request_times[_client_host(request)]

    while times and times[0] <= now - RATE_LIMIT_WINDOW_SECONDS:
        times.popleft()

    if len(times) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Slow down and try again shortly.",
            headers={"Retry-After": str(int(RATE_LIMIT_WINDOW_SECONDS))},
        )

    times.append(now)
