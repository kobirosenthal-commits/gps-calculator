"""Create a consistent backup snapshot for all CreditLens SQLite databases."""

from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from credit_auth import AUTH_DATABASE
from credit_compliance import COMPLIANCE_DATABASE
from credit_scoring import INDEX_DATABASE
from credit_validation import VALIDATION_DATABASE


def _sqlite_backup(source_path, destination_path):
    source = sqlite3.connect(source_path)
    try:
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def backup_all():
    root = ROOT
    backup_root = root / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root.mkdir(parents=True, exist_ok=True)

    dbs = {
        "credit_auth.sqlite3": Path(AUTH_DATABASE),
        "credit_compliance.sqlite3": Path(COMPLIANCE_DATABASE),
        "credit_index.sqlite3": Path(INDEX_DATABASE),
        "credit_validation.sqlite3": Path(VALIDATION_DATABASE),
    }

    copied = []
    for logical_name, source_path in dbs.items():
        if not source_path.exists():
            continue
        destination_path = backup_root / logical_name
        _sqlite_backup(source_path, destination_path)
        copied.append(logical_name)

    outcomes_file = root / "validation_data" / "loan_outcomes.csv"
    if outcomes_file.exists():
        shutil.copy2(outcomes_file, backup_root / outcomes_file.name)
        copied.append(outcomes_file.name)

    print(f"backup_dir={backup_root}")
    print(f"files_copied={len(copied)}")
    for item in copied:
        print(f"- {item}")


if __name__ == "__main__":
    backup_all()
