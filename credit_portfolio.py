"""Portfolio layer for CreditLens: management reporting and bulk runs.

Two capabilities the credit company needs beyond a single application:

1. Portfolio reporting — aggregates the immutable decision audit log into
   the numbers a risk committee reviews: volume, approval mix, override
   rate, score distribution, top adverse-action reasons and operator load.
2. Bulk pre-screening — scores many customers in one pass (book review,
   pre-approved campaigns). Pre-screening is an internal risk measurement,
   not a credit decision: nothing is written to the decision log and no
   customer-facing decision is produced.
"""

from datetime import datetime, timedelta, timezone
import csv
import io
import json
import time

from credit_compliance import open_connection
from credit_scoring import (MODEL_VERSION, all_customer_profiles, apply_policy,
                            build_customer_profile, score_profile)

MAX_BATCH = 500
MAX_SAMPLE = 5000


def _window_start(days):
    days = max(1, min(int(days), 3650))
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(), days


def portfolio_summary(days=90):
    """Management report over the decision audit log for the given window."""
    started = time.perf_counter()
    since, days = _window_start(days)
    connection = open_connection()
    try:
        rows = connection.execute(
            """SELECT decided_at, operator, customer_id, amount, score,
                      decision, override, reason_codes
               FROM decisions WHERE decided_at >= ?
               ORDER BY decided_at DESC""",
            (since,),
        ).fetchall()
        total_ever = connection.execute(
            "SELECT COUNT(*) AS c FROM decisions").fetchone()["c"]
        consents = connection.execute(
            "SELECT COUNT(*) AS c FROM consents WHERE recorded_at >= ?",
            (since,)).fetchone()["c"]
    finally:
        connection.close()

    decisions = [dict(row) for row in rows]
    total = len(decisions)
    by_decision, reasons, operators, daily = {}, {}, {}, {}
    histogram = {band: 0 for band in range(1, 11)}
    approved_exposure = requested_exposure = 0.0
    overrides = 0
    score_sum = 0.0

    for item in decisions:
        by_decision[item["decision"]] = by_decision.get(item["decision"], 0) + 1
        score_sum += item["score"]
        histogram[min(10, max(1, int(round(item["score"]))))] += 1
        requested_exposure += item["amount"]
        if item["decision"] in ("אישור", "אישור מותנה"):
            approved_exposure += item["amount"]
        if item["override"]:
            overrides += 1
        for entry in json.loads(item["reason_codes"]):
            key = (entry["code"], entry["reason"])
            reasons[key] = reasons.get(key, 0) + 1
        operators[item["operator"]] = operators.get(item["operator"], 0) + 1
        day = item["decided_at"][:10]
        daily[day] = daily.get(day, 0) + 1

    approvals = by_decision.get("אישור", 0) + by_decision.get("אישור מותנה", 0)
    ratio = (lambda count: round(count / total, 4)) if total else (lambda count: 0.0)

    return {
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "totals": {
            "decisions": total,
            "decisions_all_time": total_ever,
            "consents_recorded": consents,
            "unique_customers": len({item["customer_id"] for item in decisions}),
            "average_score": round(score_sum / total, 2) if total else 0.0,
            "approval_rate": ratio(approvals),
            "override_rate": ratio(overrides),
            "overrides": overrides,
            "requested_exposure": round(requested_exposure),
            "approved_exposure": round(approved_exposure),
        },
        "by_decision": [
            {"decision": name, "count": count, "share": ratio(count)}
            for name, count in sorted(by_decision.items(), key=lambda kv: -kv[1])
        ],
        "score_histogram": [
            {"score": band, "count": count} for band, count in histogram.items()
        ],
        "top_reasons": [
            {"code": code, "reason": reason, "count": count, "share": ratio(count)}
            for (code, reason), count in sorted(reasons.items(), key=lambda kv: -kv[1])[:6]
        ],
        "operators": [
            {"operator": name, "decisions": count}
            for name, count in sorted(operators.items(), key=lambda kv: -kv[1])[:8]
        ],
        "daily": [
            {"date": day, "count": count}
            for day, count in sorted(daily.items())[-14:]
        ],
        "runtime_ms": round((time.perf_counter() - started) * 1000),
    }


def decisions_csv(customer_id=None, days=365, limit=5000):
    """Audit-ready CSV export of the decision log."""
    since, _days = _window_start(days)
    limit = max(1, min(int(limit), 50000))
    connection = open_connection()
    try:
        if customer_id:
            rows = connection.execute(
                """SELECT decision_id, decided_at, operator, customer_id,
                          customer_name, amount, score, decision, override,
                          model_version, consent_id, reason_codes
                   FROM decisions WHERE decided_at >= ? AND customer_id = ?
                   ORDER BY decided_at DESC LIMIT ?""",
                (since, str(customer_id).strip(), limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT decision_id, decided_at, operator, customer_id,
                          customer_name, amount, score, decision, override,
                          model_version, consent_id, reason_codes
                   FROM decisions WHERE decided_at >= ?
                   ORDER BY decided_at DESC LIMIT ?""",
                (since, limit),
            ).fetchall()
    finally:
        connection.close()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "decision_id", "decided_at", "operator", "customer_id", "customer_name",
        "amount", "score", "decision", "override", "model_version",
        "consent_id", "reason_codes",
    ])
    for row in rows:
        codes = "; ".join(f"{item['code']}" for item in json.loads(row["reason_codes"]))
        writer.writerow([
            row["decision_id"], row["decided_at"], row["operator"],
            row["customer_id"], row["customer_name"], row["amount"], row["score"],
            row["decision"], "yes" if row["override"] else "no",
            row["model_version"], row["consent_id"], codes,
        ])
    return buffer.getvalue()


def _prescreen_row(customer_id, profile, amount):
    risk_score, _components = score_profile(profile, amount)
    policy = apply_policy(profile, risk_score, amount)
    return {
        "customer_id": customer_id,
        "name": profile["name"] or customer_id,
        "score": risk_score,
        "risk_band": "נמוך" if risk_score <= 3 else "בינוני" if risk_score <= 6 else "גבוה",
        "decision": policy["decision"],
        "decision_color": policy["color"],
        "override": policy["override"],
        "dti": round(min(policy["dti"], 9.99), 3),
        "exceptions": len(policy["exceptions"]),
        "hard_blocks": len(policy["hard_blocks"]),
        "monthly_income": profile["monthly_income"],
    }


def batch_prescreen(customer_ids=None, amount=100000, sample=200):
    """Bulk risk pre-screen. Internal measurement only — never logged as a
    credit decision and never shown to the customer as an answer."""
    amount = float(amount)
    if amount <= 0:
        raise ValueError("סכום ההלוואה חייב להיות גדול מאפס")
    started = time.perf_counter()
    results, not_found = [], []

    if customer_ids:
        cleaned = [str(item).strip() for item in customer_ids if str(item).strip()]
        if len(cleaned) > MAX_BATCH:
            raise ValueError(f"ניתן לעבד עד {MAX_BATCH} לקוחות בבת אחת")
        for customer_id in cleaned:
            try:
                profile, _scan = build_customer_profile(customer_id)
            except LookupError:
                not_found.append(customer_id)
                continue
            results.append(_prescreen_row(customer_id, profile, amount))
        mode = "רשימת לקוחות"
    else:
        sample = max(1, min(int(sample), MAX_SAMPLE))
        for customer_id, profile in all_customer_profiles(limit=sample):
            results.append(_prescreen_row(customer_id, profile, amount))
        mode = "סקירת תיק"

    total = len(results)
    buckets = {}
    for row in results:
        buckets[row["decision"]] = buckets.get(row["decision"], 0) + 1
    approvals = buckets.get("אישור", 0) + buckets.get("אישור מותנה", 0)

    results.sort(key=lambda row: row["score"])
    return {
        "mode": mode,
        "amount": amount,
        "model_version": MODEL_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scored": total,
        "not_found": not_found,
        "summary": {
            "average_score": round(sum(row["score"] for row in results) / total, 2) if total else 0.0,
            "approval_rate": round(approvals / total, 4) if total else 0.0,
            "overrides": sum(1 for row in results if row["override"]),
            "blocked": sum(1 for row in results if row["hard_blocks"]),
            "by_decision": [
                {"decision": name, "count": count,
                 "share": round(count / total, 4) if total else 0.0}
                for name, count in sorted(buckets.items(), key=lambda kv: -kv[1])
            ],
            "potential_exposure": round(approvals * amount),
        },
        "results": results,
        "runtime_ms": round((time.perf_counter() - started) * 1000),
        "disclaimer": ("סקירה פנימית לניהול סיכונים בלבד — אינה החלטת אשראי, "
                       "אינה נרשמת ביומן ההחלטות ואינה מהווה תשובה ללקוח."),
    }
