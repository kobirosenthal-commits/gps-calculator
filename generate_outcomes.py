"""Generate a historical loan-outcomes file for model validation.

Simulates a loan book: a sample of customers took loans 24 months ago;
each either repaid (default=0) or defaulted (default=1). Default
probability is driven by real credit drivers taken from the customer's
profile (late payments, DTI, utilization, income stability) plus noise —
NOT by the model score itself, so validation is not circular.

Output: validation_data/loan_outcomes.csv
"""

from pathlib import Path
import csv
import math
import random

from credit_scoring import all_customer_profiles

OUT = Path(__file__).resolve().parent / "validation_data" / "loan_outcomes.csv"
SAMPLE = 6000
SEED = 2026


def default_probability(p, amount, rng):
    """Logistic model over true risk drivers + individual noise."""
    income = max(p["monthly_income"], 1.0)
    dti = (p["monthly_debt_payment"] + amount * 0.0203) / income
    z = -4.1
    z += 0.5 * p["late_payments_24m"]
    z += 0.8 * p["returned_payments_12m"]
    z += 2.0 * min(dti, 1.5)
    z += 1.2 * p["credit_utilization"]
    z += 0.7 * p["overdraft_utilization"]
    z -= 0.012 * min(p["months_at_employer"], 120)
    z -= 0.55 if p["income_verified"] else 0.0
    z += 1.0 if p["monthly_income"] <= 0 else 0.0
    z += 0.22 * p["gambling_transactions_90d"]
    z += rng.gauss(0, 0.45)  # unobserved borrower-specific factors
    return 1.0 / (1.0 + math.exp(-z))


def generate():
    rng = random.Random(SEED)
    OUT.parent.mkdir(exist_ok=True)
    rows = []
    for customer_id, profile in all_customer_profiles():
        if rng.random() > SAMPLE / 20000:
            continue
        amount = rng.choice([30000, 60000, 100000, 150000, 250000])
        pd_true = default_probability(profile, amount, rng)
        rows.append({
            "customer_id": customer_id.upper(),
            "loan_amount": amount,
            "defaulted": 1 if rng.random() < pd_true else 0,
        })
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["customer_id", "loan_amount", "defaulted"])
        writer.writeheader()
        writer.writerows(rows)
    defaults = sum(r["defaulted"] for r in rows)
    print(f"wrote {len(rows)} loans, {defaults} defaults "
          f"({defaults / len(rows):.1%}) -> {OUT}")


if __name__ == "__main__":
    generate()
