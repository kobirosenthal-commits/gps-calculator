"""Demo credit decision engine.

Scale convention: risk score 1-10 where LOW is good (1-3 approve,
4-6 manual review / conditional, 7-10 decline). Component scores are
0-100 where HIGH is good; the final risk score inverts them.

The production version should replace the file scanner and
``request_bank_of_israel_credit_report`` with approved, authenticated
connectors. No real credit bureau data is queried by this demo.
"""

from datetime import datetime, timezone
from pathlib import Path
import csv
import json
import os
import sqlite3
import threading
import time

try:
    from openpyxl import load_workbook
except ImportError:  # CSV ingestion remains available without optional XLSX support.
    load_workbook = None


SOURCE_DIRECTORY = Path(os.environ.get(
    "CREDIT_SOURCE_DIRECTORY",
    Path(__file__).resolve().parent / "credit_data",
))

MODEL_VERSION = "2.1.0"  # 2.1.0: age removed from scoring (fair lending)
INDEX_DATABASE = Path(os.environ.get(
    "CREDIT_INDEX_DATABASE",
    Path(__file__).resolve().parent / "credit_index.sqlite3",
))


# ─── Field normalization ────────────────────────────────────────────────────
FIELD_ALIASES = {
    "customer_id": {"customer_id", "customerid", "id", "מספר לקוח", "תז", "תעודת זהות"},
    "name": {"name", "full_name", "שם", "שם לקוח", "שם מלא"},
    "age": {"age", "גיל"},
    "employment_status": {"employment_status", "סטטוס תעסוקה"},
    "employer": {"employer", "מעסיק", "מקום עבודה"},
    "months_at_employer": {"months_at_employer", "tenure_months", "ותק בחודשים"},
    "monthly_income": {"monthly_income", "income", "salary", "משכורת", "הכנסה חודשית"},
    "housing": {"housing", "דיור"},
    "average_balance": {"average_balance", "יתרה ממוצעת"},
    "overdraft_utilization": {"overdraft_utilization", "ניצול מסגרת עוש"},
    "returned_payments_12m": {"returned_payments_12m", "החזרות תשלום"},
    "open_accounts": {"open_accounts", "כרטיסים פתוחים"},
    "credit_utilization": {"credit_utilization", "utilization", "ניצול אשראי"},
    "on_time_payment_rate": {"on_time_payment_rate", "payment_rate", "עמידה בתשלומים"},
    "card_chargebacks_12m": {"card_chargebacks_12m", "החזרי חיוב"},
    "active_loans": {"active_loans", "loans", "הלוואות פעילות"},
    "monthly_debt_payment": {"monthly_debt_payment", "debt_payment", "החזר חודשי"},
    "late_payments_24m": {"late_payments_24m", "late_payments", "פיגורים"},
    "secured_debt": {"secured_debt", "חוב מובטח"},
    "monthly_expenses": {"monthly_expenses", "expenses", "הוצאות חודשיות"},
    "gambling_transactions_90d": {"gambling_transactions_90d", "עסקאות הימורים"},
    "income_verified": {"income_verified", "הכנסה מאומתת"},
    "identity_verified": {"identity_verified", "זהות מאומתת"},
    "aml_alert": {"aml_alert", "התראת הלבנת הון"},
    "documents_current": {"documents_current", "מסמכים בתוקף"},
}

_CANONICAL_LOOKUP = {}
for _field, _aliases in FIELD_ALIASES.items():
    for _alias in _aliases:
        _CANONICAL_LOOKUP[_alias.strip().lower().replace(" ", "_")] = _field

NUMERIC_FIELDS = {
    "age", "months_at_employer", "monthly_income", "average_balance",
    "overdraft_utilization", "returned_payments_12m", "open_accounts",
    "credit_utilization", "on_time_payment_rate", "card_chargebacks_12m",
    "active_loans", "monthly_debt_payment", "late_payments_24m",
    "secured_debt", "monthly_expenses", "gambling_transactions_90d",
}
BOOLEAN_FIELDS = {"income_verified", "identity_verified", "documents_current", "aml_alert"}
RATIO_FIELDS = {"credit_utilization", "on_time_payment_rate", "overdraft_utilization"}

# Profile defaults for fields absent from every scanned source.
PROFILE_DEFAULTS = {
    "name": None, "age": None, "employment_status": None, "employer": None,
    "housing": None, "months_at_employer": 0, "monthly_income": 0,
    "average_balance": 0, "overdraft_utilization": 0, "returned_payments_12m": 0,
    "open_accounts": 0, "credit_utilization": 0, "on_time_payment_rate": 1.0,
    "card_chargebacks_12m": 0, "active_loans": 0, "monthly_debt_payment": 0,
    "late_payments_24m": 0, "secured_debt": 0, "monthly_expenses": 0,
    "gambling_transactions_90d": 0, "income_verified": False,
    "identity_verified": False, "aml_alert": False, "documents_current": False,
}


def _canonical_field(header):
    return _CANONICAL_LOOKUP.get(str(header or "").strip().lower().replace(" ", "_"))


def _parse_number(value, default=0.0):
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "כן"}


# ─── Persistent source index ────────────────────────────────────────────────
# Excel is an ingestion format, not a query engine. Each changed workbook is
# parsed once into SQLite; loan requests then use indexed lookups rather than
# scanning millions of spreadsheet rows.
_INDEX_LOCK = threading.Lock()


def _read_rows(path):
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    if suffix in {".xlsx", ".xlsm"}:
        if load_workbook is None:
            raise ValueError("XLSX דורש התקנת openpyxl")
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = book.active
            rows_iter = sheet.iter_rows(values_only=True)
            headers = [str(v or "").strip() for v in next(rows_iter, ())]
            return [dict(zip(headers, row)) for row in rows_iter
                    if any(v is not None for v in row)]
        finally:
            book.close()
    raise ValueError(f"סוג קובץ לא נתמך: {suffix}")


def _source_paths():
    if not SOURCE_DIRECTORY.exists():
        return []
    return sorted(p for p in SOURCE_DIRECTORY.iterdir()
                  if p.is_file() and p.suffix.lower() in {".csv", ".xlsx", ".xlsm"})


def _connect_index():
    INDEX_DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(INDEX_DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS source_files (
            source_path TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            modified_ns INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            row_count INTEGER NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS customer_records (
            id INTEGER PRIMARY KEY,
            source_path TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            customer_id_norm TEXT NOT NULL,
            customer_name_norm TEXT NOT NULL,
            payload TEXT NOT NULL,
            FOREIGN KEY (source_path) REFERENCES source_files(source_path)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS ix_customer_records_id
            ON customer_records(customer_id_norm);
        CREATE INDEX IF NOT EXISTS ix_customer_records_name
            ON customer_records(customer_name_norm);
        CREATE INDEX IF NOT EXISTS ix_customer_records_source
            ON customer_records(source_path);
    """)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _normalize_row(row):
    normalized = {}
    for header, value in row.items():
        field = _canonical_field(header)
        if field and value is not None:
            normalized[field] = value
    return normalized


def _index_source(connection, path):
    rows = _read_rows(path)
    source_path = str(path.resolve())
    records = []
    for row_number, row in enumerate(rows, start=2):
        normalized = _normalize_row(row)
        customer_id = str(normalized.get("customer_id", "")).strip().casefold()
        customer_name = str(normalized.get("name", "")).strip().casefold()
        if not customer_id and not customer_name:
            continue
        records.append((
            source_path, row_number, customer_id, customer_name,
            json.dumps(normalized, ensure_ascii=False, default=str),
        ))
    stat = path.stat()
    connection.execute("DELETE FROM customer_records WHERE source_path = ?", (source_path,))
    connection.execute("DELETE FROM source_files WHERE source_path = ?", (source_path,))
    connection.execute(
        """INSERT INTO source_files
           (source_path, source_name, modified_ns, size_bytes, row_count, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (source_path, path.name, stat.st_mtime_ns, stat.st_size, len(rows),
         datetime.now(timezone.utc).isoformat()),
    )
    connection.executemany(
        """INSERT INTO customer_records
           (source_path, row_number, customer_id_norm, customer_name_norm, payload)
           VALUES (?, ?, ?, ?, ?)""",
        records,
    )
    return len(rows)


def refresh_source_index():
    """Incrementally index new or changed source files."""
    started = time.perf_counter()
    paths = _source_paths()
    with _INDEX_LOCK:
        connection = _connect_index()
        try:
            existing = {
                row["source_path"]: row
                for row in connection.execute("SELECT * FROM source_files")
            }
            current_paths = {str(path.resolve()) for path in paths}
            removed = set(existing) - current_paths
            for source_path in removed:
                connection.execute(
                    "DELETE FROM customer_records WHERE source_path = ?", (source_path,))
                connection.execute(
                    "DELETE FROM source_files WHERE source_path = ?", (source_path,))

            refreshed = 0
            for path in paths:
                source_path = str(path.resolve())
                stat = path.stat()
                indexed = existing.get(source_path)
                if (indexed and indexed["modified_ns"] == stat.st_mtime_ns
                        and indexed["size_bytes"] == stat.st_size):
                    continue
                _index_source(connection, path)
                refreshed += 1
            connection.commit()
            totals = connection.execute(
                "SELECT COUNT(*) AS files, COALESCE(SUM(row_count), 0) AS rows "
                "FROM source_files"
            ).fetchone()
            return {
                "files_scanned": totals["files"],
                "rows_scanned": totals["rows"],
                "refreshed_files": refreshed,
                "removed_files": len(removed),
                "refresh_ms": round((time.perf_counter() - started) * 1000),
            }
        finally:
            connection.close()


def _lookup_records(customer_key):
    requested = str(customer_key or "").strip().casefold()
    connection = _connect_index()
    try:
        id_rows = connection.execute(
            """SELECT r.*, s.source_name
               FROM customer_records r
               JOIN source_files s ON s.source_path = r.source_path
               WHERE r.customer_id_norm = ?
               ORDER BY s.source_name, r.row_number""",
            (requested,),
        ).fetchall()
        if id_rows:
            return id_rows

        identities = connection.execute(
            """SELECT DISTINCT customer_id_norm
               FROM customer_records
               WHERE customer_name_norm = ?""",
            (requested,),
        ).fetchall()
        if len(identities) > 1:
            raise LookupError(
                f"נמצאו כמה לקוחות בשם '{customer_key}'; יש לבחור לפי מספר לקוח")
        if not identities:
            return []
        customer_id = identities[0]["customer_id_norm"]
        if customer_id:
            return connection.execute(
                """SELECT r.*, s.source_name
                   FROM customer_records r
                   JOIN source_files s ON s.source_path = r.source_path
                   WHERE r.customer_id_norm = ?
                   ORDER BY s.source_name, r.row_number""",
                (customer_id,),
            ).fetchall()
        return connection.execute(
            """SELECT r.*, s.source_name
               FROM customer_records r
               JOIN source_files s ON s.source_path = r.source_path
               WHERE r.customer_name_norm = ?
               ORDER BY s.source_name, r.row_number""",
            (requested,),
        ).fetchall()
    finally:
        connection.close()


def build_customer_profile(customer_key):
    """Refresh changed sources, then retrieve one customer from the index."""
    requested = str(customer_key or "").strip().casefold()
    if not requested:
        raise LookupError("יש להזין שם לקוח או מספר לקוח")
    started = time.perf_counter()
    index_info = refresh_source_index()
    query_started = time.perf_counter()
    rows = _lookup_records(customer_key)
    query_ms = round((time.perf_counter() - query_started) * 1000, 2)
    if not rows:
        raise LookupError(f"הלקוח '{customer_key}' לא נמצא באף מאגר")

    profile = dict(PROFILE_DEFAULTS)
    sources = {}
    for indexed_row in rows:
        row = json.loads(indexed_row["payload"])
        source_name = indexed_row["source_name"]
        source = sources.setdefault(
            source_name,
            {"name": source_name, "rows": 0, "fields": 0, "status": "נמצא"},
        )
        source["rows"] += 1
        fields_taken = 0
        for field, value in row.items():
            if value is None or field == "customer_id":
                continue
            if field in BOOLEAN_FIELDS:
                profile[field] = _parse_bool(value)
            elif field in RATIO_FIELDS:
                parsed = _parse_number(value)
                profile[field] = parsed / 100 if parsed > 1 else parsed
            elif field in NUMERIC_FIELDS:
                profile[field] = _parse_number(value)
            else:
                profile[field] = str(value)
            fields_taken += 1
        source["fields"] += fields_taken

    matched_files = list(sources.values())
    scan_info = {
        "automatic": True,
        "index_mode": "persistent",
        "files_scanned": index_info["files_scanned"],
        "files_matched": len(matched_files),
        "rows_scanned": index_info["rows_scanned"],
        "records_retrieved": len(rows),
        "refreshed_files": index_info["refreshed_files"],
        "query_ms": query_ms,
        "scan_ms": round((time.perf_counter() - started) * 1000),
        "directory": str(SOURCE_DIRECTORY),
        "files": matched_files,
    }
    return profile, scan_info


def search_customers(query="", limit=50):
    """Search the live index by name or customer-ID prefix.
    With an empty query, returns the first customers by ID."""
    refresh_source_index()
    needle = str(query or "").strip().casefold()
    connection = _connect_index()
    try:
        if needle:
            rows = connection.execute(
                """SELECT customer_id_norm, customer_name_norm,
                          MAX(payload) AS payload
                   FROM customer_records
                   WHERE customer_id_norm LIKE ? OR customer_name_norm LIKE ?
                   GROUP BY customer_id_norm
                   ORDER BY customer_id_norm LIMIT ?""",
                (needle + "%", "%" + needle + "%", limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT customer_id_norm, customer_name_norm,
                          MAX(payload) AS payload
                   FROM customer_records
                   GROUP BY customer_id_norm
                   ORDER BY customer_id_norm LIMIT ?""",
                (limit,),
            ).fetchall()
        total = connection.execute(
            "SELECT COUNT(DISTINCT customer_id_norm) FROM customer_records"
        ).fetchone()[0]
        results = []
        for row in rows:
            payload = json.loads(row["payload"])
            results.append({
                "customer_id": str(payload.get("customer_id", row["customer_id_norm"])).strip(),
                "name": str(payload.get("name", row["customer_name_norm"])).strip(),
            })
        return {"customers": results, "total_customers": total}
    finally:
        connection.close()


# ─── Scoring ────────────────────────────────────────────────────────────────
# Weights aligned with common bureau models (FICO-style): payment history
# and capacity dominate; verification and behavioral signals refine.
CRITERIA = [
    ("היסטוריית תשלומים", "פיגורים, החזרות תשלום והחזרי חיוב", 30),
    ("יכולת החזר (DTI)", "יחס חוב להכנסה כולל ההלוואה החדשה", 22),
    ("ניצול אשראי", "ניצול מסגרות בכרטיסים ובעו\"ש", 14),
    ("יציבות תעסוקה והכנסה", "ותק, אימות הכנסה וגיל", 12),
    ("תזרים פנוי", "הכנסה פחות הוצאות והחזרי חוב", 10),
    ("אימות וזהות (KYC/AML)", "זהות, מסמכים והתראות הלבנת הון", 7),
    ("חשיפה והתנהגות", "הלוואות פעילות, חשבונות והימורים", 5),
]


def _component_scores(p, amount):
    income = max(p["monthly_income"], 1.0)
    # Estimated new monthly payment: 60-month loan at ~8% APR ≈ 2.03% of principal.
    new_payment = amount * 0.0203
    debt_after = p["monthly_debt_payment"] + new_payment
    dti = debt_after / income

    payment_history = (100
                       - p["late_payments_24m"] * 18
                       - p["returned_payments_12m"] * 22
                       - p["card_chargebacks_12m"] * 15)
    payment_history += (p["on_time_payment_rate"] - 0.9) * 100  # ±10 refinement

    capacity = 100 - dti * 175  # DTI 0.4 -> 30; DTI 0.57 -> 0

    utilization = 100 - (p["credit_utilization"] * 80 + p["overdraft_utilization"] * 40)

    # Fair lending: age and other protected attributes are intentionally
    # excluded from scoring. Stability relies on tenure and verification only.
    stability = min(70, p["months_at_employer"] * 1.4)
    stability += 30 if p["income_verified"] else 0

    free_cash = income - p["monthly_expenses"] - debt_after
    cash_flow = free_cash / income * 250  # 40% free cash -> 100

    kyc = 0
    kyc += 40 if p["identity_verified"] else 0
    kyc += 30 if p["documents_current"] else 0
    kyc += 30 if not p["aml_alert"] else 0

    exposure = (100
                - p["active_loans"] * 15
                - max(0, p["open_accounts"] - 2) * 8
                - p["gambling_transactions_90d"] * 12)

    clamp = lambda v: max(0.0, min(100.0, v))
    return {
        "היסטוריית תשלומים": clamp(payment_history),
        "יכולת החזר (DTI)": clamp(capacity),
        "ניצול אשראי": clamp(utilization),
        "יציבות תעסוקה והכנסה": clamp(stability),
        "תזרים פנוי": clamp(cash_flow),
        "אימות וזהות (KYC/AML)": clamp(kyc),
        "חשיפה והתנהגות": clamp(exposure),
    }


def request_bank_of_israel_credit_report(customer_id):
    """Return a traceable sandbox response until an approved connector exists."""
    return {
        "status": "ממתין לחיבור מאושר",
        "request_id": f"BOI-SANDBOX-{customer_id}",
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "message": "לא נשלחה בקשה אמיתית. יש לחבר ספק מורשה, הסכמה ואימות לפני הפעלה.",
    }


# Standardized adverse-action reason codes (duty to explain).
REASON_CODES = {
    "R01": "היסטוריית תשלומים חלשה — פיגורים או החזרות תשלום",
    "R02": "יחס חוב להכנסה גבוה ביחס לסכום המבוקש",
    "R03": "ניצול גבוה של מסגרות אשראי קיימות",
    "R04": "יציבות תעסוקה או הכנסה בלתי מספקת",
    "R05": "תזרים פנוי נמוך לאחר הוצאות והחזרי חוב",
    "R06": "אימות זהות או מסמכים חסרים",
    "R07": "חשיפה גבוהה — ריבוי הלוואות או התנהגות בסיכון",
    "R08": "התראת איסור הלבנת הון פתוחה",
    "R09": "לא דווחה הכנסה חודשית",
}

_COMPONENT_REASONS = [
    ("היסטוריית תשלומים", "R01", 55),
    ("יכולת החזר (DTI)", "R02", 50),
    ("ניצול אשראי", "R03", 45),
    ("יציבות תעסוקה והכנסה", "R04", 50),
    ("תזרים פנוי", "R05", 40),
    ("אימות וזהות (KYC/AML)", "R06", 65),
    ("חשיפה והתנהגות", "R07", 45),
]


def _derive_reason_codes(components, profile, dti):
    """Weakest-first standardized reason codes for the decision notice."""
    codes = []
    if profile["aml_alert"]:
        codes.append({"code": "R08", "reason": REASON_CODES["R08"]})
    if profile["monthly_income"] <= 0:
        codes.append({"code": "R09", "reason": REASON_CODES["R09"]})
    ranked = sorted(
        ((components[name], code) for name, code, threshold in _COMPONENT_REASONS
         if components[name] < threshold),
        key=lambda item: item[0],
    )
    for _value, code in ranked:
        if all(existing["code"] != code for existing in codes):
            codes.append({"code": code, "reason": REASON_CODES[code]})
    return codes[:4]


def _collect_exceptions(p, dti):
    exceptions = []
    if p["monthly_income"] <= 0:
        exceptions.append("אין הכנסה חודשית מדווחת — לא ניתן לחשב יחס חוב להכנסה")
    if 0 < p["months_at_employer"] < 12:
        exceptions.append(f"ותק תעסוקתי {int(p['months_at_employer'])} חודשים בלבד")
    if p["late_payments_24m"]:
        exceptions.append(f"{int(p['late_payments_24m'])} פיגורים ב-24 החודשים האחרונים")
    if p["returned_payments_12m"]:
        exceptions.append(f"{int(p['returned_payments_12m'])} החזרות תשלום ב-12 חודשים")
    if p["monthly_income"] > 0 and dti > 0.4:
        # Displayed ratios are capped: without an income figure the raw ratio
        # is meaningless and must never reach a customer notice.
        exceptions.append(
            f"יחס חוב להכנסה גבוה ({dti:.0%})" if dti <= 5
            else "יחס חוב להכנסה גבוה מאוד (מעל 500%)")
    if p["aml_alert"]:
        exceptions.append("קיימת התראת הלבנת הון פתוחה")
    if not p["documents_current"]:
        exceptions.append("מסמכי לקוח אינם בתוקף")
    return exceptions


def score_profile(profile, amount):
    """Pure scoring: profile + amount -> (risk_score, components).
    No I/O; used by both live assessment and bulk validation."""
    components = _component_scores(profile, amount)
    weighted_total = sum(components[name] * weight / 100
                         for name, _desc, weight in CRITERIA)
    risk_score = round(1 + (100 - weighted_total) * 0.09, 1)
    return risk_score, components


def _profile_from_payloads(payloads):
    """Merge normalized row payloads (dicts) into one typed profile."""
    profile = dict(PROFILE_DEFAULTS)
    for row in payloads:
        for field, value in row.items():
            if value is None or field == "customer_id":
                continue
            if field in BOOLEAN_FIELDS:
                profile[field] = _parse_bool(value)
            elif field in RATIO_FIELDS:
                parsed = _parse_number(value)
                profile[field] = parsed / 100 if parsed > 1 else parsed
            elif field in NUMERIC_FIELDS:
                profile[field] = _parse_number(value)
            else:
                profile[field] = str(value)
    return profile


def all_customer_profiles(limit=None):
    """Yield (customer_id, profile) for every indexed customer in one pass.
    Built for bulk validation — avoids 20,000 separate lookups."""
    refresh_source_index()
    connection = _connect_index()
    try:
        rows = connection.execute(
            """SELECT customer_id_norm, payload FROM customer_records
               WHERE customer_id_norm != ''
               ORDER BY customer_id_norm"""
        ).fetchall()
    finally:
        connection.close()
    current_id, bucket, produced = None, [], 0
    for row in rows:
        if row["customer_id_norm"] != current_id:
            if current_id is not None:
                yield current_id.upper(), _profile_from_payloads(bucket)
                produced += 1
                if limit and produced >= limit:
                    return
            current_id, bucket = row["customer_id_norm"], []
        bucket.append(json.loads(row["payload"]))
    if current_id is not None and (not limit or produced < limit):
        yield current_id.upper(), _profile_from_payloads(bucket)


def apply_policy(profile, risk_score, amount):
    """Single source of truth for the credit policy decision.

    Returns the decision, its colour, the DTI, the formal exceptions,
    hard compliance blocks and whether judgment override was applied.
    Used by both the single application flow and bulk portfolio runs.
    """
    income = max(profile["monthly_income"], 1.0)
    dti = (profile["monthly_debt_payment"] + amount * 0.0203) / income
    exceptions = _collect_exceptions(profile, dti)

    # Base decision strictly by risk band.
    if risk_score <= 3:
        decision, color = "אישור", "green"
    elif risk_score <= 6:
        decision, color = "בדיקה ידנית", "amber"
    else:
        decision, color = "דחייה", "red"

    # Judgment override: a young-file customer near the approval border with
    # strong verification, real verified income, sane DTI and a clean payment
    # record can be conditionally approved despite formal exceptions.
    # Never with AML alerts, zero income, or heavy debt loads.
    override = False
    if (exceptions and decision != "אישור" and risk_score <= 7.0
            and profile["identity_verified"] and not profile["aml_alert"]
            and profile["monthly_income"] > 0 and profile["income_verified"]
            and dti <= 0.5
            and profile["on_time_payment_rate"] >= 0.95
            and profile["late_payments_24m"] <= 1):
        override = True
        decision, color = "אישור מותנה", "amber"

    hard_blocks = []
    if profile["aml_alert"]:
        hard_blocks.append("התראת AML פתוחה — נדרש טיפול ציות לפני כל אישור")
        decision, color = "דחייה", "red"
        override = False

    return {
        "decision": decision,
        "color": color,
        "dti": dti,
        "exceptions": exceptions,
        "hard_blocks": hard_blocks,
        "override": override,
    }


def assess_application(customer_id="CUS-10482", amount=100000):
    amount = float(amount)
    if amount <= 0:
        raise ValueError("סכום ההלוואה חייב להיות גדול מאפס")
    profile, scan_info = build_customer_profile(customer_id)

    risk_score, components = score_profile(profile, amount)
    breakdown = []
    for name, description, weight in CRITERIA:
        value = round(components[name])
        breakdown.append({
            "name": name, "description": description, "weight": weight,
            "value": value, "contribution": round(value * weight / 100, 1),
        })

    policy = apply_policy(profile, risk_score, amount)
    decision, color = policy["decision"], policy["color"]
    dti, exceptions = policy["dti"], policy["exceptions"]
    hard_blocks, override = policy["hard_blocks"], policy["override"]

    return {
        "customer": {
            "customer_id": customer_id,
            "name": profile["name"] or str(customer_id),
            "employer": profile["employer"] or "לא ידוע",
            "age": profile["age"],  # display/KYC only — excluded from scoring
            "monthly_income": profile["monthly_income"],
        },
        "amount": amount,
        "score": risk_score,
        "score_scale": "1 (סיכון נמוך) עד 10 (סיכון גבוה)",
        "risk_band": "נמוך" if risk_score <= 3 else "בינוני" if risk_score <= 6 else "גבוה",
        "decision": decision,
        "decision_color": color,
        "dti": round(min(dti, 9.99), 3),
        "model_version": MODEL_VERSION,
        "reason_codes": _derive_reason_codes(components, profile, dti)
        if decision != "אישור" else [],
        "sources": scan_info["files"],
        "records_scanned": sum(f["fields"] for f in scan_info["files"]),
        "source_scan": {k: v for k, v in scan_info.items() if k != "files"},
        "breakdown": breakdown,
        "exceptions": exceptions,
        "hard_blocks": hard_blocks,
        "override": override,
        "override_note": ("המערכת הפעילה שיקול דעת: לקוח בתחילת דרכו עם אימות מלא "
                          "והיסטוריית תשלומים חזקה — אושר בתנאים למרות החריגות.")
        if override else None,
        "regulatory": request_bank_of_israel_credit_report(customer_id),
    }
