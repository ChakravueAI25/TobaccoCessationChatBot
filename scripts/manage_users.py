"""Create, update and list operator accounts.

Run this rather than inserting rows by hand: it applies the password policy and produces the
scrypt record the login endpoint expects.

    uv run python scripts/manage_users.py create abhi_22 --display-name "Abhinay"
    uv run python scripts/manage_users.py set-password abhi_22
    uv run python scripts/manage_users.py list
    uv run python scripts/manage_users.py disable abhi_22

The password is **prompted for, never passed as an argument**. A password on a command line ends
up in shell history, in the process list while the command runs, and in any terminal transcript —
which is why there is no `--password` flag. `QS_ADMIN_PASSWORD` is honoured for a non-interactive
run (CI, a provisioning script); prefer the prompt.

Nothing here writes a password into a file, and no default account exists: an install with no
users cannot be signed into, which is the correct state until someone runs this deliberately.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime, timezone

# Importable when run as `python scripts/manage_users.py` from the repository root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app.database import SessionLocal  # noqa: E402
from api.app.models import AuthSession, User  # noqa: E402
from api.app.security import (  # noqa: E402
    PasswordPolicyError,
    hash_password,
    validate_password,
    validate_username,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_password(username: str, *, confirm: bool) -> str:
    from_env = os.environ.get("QS_ADMIN_PASSWORD")
    if from_env:
        print("Using QS_ADMIN_PASSWORD from the environment.")
        validate_password(from_env, username=username)
        return from_env

    password = getpass.getpass("New password: ")
    if confirm and password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords did not match. Nothing changed.")
    validate_password(password, username=username)
    return password


def create(args: argparse.Namespace) -> int:
    username = args.username.strip().lower()
    validate_username(username)

    with SessionLocal() as db:
        if db.get(User, username) is not None:
            print(f"User {username!r} already exists. Use set-password to change it.")
            return 1

        password = _read_password(username, confirm=True)
        db.add(
            User(
                username=username,
                password_hash=hash_password(password),
                display_name=args.display_name,
                role=args.role,
                disabled=False,
                password_changed_at=_now(),
            )
        )
        db.commit()

    print(f"Created {username!r}. Sign in at POST /api/v1/auth/login or on the dashboard.")
    return 0


def set_password(args: argparse.Namespace) -> int:
    username = args.username.strip().lower()

    with SessionLocal() as db:
        user = db.get(User, username)
        if user is None:
            print(f"No such user: {username!r}")
            return 1

        user.password_hash = hash_password(_read_password(username, confirm=True))
        user.password_changed_at = _now()

        # Same rule the change-password endpoint follows: a password reset is what happens when
        # a credential is suspected leaked, so every existing session goes with it.
        revoked = db.query(AuthSession).filter(
            AuthSession.username == username,
            AuthSession.revoked_at.is_(None),
        ).update({"revoked_at": _now()})
        db.commit()

    print(f"Password changed for {username!r}. {revoked} active session(s) signed out.")
    return 0


def set_disabled(args: argparse.Namespace, disabled: bool) -> int:
    username = args.username.strip().lower()

    with SessionLocal() as db:
        user = db.get(User, username)
        if user is None:
            print(f"No such user: {username!r}")
            return 1
        user.disabled = disabled
        if disabled:
            db.query(AuthSession).filter(
                AuthSession.username == username,
                AuthSession.revoked_at.is_(None),
            ).update({"revoked_at": _now()})
        db.commit()

    print(f"{username!r} is now {'disabled' if disabled else 'enabled'}.")
    return 0


def list_users(_: argparse.Namespace) -> int:
    with SessionLocal() as db:
        users = db.query(User).order_by(User.username).all()
        if not users:
            print("No operator accounts. Create one with:  manage_users.py create <username>")
            return 0
        print(f"{'username':<20} {'role':<12} {'state':<10} last login")
        for user in users:
            state = "disabled" if user.disabled else "active"
            last = user.last_login_at.isoformat() if user.last_login_at else "never"
            print(f"{user.username:<20} {user.role:<12} {state:<10} {last}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Quit Smoke operator accounts.")
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create", help="create an operator account")
    create_parser.add_argument("username")
    create_parser.add_argument("--display-name", default=None)
    create_parser.add_argument("--role", default="operator")
    create_parser.set_defaults(func=create)

    password_parser = sub.add_parser("set-password", help="change a password")
    password_parser.add_argument("username")
    password_parser.set_defaults(func=set_password)

    disable_parser = sub.add_parser("disable", help="disable an account and sign it out")
    disable_parser.add_argument("username")
    disable_parser.set_defaults(func=lambda a: set_disabled(a, True))

    enable_parser = sub.add_parser("enable", help="re-enable an account")
    enable_parser.add_argument("username")
    enable_parser.set_defaults(func=lambda a: set_disabled(a, False))

    list_parser = sub.add_parser("list", help="list operator accounts")
    list_parser.set_defaults(func=list_users)

    args = parser.parse_args()
    try:
        return args.func(args)
    except PasswordPolicyError as error:
        print(f"Rejected: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
