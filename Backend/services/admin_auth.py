"""Username and password sign-in for the admin section.

Everything here is standard library. Passwords are stored as a scrypt digest
and never in plain text, the session cookie is signed so it cannot be forged
or edited, and both comparisons run in constant time.

Configure with three environment variables:

    PHARMASEARCH_ADMIN_USERNAME       the admin's user name
    PHARMASEARCH_ADMIN_PASSWORD_HASH  the digest printed by tools/make_admin_password.py
    PHARMASEARCH_SESSION_SECRET       any long random string

Leaving PHARMASEARCH_SESSION_SECRET unset is allowed for local use: a random
secret is generated per process, which simply signs everyone out on restart.
Without the first two the admin section stays closed and says so, so a missing
configuration can never leave it standing open.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone

import config  # noqa: F401 -- loads Backend/.env before the variables below are read
from core.logging_config import get_logger


logger = get_logger(__name__)

SESSION_COOKIE_NAME = "pharmasearch_admin"
SESSION_TTL_SECONDS = 8 * 60 * 60

# The sign-in guards the whole application, not only /admin, so the cookie has
# to travel with every request rather than just the admin ones.
SESSION_COOKIE_PATH = "/"

# Work factors for scrypt. n is the cost, and raising it makes both a login
# and an offline guess proportionally slower.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32

# A user name and password pair may be tried this many times in this window
# before the account stops answering, so the form cannot be used to guess.
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 15 * 60

_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()
_process_secret = secrets.token_urlsafe(48)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Return a self-describing digest that verify_password() can read back."""
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_KEY_BYTES,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """True when the password produces the stored digest. Never raises."""
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=_b64decode(salt),
            n=int(n), r=int(r), p=int(p), dklen=len(_b64decode(expected)),
        )
    except Exception:
        logger.warning("The stored admin password digest could not be read")
        return False
    return hmac.compare_digest(digest, _b64decode(expected))


def _session_secret() -> str:
    return os.getenv("PHARMASEARCH_SESSION_SECRET") or _process_secret


def admin_username() -> str:
    return (os.getenv("PHARMASEARCH_ADMIN_USERNAME") or "").strip()


def _admin_password_hash() -> str:
    return (os.getenv("PHARMASEARCH_ADMIN_PASSWORD_HASH") or "").strip()


def admin_is_configured() -> bool:
    """True once a user name and a password digest are both present."""
    return bool(admin_username() and _admin_password_hash())


def _sign(payload: str) -> str:
    return _b64encode(
        hmac.new(_session_secret().encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    )


def issue_session(username: str) -> str:
    payload = _b64encode(
        json.dumps({"u": username, "exp": int(time.time()) + SESSION_TTL_SECONDS}).encode("utf-8")
    )
    return f"{payload}.{_sign(payload)}"


def read_session(token: str | None) -> str | None:
    """Return the signed-in user name, or None for anything not currently valid."""
    if not token or "." not in token:
        return None
    payload, _, signature = token.partition(".")
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        claims = json.loads(_b64decode(payload))
    except Exception:
        return None
    if int(claims.get("exp", 0)) < time.time():
        return None
    username = str(claims.get("u") or "")
    # A user name changed in the environment must not keep old cookies alive.
    return username if username and username == admin_username() else None


def _attempt_key(username: str, client_ip: str) -> str:
    return f"{username.lower()}|{client_ip}"


def seconds_until_unlocked(username: str, client_ip: str) -> int:
    """How long this user name and address must wait, or 0 when free to try."""
    key = _attempt_key(username, client_ip)
    now = time.time()
    with _attempts_lock:
        recent = [stamp for stamp in _attempts.get(key, []) if now - stamp < _LOCKOUT_SECONDS]
        _attempts[key] = recent
        if len(recent) < _MAX_ATTEMPTS:
            return 0
        return max(1, int(_LOCKOUT_SECONDS - (now - min(recent))))


def record_failure(username: str, client_ip: str) -> None:
    key = _attempt_key(username, client_ip)
    with _attempts_lock:
        _attempts.setdefault(key, []).append(time.time())


def clear_failures(username: str, client_ip: str) -> None:
    with _attempts_lock:
        _attempts.pop(_attempt_key(username, client_ip), None)


def authenticate(username: str, password: str) -> bool:
    """True when the pair matches the configured admin.

    The password is checked even when the user name is already wrong so that
    a wrong name and a wrong password take the same time to answer.
    """
    expected_user = admin_username()
    expected_hash = _admin_password_hash()
    if not expected_user or not expected_hash:
        return False
    password_ok = verify_password(password, expected_hash)
    user_ok = hmac.compare_digest(username.strip(), expected_user)
    return password_ok and user_ok


def register_user(username: str, password: str) -> tuple[bool, str]:
    """Register a new admin user in the database.

    Returns (success, message). On success, message is empty.
    On failure, message explains why (username taken, invalid password, etc).
    """
    from repository import get_connection, initialize_database

    username = username.strip()
    if not username:
        return False, "Username cannot be empty."
    if len(username) < 3:
        return False, "Username must be at least 3 characters."
    if len(password) < 12:
        return False, "Password must be at least 12 characters."
    if not password:
        return False, "Password cannot be empty."

    initialize_database()
    try:
        with get_connection() as conn:
            # Check if username already exists
            existing = conn.execute(
                "SELECT id FROM admin_users WHERE username = ?",
                (username.lower(),)
            ).fetchone()
            if existing:
                return False, "Username already taken."

            # Create new user
            password_hash = hash_password(password)
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, created_at, is_active) VALUES (?, ?, ?, ?)",
                (username.lower(), password_hash, datetime.now(timezone.utc).isoformat(), 1)
            )
            logger.info(f"New admin user registered: {username}")
            return True, ""
    except Exception as e:
        logger.error(f"Registration failed: {e}")
        return False, "Registration failed. Please try again."


def authenticate_db_user(username: str, password: str) -> bool:
    """Check credentials against the admin_users database."""
    from repository import get_connection, initialize_database

    username = username.strip()
    if not username or not password:
        return False

    initialize_database()
    try:
        with get_connection() as conn:
            user = conn.execute(
                "SELECT password_hash FROM admin_users WHERE username = ? AND is_active = 1",
                (username.lower(),)
            ).fetchone()
            if not user:
                return False

            password_ok = verify_password(password, user[0])
            if password_ok:
                # Update last_login
                conn.execute(
                    "UPDATE admin_users SET last_login = ? WHERE username = ?",
                    (datetime.now(timezone.utc).isoformat(), username.lower())
                )
            return password_ok
    except Exception as e:
        logger.error(f"Database authentication failed: {e}")
        return False


def user_exists(username: str) -> bool:
    """Check if a user exists in the database."""
    from repository import get_connection, initialize_database

    initialize_database()
    try:
        with get_connection() as conn:
            result = conn.execute(
                "SELECT id FROM admin_users WHERE username = ?",
                (username.strip().lower(),)
            ).fetchone()
            return bool(result)
    except Exception:
        return False
