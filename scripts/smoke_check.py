"""Smoke checks for auth, scoring, readiness and role gates."""

import sys
import os
import requests

BASE_URL = os.environ.get("CREDIT_SMOKE_BASE_URL", "http://127.0.0.1:5000").rstrip("/")


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _login(session, username, password):
    response = session.post(
        f"{BASE_URL}/login",
        json={"username": username, "password": password},
        timeout=20,
    )
    _assert(response.status_code == 200, f"login failed for {username}: {response.text}")


def run():
    health = requests.get(f"{BASE_URL}/healthz", timeout=20)
    _assert(health.status_code == 200, f"health failed: {health.text}")

    ready = requests.get(f"{BASE_URL}/readyz", timeout=60)
    _assert(ready.status_code == 200, f"ready failed: {ready.text}")
    ready_payload = ready.json()
    _assert(ready_payload.get("ready") is True, "readyz returned ready=false")

    anon = requests.get(f"{BASE_URL}/api/validation/latest", timeout=20)
    _assert(anon.status_code in (401, 403), "anonymous access unexpectedly allowed")

    underwriter = requests.Session()
    _login(underwriter, "dan.k", "Loan!2026")
    customers = underwriter.get(f"{BASE_URL}/api/credit/customers?q=CUS-", timeout=30)
    _assert(customers.status_code == 200, f"customers endpoint failed: {customers.text}")
    items = customers.json().get("customers", [])
    _assert(len(items) > 0, "no customers returned from index")
    customer_id = items[0]["customer_id"]

    assessment = underwriter.post(
        f"{BASE_URL}/api/credit/assess",
        json={"customer_id": customer_id, "amount": 100000, "consent_given": True},
        timeout=60,
    )
    _assert(assessment.status_code == 200, f"assessment failed: {assessment.text}")
    decision = assessment.json()
    _assert("score" in decision and "decision_id" in decision, "assessment response missing keys")

    underwriter_validation = underwriter.get(f"{BASE_URL}/api/validation/latest", timeout=20)
    _assert(underwriter_validation.status_code == 403, "underwriter can access validation endpoint")

    underwriter_portfolio = underwriter.get(f"{BASE_URL}/api/credit/portfolio", timeout=30)
    _assert(underwriter_portfolio.status_code == 403, "underwriter can access portfolio report")

    letter = underwriter.get(f"{BASE_URL}/decision/{decision['decision_id']}", timeout=20)
    _assert(letter.status_code == 200, f"decision letter failed: {letter.status_code}")
    _assert(decision["decision_id"] in letter.text, "decision letter missing decision id")

    missing_letter = underwriter.get(f"{BASE_URL}/decision/DEC-DOES-NOT-EXIST", timeout=20)
    _assert(missing_letter.status_code == 404, "unknown decision letter did not return 404")

    risk_manager = requests.Session()
    _login(risk_manager, "rachel.r", "Risk!2026")
    validation = risk_manager.get(f"{BASE_URL}/api/validation/latest", timeout=30)
    _assert(validation.status_code == 200, f"risk manager validation failed: {validation.text}")

    report = risk_manager.get(f"{BASE_URL}/api/credit/portfolio?days=90", timeout=60)
    _assert(report.status_code == 200, f"portfolio report failed: {report.text}")
    totals = report.json().get("totals", {})
    _assert(totals.get("decisions", 0) > 0, "portfolio report has no decisions")

    export = risk_manager.get(f"{BASE_URL}/api/credit/decisions.csv?days=365", timeout=60)
    _assert(export.status_code == 200, f"csv export failed: {export.status_code}")
    _assert("decision_id" in export.text, "csv export missing header")

    batch = risk_manager.post(
        f"{BASE_URL}/api/credit/batch",
        json={"amount": 80000, "sample": 25},
        timeout=120,
    )
    _assert(batch.status_code == 200, f"batch prescreen failed: {batch.text}")
    batch_payload = batch.json()
    _assert(batch_payload.get("scored") == 25, "batch prescreen returned wrong count")

    named_batch = risk_manager.post(
        f"{BASE_URL}/api/credit/batch",
        json={"customer_ids": [customer_id], "amount": 50000},
        timeout=60,
    )
    _assert(named_batch.status_code == 200, f"named batch failed: {named_batch.text}")
    _assert(named_batch.json()["scored"] == 1, "named batch did not score the customer")

    admin = requests.Session()
    _login(admin, "admin", "Admin!2026")
    admin_page = admin.get(f"{BASE_URL}/admin", timeout=20)
    _assert(admin_page.status_code == 200, "admin page unavailable for admin")
    rm_admin_page = risk_manager.get(f"{BASE_URL}/admin", timeout=20)
    _assert(rm_admin_page.status_code == 403, "risk manager can open admin page")

    print("Smoke check passed.")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # keep failure explicit in CI/ops logs
        print(f"Smoke check failed: {exc}")
        sys.exit(1)
