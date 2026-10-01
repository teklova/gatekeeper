#!/usr/bin/env python3
"""Provision a clean SQLite schema for gatekeeper."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

DB_DEFAULT = "data/gatekeeper.db"


def initialize_database(db_path: str = DB_DEFAULT, force: bool = False) -> str:
    """Create an empty production database with the required schema.

    Args:
        db_path: filesystem path to the SQLite database.
        force: if True, drop and recreate the table and any data.
    """
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(str(target))) as conn:
        cursor = conn.cursor()

        if force:
            cursor.execute("DROP TABLE IF EXISTS carwash_audit")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS carwash_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gate_id TEXT,
                vehicle_type TEXT,
                entry_time TEXT,
                exit_time TEXT,
                dwell_seconds REAL,
                sms_sent INTEGER DEFAULT 0,
                is_synced INTEGER DEFAULT 0,
                anomaly_type TEXT DEFAULT 'NORMAL',
                unpaid_flag INTEGER DEFAULT 1
            )
            """
        )

        columns = [row[1] for row in cursor.execute("PRAGMA table_info(carwash_audit)")]
        for column_name, column_sql in {
            "dwell_seconds": "ALTER TABLE carwash_audit ADD COLUMN dwell_seconds REAL",
            "sms_sent": "ALTER TABLE carwash_audit ADD COLUMN sms_sent INTEGER DEFAULT 0",
            "is_synced": "ALTER TABLE carwash_audit ADD COLUMN is_synced INTEGER DEFAULT 0",
            "anomaly_type": "ALTER TABLE carwash_audit ADD COLUMN anomaly_type TEXT DEFAULT 'NORMAL'",
            "unpaid_flag": "ALTER TABLE carwash_audit ADD COLUMN unpaid_flag INTEGER DEFAULT 1",
        }.items():
            if column_name not in columns:
                try:
                    cursor.execute(column_sql)
                except sqlite3.OperationalError:
                    continue

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_entry_gate ON carwash_audit(entry_time, gate_id)"
        )
        conn.commit()

    return str(target)


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the gatekeeper SQLite database.")
    parser.add_argument("--db-path", default=DB_DEFAULT, help="SQLite database path")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Drop existing tables and recreate the schema with no data.",
    )
    args = parser.parse_args()

    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger(__name__)

    database_path = initialize_database(args.db_path, force=args.force)
    logger.info("Database initialized: %s", database_path)


if __name__ == "__main__":
    main()


