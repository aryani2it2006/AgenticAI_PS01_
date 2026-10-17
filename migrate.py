"""Standalone database migration utility for SyncSoul AI.

Backs up the existing database before running safe, non-destructive migrations.
Usage:
    python migrate.py
"""
import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from coordinator import config, db


def run_migration():
    print(f"[migration] Target database: {config.DB_PATH}")
    backup_file = db.backup_db()
    if backup_file:
        print(f"[migration] [OK] Backup successfully created: {backup_file}")
    else:
        print("[migration] [INFO] No existing database found to back up. Initializing fresh schema.")

    db.init()
    print("[migration] [OK] Schema initialization and incremental migrations completed successfully.")
    print("[migration] Database is ready for SyncSoul AI!")


if __name__ == "__main__":
    run_migration()
