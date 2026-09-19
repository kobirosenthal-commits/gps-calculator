"""Authentication and authorization for CreditLens.

Users are stored with salted PBKDF2 password hashes. Roles gate access:
  admin         — user management + everything below
  risk_manager  — decision history, compliance policy + everything below
  underwriter   — assess, consent, customer search

Login attempts are rate-limited per user and recorded in an auth event
log. In production this module maps to the bank's IdP (SSO/SAML/OIDC);
the role contract stays the same.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import secrets
import sqlite3
import threading

from werkzeug.security import check_password_hash, generate_password_hash

AUTH_DATABASE = Path(os.environ.get(
    "CREDIT_AUTH_DATABASE",
    Path(__file__).resolve().parent / "credit_auth.sqlite3",
))

ROLES = ("underwriter", "risk_manager", "admin")
_ROLE_RANK = {role: rank for rank, role in enumerate(ROLES)}

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

# Demo bootstrap users — replace with the bank IdP before production.
DEFAULT_USERS = [
    ("5331", "5331", "admin", "מנהל מערכת"),
    ("rachel.r", "Risk!2026", "risk_manager", "רחל רביב — מנהלת סיכונים"),
    ("dan.k", "Loan!2026", "underwriter", "דן קליין — חתם אשראי"),
]

_LOCK = threading.Lock()


def _connect():
    AUTH_DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(AUTH_DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            display_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_events (
            id INTEGER PRIMARY KEY,
            at TEXT NOT NULL,
            username TEXT NOT NULL,
            event TEXT NOT NULL,
            detail TEXT
        );
    """)
    return connection


def _log_event(connection, username, event, detail=None):
    connection.execute(
        "INSERT INTO auth_events (at, username, event, detail) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), username, event, detail),
    )


def ensure_default_users():
    """Create bootstrap users once (no-op if they already exist)."""
    with _LOCK:
        connection = _connect()
        try:
            for username, password, role, display_name in DEFAULT_USERS:
                exists = connection.execute(
                    "SELECT 1 FROM users WHERE username = ?", (username,)
                ).fetchone()
                if not exists:
                    connection.execute(
                        """INSERT INTO users
                           (username, password_hash, role, display_name, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (username,
                         generate_password_hash(password, method="pbkdf2:sha256:600000"),
                         role, display_name,
                         datetime.now(timezone.utc).isoformat()),
                    )
                    _log_event(connection, username, "user_created", f"role={role}")
            connection.commit()
        finally:
            connection.close()


def authenticate(username, password):
    """Validate credentials with lockout. Returns user dict or raises
    PermissionError with a Hebrew message safe to show the operator."""
    username = (username or "").strip().lower()
    now = datetime.now(timezone.utc)
    with _LOCK:
        connection = _connect()
        try:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            generic = PermissionError("שם משתמש או סיסמה שגויים")
            if row is None or not row["active"]:
                _log_event(connection, username, "login_failed", "unknown_or_inactive")
                connection.commit()
                raise generic
            if row["locked_until"] and now < datetime.fromisoformat(row["locked_until"]):
                _log_event(connection, username, "login_blocked", "locked")
                connection.commit()
                raise PermissionError(
                    f"החשבון נעול עקב ניסיונות כושלים; נסה שוב בעוד עד {LOCKOUT_MINUTES} דקות")
            if not check_password_hash(row["password_hash"], password or ""):
                failed = row["failed_attempts"] + 1
                locked_until = None
                if failed >= MAX_FAILED_ATTEMPTS:
                    locked_until = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
                    failed = 0
                connection.execute(
                    "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE username = ?",
                    (failed, locked_until, username),
                )
                _log_event(connection, username, "login_failed",
                           "locked_out" if locked_until else f"attempt={failed}")
                connection.commit()
                raise generic
            connection.execute(
                "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE username = ?",
                (username,),
            )
            _log_event(connection, username, "login_ok")
            connection.commit()
            return {"username": row["username"], "role": row["role"],
                    "display_name": row["display_name"]}
        finally:
            connection.close()


def has_role(user_role, required_role):
    return _ROLE_RANK.get(user_role, -1) >= _ROLE_RANK.get(required_role, 99)


def list_users():
    connection = _connect()
    try:
        rows = connection.execute(
            """SELECT username, role, display_name, active, locked_until, created_at
               FROM users ORDER BY username"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def create_user(username, password, role, display_name):
    username = (username or "").strip().lower()
    if not username or not password or len(password) < 8:
        raise ValueError("נדרשים שם משתמש וסיסמה באורך 8 תווים לפחות")
    if role not in ROLES:
        raise ValueError(f"תפקיד לא מוכר; יש לבחור מתוך {', '.join(ROLES)}")
    with _LOCK:
        connection = _connect()
        try:
            exists = connection.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)
            ).fetchone()
            if exists:
                raise ValueError("שם המשתמש כבר קיים")
            connection.execute(
                """INSERT INTO users
                   (username, password_hash, role, display_name, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (username,
                 generate_password_hash(password, method="pbkdf2:sha256:600000"),
                 role, display_name or username,
                 datetime.now(timezone.utc).isoformat()),
            )
            _log_event(connection, username, "user_created", f"role={role}")
            connection.commit()
        finally:
            connection.close()
    return {"username": username, "role": role}


def auth_events(limit=100):
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT at, username, event, detail FROM auth_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def secret_key():
    """Stable Flask session key persisted next to the auth database."""
    key_path = AUTH_DATABASE.with_suffix(".key")
    if key_path.exists():
        return key_path.read_bytes()
    key = secrets.token_bytes(32)
    key_path.write_bytes(key)
    return key
