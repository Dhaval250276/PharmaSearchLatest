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
import re
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


def _is_valid_email(email: str) -> bool:
    """Validate email format using RFC 5322 simplified pattern."""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def _generate_verification_token() -> str:
    """Generate a secure verification token for email confirmation."""
    return secrets.token_urlsafe(32)


def send_verification_email(email: str, verification_token: str, app_url: str = "http://localhost:8000") -> bool:
    """Send verification email with confirmation link.

    Returns True if sent successfully, False otherwise.
    """
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    try:
        # Get email config from environment
        smtp_host = os.getenv("SMTP_HOST", "localhost")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_password = os.getenv("SMTP_PASSWORD", "")
        from_email = os.getenv("FROM_EMAIL", "noreply@pharmasearch.local")

        # Create verification link
        verification_url = f"{app_url}/admin/verify-email?token={verification_token}"

        # Create email
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Confirm Your PharmaSearch Admin Account"
        msg["From"] = from_email
        msg["To"] = email

        # Plain text version
        text = f"""Welcome to PharmaSearch!

Please confirm your email address by clicking the link below:

{verification_url}

This link will expire in 24 hours.

If you did not create this account, please ignore this email."""

        # HTML version
        html = f"""<html>
  <body>
    <h2>Welcome to PharmaSearch!</h2>
    <p>Please confirm your email address by clicking the button below:</p>
    <p><a href="{verification_url}" style="background-color: #4a90e2; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">Confirm Email</a></p>
    <p>Or copy this link:<br>{verification_url}</p>
    <p><small>This link will expire in 24 hours.</small></p>
    <p><small>If you did not create this account, please ignore this email.</small></p>
  </body>
</html>"""

        part1 = MIMEText(text, "plain")
        part2 = MIMEText(html, "html")
        msg.attach(part1)
        msg.attach(part2)

        # Send email
        if smtp_host and smtp_host != "localhost":
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                if smtp_user and smtp_password:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                server.send_message(msg)
                logger.info(f"Verification email sent to {email}")
                return True
        else:
            logger.warning(f"SMTP not configured. Verification email not sent to {email}")
            return False
    except Exception as e:
        logger.error(f"Failed to send verification email to {email}: {e}")
        return False


def register_user(email: str, password: str, app_url: str = "http://localhost:8000") -> tuple[bool, str]:
    """Register a new admin user in the database.

    Returns (success, message). On success, message is empty.
    On failure, message explains why (email invalid, taken, invalid password, etc).
    """
    from repository import get_connection, initialize_database

    email = email.strip()
    if not email:
        return False, "Email cannot be empty."
    if not _is_valid_email(email):
        return False, "Please enter a valid email address."
    if len(password) < 12:
        return False, "Password must be at least 12 characters."
    if not password:
        return False, "Password cannot be empty."

    initialize_database()
    try:
        with get_connection() as conn:
            # Check if email already exists
            existing = conn.execute(
                "SELECT id FROM admin_users WHERE username = ?",
                (email.lower(),)
            ).fetchone()
            if existing:
                return False, "Email already registered."

            # Create verification token
            verification_token = _generate_verification_token()
            verification_expires = (datetime.now(timezone.utc) + __import__('datetime').timedelta(hours=24)).isoformat()

            # Create new user
            password_hash = hash_password(password)
            conn.execute(
                """INSERT INTO admin_users
                   (username, password_hash, created_at, is_active, email_verified,
                    verification_token, verification_expires)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (email.lower(), password_hash, datetime.now(timezone.utc).isoformat(), 1, 0,
                 verification_token, verification_expires)
            )
            logger.info(f"New admin user registered: {email}")

            # Send verification email
            send_verification_email(email, verification_token, app_url)
            return True, ""
    except Exception as e:
        logger.error(f"Registration failed: {e}")
        return False, "Registration failed. Please try again."


def verify_email(token: str) -> tuple[bool, str]:
    """Verify email using the verification token.

    Returns (success, message).
    """
    from repository import get_connection, initialize_database

    initialize_database()
    try:
        with get_connection() as conn:
            user = conn.execute(
                """SELECT id, verification_expires, username
                   FROM admin_users
                   WHERE verification_token = ? AND email_verified = 0""",
                (token,)
            ).fetchone()

            if not user:
                return False, "Invalid or expired verification link."

            # Check if token has expired
            if datetime.fromisoformat(user[1]) < datetime.now(timezone.utc):
                return False, "Verification link has expired. Please register again."

            # Mark email as verified
            conn.execute(
                "UPDATE admin_users SET email_verified = 1, verification_token = NULL WHERE id = ?",
                (user[0],)
            )
            logger.info(f"Email verified for user: {user[2]}")
            return True, "Email verified successfully! You can now sign in."
    except Exception as e:
        logger.error(f"Email verification failed: {e}")
        return False, "Verification failed. Please try again."


def authenticate_db_user(username: str, password: str) -> tuple[bool, str]:
    """Check credentials against the admin_users database.

    Returns (success, message). On success, message is empty.
    On failure, message explains why (email not verified, etc).
    """
    from repository import get_connection, initialize_database

    username = username.strip()
    if not username or not password:
        return False, ""

    initialize_database()
    try:
        with get_connection() as conn:
            user = conn.execute(
                "SELECT password_hash, email_verified FROM admin_users WHERE username = ? AND is_active = 1",
                (username.lower(),)
            ).fetchone()
            if not user:
                return False, ""

            password_hash, email_verified = user
            password_ok = verify_password(password, password_hash)

            if not password_ok:
                return False, ""

            # Check if email is verified
            if not email_verified:
                return False, "Please verify your email address before signing in."

            # Update last_login
            conn.execute(
                "UPDATE admin_users SET last_login = ? WHERE username = ?",
                (datetime.now(timezone.utc).isoformat(), username.lower())
            )
            return True, ""
    except Exception as e:
        logger.error(f"Database authentication failed: {e}")
        return False, ""


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


def request_password_reset(email: str, app_url: str = "http://localhost:8000") -> tuple[bool, str]:
    """Generate password reset token and send reset email.

    Returns (success, message).
    """
    from repository import get_connection, initialize_database

    email = email.strip()
    if not email:
        return False, "Email cannot be empty."
    if not _is_valid_email(email):
        return False, "Please enter a valid email address."

    initialize_database()
    try:
        with get_connection() as conn:
            user = conn.execute(
                "SELECT id FROM admin_users WHERE username = ? AND is_active = 1",
                (email.lower(),)
            ).fetchone()
            if not user:
                return True, "If an account exists with this email, a password reset link has been sent."

            # Generate reset token
            reset_token = _generate_verification_token()
            reset_expires = (datetime.now(timezone.utc) + __import__('datetime').timedelta(hours=1)).isoformat()

            # Store reset token
            conn.execute(
                "UPDATE admin_users SET reset_token = ?, reset_expires = ? WHERE id = ?",
                (reset_token, reset_expires, user[0])
            )
            logger.info(f"Password reset requested for {email}")

            # Send reset email
            send_password_reset_email(email, reset_token, app_url)
            return True, "If an account exists with this email, a password reset link has been sent."
    except Exception as e:
        logger.error(f"Password reset request failed: {e}")
        return False, "Password reset request failed. Please try again."


def send_password_reset_email(email: str, reset_token: str, app_url: str = "http://localhost:8000") -> bool:
    """Send password reset email with link.

    Returns True if sent successfully, False otherwise.
    """
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    try:
        smtp_host = os.getenv("SMTP_HOST", "localhost")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_password = os.getenv("SMTP_PASSWORD", "")
        from_email = os.getenv("FROM_EMAIL", "noreply@pharmasearch.local")

        reset_url = f"{app_url}/admin/reset-password?token={reset_token}"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Reset Your PharmaSearch Admin Password"
        msg["From"] = from_email
        msg["To"] = email

        text = f"""Password Reset Request

We received a request to reset the password for your PharmaSearch admin account.

Click the link below to reset your password:

{reset_url}

This link will expire in 1 hour.

If you did not request this password reset, please ignore this email."""

        html = f"""<html>
  <body>
    <h2>Password Reset Request</h2>
    <p>We received a request to reset the password for your PharmaSearch admin account.</p>
    <p><a href="{reset_url}" style="background-color: #4a90e2; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">Reset Password</a></p>
    <p>Or copy this link:<br>{reset_url}</p>
    <p><small>This link will expire in 1 hour.</small></p>
    <p><small>If you did not request this password reset, please ignore this email.</small></p>
  </body>
</html>"""

        part1 = MIMEText(text, "plain")
        part2 = MIMEText(html, "html")
        msg.attach(part1)
        msg.attach(part2)

        if smtp_host and smtp_host != "localhost":
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                if smtp_user and smtp_password:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                server.send_message(msg)
                logger.info(f"Password reset email sent to {email}")
                return True
        else:
            logger.warning(f"SMTP not configured. Password reset email not sent to {email}")
            return False
    except Exception as e:
        logger.error(f"Failed to send password reset email to {email}: {e}")
        return False


def reset_password(token: str, new_password: str) -> tuple[bool, str]:
    """Reset password using reset token.

    Returns (success, message).
    """
    from repository import get_connection, initialize_database

    new_password = new_password or ""
    if len(new_password) < 12:
        return False, "Password must be at least 12 characters."
    if not new_password:
        return False, "Password cannot be empty."

    initialize_database()
    try:
        with get_connection() as conn:
            user = conn.execute(
                """SELECT id, reset_expires, username
                   FROM admin_users
                   WHERE reset_token = ? AND reset_token IS NOT NULL""",
                (token,)
            ).fetchone()

            if not user:
                return False, "Invalid or expired password reset link."

            # Check if token has expired
            if datetime.fromisoformat(user[1]) < datetime.now(timezone.utc):
                return False, "Password reset link has expired. Please request a new one."

            # Update password
            password_hash = hash_password(new_password)
            conn.execute(
                "UPDATE admin_users SET password_hash = ?, reset_token = NULL, reset_expires = NULL WHERE id = ?",
                (password_hash, user[0])
            )
            logger.info(f"Password reset for user: {user[2]}")
            return True, "Password reset successfully! You can now sign in with your new password."
    except Exception as e:
        logger.error(f"Password reset failed: {e}")
        return False, "Password reset failed. Please try again."
