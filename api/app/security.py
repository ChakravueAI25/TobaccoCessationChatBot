"""Password hashing, password policy, and session tokens.

Spec 22 §92 keeps secrets out of the repository, and spec 21 §67 requires security testing of
this boundary. Everything here is deliberately boring: the interesting decisions are which
algorithm and which parameters, and both are written down rather than left to a library default.

**Hashing is `hashlib.scrypt`, from the standard library.** scrypt is memory-hard and is one of
the algorithms the OWASP Password Storage Cheat Sheet recommends, so no dependency is needed —
which matters for a backend the study team installs on a laptop. The parameters below are the
OWASP minimum (n=2^17, r=8, p=1) and were measured at ~540 ms and ~128 MiB per hash on the
development machine. That is deliberately slow: it is the entire defence against an offline
attack on a stolen database, and this service authenticates a handful of researchers, not
thousands of users per second.

Never replace this with a bare `sha256(password)`. A single-pass hash of a human-chosen password
is recoverable in minutes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import string

# --------------------------------------------------------------------------- hashing

# OWASP minimum for scrypt. Encoded into every stored hash so the parameters can be raised
# later without invalidating existing passwords — verify reads them back from the record.
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

# Python's scrypt refuses to allocate more than this unless told otherwise, and the default is
# far below what n=2^17 needs. 256 MiB leaves headroom above the ~128 MiB the parameters use.
SCRYPT_MAXMEM = 256 * 1024 * 1024

HASH_SCHEME = "scrypt"


def hash_password(password: str) -> str:
    """Return a self-describing hash: ``scrypt$n$r$p$salt$key``, both parts base64.

    Self-describing so [verify_password] never has to assume the parameters a stored hash was
    produced with. Raising SCRYPT_N in a year then keeps old logins working.
    """
    salt = secrets.token_bytes(SALT_BYTES)
    derived = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join(
        [
            HASH_SCHEME,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _b64(salt),
            _b64(derived),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a hash from [hash_password].

    Returns False rather than raising on a malformed record: a corrupted row must fail the
    login, not crash the endpoint and reveal that the account exists.
    """
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
        if scheme != HASH_SCHEME:
            return False
        derived = _derive(password, _unb64(salt_b64), int(n), int(r), int(p))
    except (ValueError, TypeError, MemoryError):
        return False

    # compare_digest, never ==. A short-circuiting comparison leaks how much of the hash
    # matched, which is enough to reconstruct it one byte at a time.
    return hmac.compare_digest(derived, _unb64(key_b64))


def needs_rehash(stored: str) -> bool:
    """True when a stored hash uses weaker parameters than the current settings.

    Called after a successful login so an account silently upgrades the next time its owner
    signs in, rather than staying on old parameters until someone remembers to migrate.
    """
    try:
        scheme, n, r, p, _salt, _key = stored.split("$")
    except ValueError:
        return True
    return scheme != HASH_SCHEME or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(encoded: str) -> bytes:
    return base64.b64decode(encoded.encode("ascii"))


# ---------------------------------------------------------------------------- policy

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{3,32}$")

# Rejected outright regardless of whether they satisfy the character rules. Short list on
# purpose: a long denylist gives a false sense of coverage, and the real defence against
# guessing is the rate limit in `routes/auth.py`, not this.
COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd",
        "qwerty123",
        "12345678",
        "123456789",
        "admin123",
        "letmein1",
        "welcome1",
        "changeme",
        "iloveyou",
        "quitsmoke",
        "quitsmoke1",
    }
)

_SYMBOLS = set(string.punctuation)


class PasswordPolicyError(ValueError):
    """A password that the policy refuses. The message is safe to show the operator."""


def validate_password(password: str, username: str | None = None) -> None:
    """Raise [PasswordPolicyError] unless ``password`` satisfies the policy.

    The rules are the ordinary four-class ones: at least eight characters with an upper-case
    letter, a lower-case letter, a digit and a symbol. `Abhi@2004` satisfies all four.

    Length is capped as well as floored — scrypt hashes the whole input, so an unbounded
    password is a cheap way to make every login attempt expensive.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    if not any(c.isupper() for c in password):
        raise PasswordPolicyError("Password must contain an upper-case letter.")
    if not any(c.islower() for c in password):
        raise PasswordPolicyError("Password must contain a lower-case letter.")
    if not any(c.isdigit() for c in password):
        raise PasswordPolicyError("Password must contain a digit.")
    if not any(c in _SYMBOLS for c in password):
        raise PasswordPolicyError("Password must contain a symbol.")
    if password.lower() in COMMON_PASSWORDS:
        raise PasswordPolicyError("That password is too common. Choose another.")
    if username and username.lower() in password.lower():
        raise PasswordPolicyError("Password must not contain the username.")


def validate_username(username: str) -> None:
    if not USERNAME_PATTERN.fullmatch(username):
        raise PasswordPolicyError(
            "Username must be 3-32 characters of lower-case letters, digits, dot, underscore or hyphen."
        )


# ---------------------------------------------------------------------------- tokens

TOKEN_BYTES = 32


def new_session_token() -> str:
    """A fresh opaque session token.

    Opaque and server-stored rather than a signed JWT: this service already has a database, a
    stored token can be revoked immediately on logout, and there is no second service that
    needs to verify it without a round trip. A JWT would add a signing key to manage and make
    logout advisory.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """What gets stored for a session token.

    The token itself is a bearer credential, so the database holds only a SHA-256 of it — a
    stolen dump then cannot be replayed as a live session. A single hash pass is correct here
    and *not* correct for passwords: this input is 256 bits of `secrets` output, so there is no
    guessable space for an attacker to search.
    """
    return hashlib.sha256(token.encode("ascii")).hexdigest()
