"""Model validation engine: discrimination, calibration and policy stats.

Joins historical loan outcomes with current customer profiles, scores
every loan with the production scoring function, and computes:
  - AUC / Gini / KS (discrimination)
  - decile table (default rate per score decile)
  - risk-band calibration (observed default rate per decision band)
Reports are persisted to credit_validation.sqlite3 for the audit trail.
"""

from datetime import datetime, timezone
from pathlib import Path
import csv
import json
import os
import sqlite3
import threading
import time
import uuid

from credit_scoring import (MODEL_VERSION, all_customer_profiles, score_profile)

OUTCOMES_FILE = Path(os.environ.get(
    "CREDIT_OUTCOMES_FILE",
    Path(__file__).resolve().parent / "validation_data" / "loan_outcomes.csv",
))
VALIDATION_DATABASE = Path(os.environ.get(
    "CREDIT_VALIDATION_DATABASE",
    Path(__file__).resolve().parent / "credit_validation.sqlite3",
))

_LOCK = threading.Lock()

# Acceptance thresholds (industry rules of thumb for consumer credit).
THRESHOLDS = {"auc_min": 0.70, "ks_min": 0.25}


def _connect():
    VALIDATION_DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(VALIDATION_DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS validation_runs (
            run_id TEXT PRIMARY KEY,
            ran_at TEXT NOT NULL,
            operator TEXT NOT NULL,
            model_version TEXT NOT NULL,
            loans INTEGER NOT NULL,
            defaults INTEGER NOT NULL,
            auc REAL NOT NULL,
            gini REAL NOT NULL,
            ks REAL NOT NULL,
            passed INTEGER NOT NULL,
            report TEXT NOT NULL
        );
    """)
    return connection


def _load_outcomes():
    if not OUTCOMES_FILE.exists():
        raise FileNotFoundError(
            "קובץ תוצאות הלוואות לא נמצא — יש להריץ generate_outcomes.py "
            f"או להגדיר CREDIT_OUTCOMES_FILE ({OUTCOMES_FILE})")
    with OUTCOMES_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        return [{"customer_id": row["customer_id"].strip().upper(),
                 "loan_amount": float(row["loan_amount"]),
                 "defaulted": int(row["defaulted"])}
                for row in csv.DictReader(f)]


def _auc_and_ks(scored):
    """AUC via rank statistic (ties averaged); KS over the score range.
    scored: list of (risk_score, defaulted)."""
    ranked = sorted(scored, key=lambda pair: pair[0])
    n = len(ranked)
    # Average ranks for tied scores
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    positives = sum(1 for _s, d in ranked if d == 1)
    negatives = n - positives
    if positives == 0 or negatives == 0:
        raise ValueError("בקובץ התוצאות אין גם כשלים וגם פירעונות — לא ניתן לתקף")
    rank_sum_pos = sum(rank for rank, (_s, d) in zip(ranks, ranked) if d == 1)
    # Higher score = higher risk; AUC of score as a default predictor.
    auc = (rank_sum_pos - positives * (positives + 1) / 2) / (positives * negatives)

    # KS: max gap between cumulative distributions of goods and bads.
    ks = 0.0
    cum_pos = cum_neg = 0
    idx = 0
    while idx < n:
        j = idx
        while j + 1 < n and ranked[j + 1][0] == ranked[idx][0]:
            j += 1
        for k in range(idx, j + 1):
            if ranked[k][1] == 1:
                cum_pos += 1
            else:
                cum_neg += 1
        ks = max(ks, abs(cum_pos / positives - cum_neg / negatives))
        idx = j + 1
    return auc, ks


def _decile_table(scored):
    ranked = sorted(scored, key=lambda pair: pair[0])
    n = len(ranked)
    table = []
    for decile in range(10):
        chunk = ranked[decile * n // 10:(decile + 1) * n // 10]
        if not chunk:
            continue
        defaults = sum(d for _s, d in chunk)
        table.append({
            "decile": decile + 1,
            "score_min": round(chunk[0][0], 1),
            "score_max": round(chunk[-1][0], 1),
            "loans": len(chunk),
            "defaults": defaults,
            "default_rate": round(defaults / len(chunk), 4),
        })
    return table


def _band_table(scored):
    bands = [("אישור (1-3)", 1, 3), ("בדיקה ידנית (3-6)", 3, 6),
             ("דחייה (6-10)", 6, 10.01)]
    table = []
    for label, low, high in bands:
        chunk = [(s, d) for s, d in scored if low <= s < high] if low > 1 else \
                [(s, d) for s, d in scored if s < high]
        if not chunk:
            table.append({"band": label, "loans": 0, "defaults": 0, "default_rate": None})
            continue
        defaults = sum(d for _s, d in chunk)
        table.append({"band": label, "loans": len(chunk), "defaults": defaults,
                      "default_rate": round(defaults / len(chunk), 4)})
    return table


def run_validation(operator="system"):
    """Score all historical loans with the production model and evaluate."""
    started = time.perf_counter()
    outcomes = _load_outcomes()
    profiles = dict(all_customer_profiles())
    scored = []
    missing = 0
    for loan in outcomes:
        profile = profiles.get(loan["customer_id"])
        if profile is None:
            missing += 1
            continue
        risk_score, _components = score_profile(profile, loan["loan_amount"])
        scored.append((risk_score, loan["defaulted"]))
    if len(scored) < 100:
        raise ValueError(f"נמצאו רק {len(scored)} הלוואות תואמות — אין די נתונים לתיקוף")

    auc, ks = _auc_and_ks(scored)
    gini = 2 * auc - 1
    defaults = sum(d for _s, d in scored)
    passed = auc >= THRESHOLDS["auc_min"] and ks >= THRESHOLDS["ks_min"]

    report = {
        "run_id": f"VAL-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "operator": operator,
        "model_version": MODEL_VERSION,
        "loans": len(scored),
        "defaults": defaults,
        "default_rate": round(defaults / len(scored), 4),
        "unmatched_loans": missing,
        "auc": round(auc, 4),
        "gini": round(gini, 4),
        "ks": round(ks, 4),
        "thresholds": THRESHOLDS,
        "passed": passed,
        "deciles": _decile_table(scored),
        "bands": _band_table(scored),
        "runtime_ms": round((time.perf_counter() - started) * 1000),
        "outcomes_file": str(OUTCOMES_FILE),
    }
    with _LOCK:
        connection = _connect()
        try:
            connection.execute(
                """INSERT INTO validation_runs
                   (run_id, ran_at, operator, model_version, loans, defaults,
                    auc, gini, ks, passed, report)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (report["run_id"], report["ran_at"], operator, MODEL_VERSION,
                 report["loans"], defaults, report["auc"], report["gini"],
                 report["ks"], 1 if passed else 0,
                 json.dumps(report, ensure_ascii=False)),
            )
            connection.commit()
        finally:
            connection.close()
    return report


def latest_report():
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT report FROM validation_runs ORDER BY ran_at DESC LIMIT 1"
        ).fetchone()
        return json.loads(row["report"]) if row else None
    finally:
        connection.close()


def validation_history(limit=20):
    connection = _connect()
    try:
        rows = connection.execute(
            """SELECT run_id, ran_at, operator, model_version, loans,
                      defaults, auc, gini, ks, passed
               FROM validation_runs ORDER BY ran_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) | {"passed": bool(row["passed"])} for row in rows]
    finally:
        connection.close()
