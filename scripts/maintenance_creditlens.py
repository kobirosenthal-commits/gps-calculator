"""Run lightweight operational maintenance for CreditLens data stores."""

from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from credit_auth import AUTH_DATABASE
from credit_compliance import COMPLIANCE_DATABASE, purge_expired_records
from credit_scoring import INDEX_DATABASE
from credit_validation import VALIDATION_DATABASE


def _vacuum(database_path):
    path = Path(database_path)
    if not path.exists():
        return False
    connection = sqlite3.connect(path)
    try:
        connection.execute("VACUUM")
    finally:
        connection.close()
    return True


def run_maintenance():
    purge_stats = purge_expired_records()
    vacuumed = {
        "credit_auth.sqlite3": _vacuum(AUTH_DATABASE),
        "credit_compliance.sqlite3": _vacuum(COMPLIANCE_DATABASE),
        "credit_index.sqlite3": _vacuum(INDEX_DATABASE),
        "credit_validation.sqlite3": _vacuum(VALIDATION_DATABASE),
    }
    print("purge:", purge_stats)
    print("vacuum:", vacuumed)


if __name__ == "__main__":
    run_maintenance()
