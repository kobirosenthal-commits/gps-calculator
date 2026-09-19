"""Compliance layer for CreditLens: consent registry, immutable decision
audit log, and retention policy.

Records are stored in a dedicated SQLite database so the audit trail is
separate from the operational index. In production this maps to a
write-once store with restricted access.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import sqlite3
import threading
import uuid

COMPLIANCE_DATABASE = Path(os.environ.get(
    "CREDIT_COMPLIANCE_DATABASE",
    Path(__file__).resolve().parent / "credit_compliance.sqlite3",
))

# Policy defaults — align with legal review before production.
CONSENT_VALIDITY_DAYS = 60        # Bank of Israel credit-data consent window
DECISION_RETENTION_DAYS = 7 * 365  # regulatory record keeping
CONSENT_RETENTION_DAYS = 7 * 365

_LOCK = threading.Lock()


def _connect():
    COMPLIANCE_DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(COMPLIANCE_DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS consents (
            consent_id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            operator TEXT NOT NULL,
            scope TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_consents_customer
            ON consents(customer_id, expires_at);
        CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY,
            decided_at TEXT NOT NULL,
            operator TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            customer_name TEXT,
            amount REAL NOT NULL,
            score REAL NOT NULL,
            decision TEXT NOT NULL,
            override INTEGER NOT NULL,
            model_version TEXT NOT NULL,
            consent_id TEXT NOT NULL,
            reason_codes TEXT NOT NULL,
            exceptions TEXT NOT NULL,
            hard_blocks TEXT NOT NULL,
            sources TEXT NOT NULL,
            breakdown TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_decisions_customer
            ON decisions(customer_id, decided_at);
    """)
    return connection


def open_connection():
    """Schema-initialized connection for analytics and audit exports."""
    return _connect()


def record_consent(customer_id, operator="demo-operator",
                   scope="credit-assessment"):
    """Register informed customer consent; returns the consent record."""
    now = datetime.now(timezone.utc)
    record = {
        "consent_id": f"CNS-{uuid.uuid4().hex[:12].upper()}",
        "customer_id": str(customer_id).strip(),
        "operator": operator,
        "scope": scope,
        "recorded_at": now.isoformat(),
        "expires_at": (now + timedelta(days=CONSENT_VALIDITY_DAYS)).isoformat(),
    }
    with _LOCK:
        connection = _connect()
        try:
            connection.execute(
                """INSERT INTO consents
                   (consent_id, customer_id, operator, scope, recorded_at, expires_at)
                   VALUES (:consent_id, :customer_id, :operator, :scope,
                           :recorded_at, :expires_at)""",
                record,
            )
            connection.commit()
        finally:
            connection.close()
    return record


def get_active_consent(customer_id):
    """Return the newest unexpired, unrevoked consent for the customer."""
    now = datetime.now(timezone.utc).isoformat()
    connection = _connect()
    try:
        row = connection.execute(
            """SELECT * FROM consents
               WHERE customer_id = ? AND revoked_at IS NULL AND expires_at > ?
               ORDER BY recorded_at DESC LIMIT 1""",
            (str(customer_id).strip(), now),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def record_decision(result, operator, consent_id):
    """Append the full decision snapshot to the immutable audit log."""
    decision_id = f"DEC-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
    row = (
        decision_id,
        datetime.now(timezone.utc).isoformat(),
        operator,
        result["customer"]["customer_id"],
        result["customer"]["name"],
        result["amount"],
        result["score"],
        result["decision"],
        1 if result["override"] else 0,
        result["model_version"],
        consent_id,
        json.dumps(result["reason_codes"], ensure_ascii=False),
        json.dumps(result["exceptions"], ensure_ascii=False),
        json.dumps(result["hard_blocks"], ensure_ascii=False),
        json.dumps(result["sources"], ensure_ascii=False),
        json.dumps(result["breakdown"], ensure_ascii=False),
    )
    with _LOCK:
        connection = _connect()
        try:
            connection.execute(
                """INSERT INTO decisions VALUES
                   (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )
            connection.commit()
        finally:
            connection.close()
    return decision_id


def get_decision(decision_id):
    """Full decision snapshot for the customer decision notice / audit view."""
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM decisions WHERE decision_id = ?",
            (str(decision_id).strip(),),
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        for field in ("reason_codes", "exceptions", "hard_blocks", "sources", "breakdown"):
            record[field] = json.loads(record[field])
        record["override"] = bool(record["override"])
        return record
    finally:
        connection.close()


def decision_history(customer_id=None, limit=50):
    """Recent decisions, optionally filtered by customer."""
    connection = _connect()
    try:
        if customer_id:
            rows = connection.execute(
                """SELECT decision_id, decided_at, operator, customer_id,
                          customer_name, amount, score, decision, override,
                          model_version, consent_id, reason_codes
                   FROM decisions WHERE customer_id = ?
                   ORDER BY decided_at DESC LIMIT ?""",
                (str(customer_id).strip(), limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT decision_id, decided_at, operator, customer_id,
                          customer_name, amount, score, decision, override,
                          model_version, consent_id, reason_codes
                   FROM decisions ORDER BY decided_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["reason_codes"] = json.loads(item["reason_codes"])
            item["override"] = bool(item["override"])
            history.append(item)
        return history
    finally:
        connection.close()


def purge_expired_records():
    """Apply the retention policy; returns counts of purged rows."""
    now = datetime.now(timezone.utc)
    decision_cutoff = (now - timedelta(days=DECISION_RETENTION_DAYS)).isoformat()
    consent_cutoff = (now - timedelta(days=CONSENT_RETENTION_DAYS)).isoformat()
    with _LOCK:
        connection = _connect()
        try:
            decisions = connection.execute(
                "DELETE FROM decisions WHERE decided_at < ?", (decision_cutoff,)
            ).rowcount
            consents = connection.execute(
                "DELETE FROM consents WHERE recorded_at < ?", (consent_cutoff,)
            ).rowcount
            connection.commit()
            return {"decisions_purged": decisions, "consents_purged": consents}
        finally:
            connection.close()


def compliance_policy():
    return {
        "consent_validity_days": CONSENT_VALIDITY_DAYS,
        "decision_retention_days": DECISION_RETENTION_DAYS,
        "consent_retention_days": CONSENT_RETENTION_DAYS,
        "prohibited_scoring_variables": [
            "גיל", "מגדר", "לאום", "דת", "מצב משפחתי", "כתובת מגורים",
        ],
        "notes": "נתוני בנק ישראל יתקבלו אך ורק דרך לשכת אשראי מורשית ובהסכמת לקוח.",
    }
