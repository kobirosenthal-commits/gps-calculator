"""Generate synthetic Excel source systems for the CreditLens demo.

Creates 20 XLSX files (6 topic systems + 14 supporting systems) with one
row per customer in each — 20,000 unique customers with realistic Hebrew
names. Profiles are randomized with a fixed seed so runs are reproducible:
mixed employment, income levels, payment histories, and a few risky
AML-flagged customers.
"""

from pathlib import Path
import random

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parent / "credit_data"
CUSTOMER_COUNT = 20000

FIRST_NAMES = [
    "יוסי", "דוד", "משה", "אבי", "איתי", "עומר", "נועם", "אריאל", "דניאל", "יונתן",
    "אורי", "עידו", "גיא", "רועי", "אלון", "תומר", "ניר", "שחר", "עמית", "ליאור",
    "שרה", "רחל", "מיכל", "נועה", "תמר", "יעל", "שירה", "ליאת", "הילה", "רוני",
    "מאיה", "עדי", "דנה", "אורית", "גלית", "ענת", "סיגל", "אפרת", "הדס", "טליה",
    "אברהם", "יצחק", "יעקב", "שלמה", "מרדכי", "חיים", "אליהו", "שמעון", "ראובן", "בנימין",
    "פאטמה", "מוחמד", "אחמד", "עלי", "יוסף", "מרים", "לינה", "נור", "סמירה", "ראמי",
]
LAST_NAMES = [
    "כהן", "לוי", "מזרחי", "פרץ", "ביטון", "דהן", "אברהם", "פרידמן", "מלכה", "אזולאי",
    "כץ", "יוסף", "דוד", "עמר", "אוחיון", "חדד", "גבאי", "בן דוד", "אדרי", "לוין",
    "שפירא", "גולן", "ברק", "אלבז", "אשכנזי", "בוזגלו", "נחום", "סבן", "רוזנברג", "וקנין",
    "חורי", "עבאס", "מנסור", "סרחאן", "זועבי", "גרינברג", "וייס", "הרשקוביץ", "בלום", "פלד",
]
EMPLOYERS = [
    "טכנולוגיות אלפא בע\"מ", "בנק הצפון", "רשת שופרסל", "מרכז רפואי הדסה",
    "חברת חשמל", "סטארטאפ QubeAI", "עיריית תל אביב", "אינטל ישראל",
    "מוסך האחים לוי", "משרד עו\"ד גולן ושות'", "חקלאות עמק חפר", "צה\"ל (קבע)",
    "אלביט מערכות", "טבע תעשיות", "בזק בינלאומי", "קופת חולים כללית",
    "רכבת ישראל", "נמל אשדוד", "מלון דן כרמל", "רשת פוקס",
]
STATUSES = ["מועסק", "מועסק", "מועסק", "מועסק", "עצמאי", "מובטל"]
HOUSING = ["שכירות", "בעלות", "בעלות", "גר עם משפחה"]


def _build_customers(rng):
    """20,000 unique (id, name) pairs. Name collisions get a numeric suffix."""
    customers = []
    seen_names = {}
    for index in range(CUSTOMER_COUNT):
        cid = f"CUS-{10000 + index}"
        base = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        count = seen_names.get(base, 0) + 1
        seen_names[base] = count
        name = base if count == 1 else f"{base} {count}"
        customers.append((cid, name))
    return customers


def _profile(rng):
    """One coherent random profile per customer."""
    status = rng.choice(STATUSES)
    employed = status != "מובטל"
    income = 0 if not employed else int(rng.triangular(5500, 45000, 14000))
    months = 0 if not employed else int(rng.triangular(1, 240, 30))
    age = rng.randint(21, 70)
    expenses = int(income * rng.uniform(0.35, 0.9)) if income else rng.randint(3000, 9000)
    late = rng.choices([0, 1, 2, 4, 7], weights=[55, 20, 12, 8, 5])[0]
    risky = rng.random() < 0.06
    p = {
        "age": age, "status": status,
        "employer": rng.choice(EMPLOYERS) if employed else "",
        "months": months, "income": income,
        "housing": rng.choice(HOUSING),
        "balance": int(rng.triangular(-8000, 90000, 9000)),
        "od_util": round(rng.uniform(0, 0.95), 2),
        "returned": rng.choices([0, 1, 3], weights=[80, 14, 6])[0],
        "cards": rng.randint(1, 6),
        "card_util": round(rng.uniform(0.02, 0.97), 2),
        "on_time": 1.0 if late == 0 else round(rng.uniform(0.62, 0.99), 2),
        "chargebacks": rng.choices([0, 1, 2], weights=[88, 9, 3])[0],
        "loans": rng.choices([0, 1, 2, 3, 5], weights=[30, 34, 22, 10, 4])[0],
        "late": late,
        "secured": rng.choices([0, 1], weights=[70, 30])[0],
        "expenses": expenses,
        "gambling": rng.choices([0, 2, 8], weights=[86, 10, 4])[0],
        "inc_verified": employed and rng.random() < 0.85,
        "id_verified": rng.random() < 0.96,
        "aml": risky and rng.random() < 0.5,
        "docs": rng.random() < 0.9,
    }
    p["debt_pay"] = 0 if p["loans"] == 0 else (
        int(max(300, p["income"] * rng.uniform(0.05, 0.4))) if p["income"]
        else rng.randint(500, 4000))
    return p


def generate():
    rng = random.Random(42)
    customers = _build_customers(rng)
    profiles = {cid: (name, _profile(rng)) for cid, name in customers}

    sources = {
        "01_crm_customers.xlsx": (
            ["customer_id", "name", "age", "employment_status", "employer",
             "months_at_employer", "monthly_income", "housing"],
            lambda c, n, p: [c, n, p["age"], p["status"], p["employer"],
                             p["months"], p["income"], p["housing"]],
        ),
        "02_accounts.xlsx": (
            ["customer_id", "name", "average_balance", "overdraft_utilization",
             "returned_payments_12m"],
            lambda c, n, p: [c, n, p["balance"], p["od_util"], p["returned"]],
        ),
        "03_cards.xlsx": (
            ["customer_id", "name", "open_accounts", "credit_utilization",
             "on_time_payment_rate", "card_chargebacks_12m"],
            lambda c, n, p: [c, n, p["cards"], p["card_util"], p["on_time"],
                             p["chargebacks"]],
        ),
        "04_loans.xlsx": (
            ["customer_id", "name", "active_loans", "monthly_debt_payment",
             "late_payments_24m", "secured_debt"],
            lambda c, n, p: [c, n, p["loans"], p["debt_pay"], p["late"], p["secured"]],
        ),
        "05_transactions.xlsx": (
            ["customer_id", "name", "monthly_expenses",
             "gambling_transactions_90d", "income_verified"],
            lambda c, n, p: [c, n, p["expenses"], p["gambling"], p["inc_verified"]],
        ),
        "06_kyc.xlsx": (
            ["customer_id", "name", "identity_verified", "aml_alert",
             "documents_current"],
            lambda c, n, p: [c, n, p["id_verified"], p["aml"], p["docs"]],
        ),
    }
    for number in range(7, 21):
        sources[f"{number:02d}_supporting_data_{number}.xlsx"] = (
            ["customer_id", "name", "record_type", "value", "updated_at"],
            lambda c, n, p, k=number: [c, n, f"קטגוריה {k}",
                                       rng.randint(0, 999), "2026-09-01"],
        )

    ROOT.mkdir(exist_ok=True)
    for filename, (headers, row_factory) in sources.items():
        book = Workbook(write_only=True)
        sheet = book.create_sheet("data")
        sheet.append(headers)
        for cid, (name, p) in profiles.items():
            sheet.append(row_factory(cid, name, p))
        book.save(ROOT / filename)
    print(f"Generated {len(sources)} files x {CUSTOMER_COUNT} rows in {ROOT}")


if __name__ == "__main__":
    generate()
