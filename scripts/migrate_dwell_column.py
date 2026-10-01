"""Migration helper: convert `dwell_time_mins` -> `dwell_seconds`.

Searches for a target table (`wash_events` or `carwash_audit`), and if
`dwell_time_mins` exists, creates `dwell_seconds` (if missing) and migrates
values using `dwell_seconds = dwell_time_mins * 60`.

Usage:
    python scripts/migrate_dwell_column.py --db-path gatekeeper.db
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def find_target_table(conn: sqlite3.Connection, candidates=("wash_events", "carwash_audit")) -> Optional[str]:
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {row[0] for row in cursor.fetchall()}
    for name in candidates:
        if name in existing:
            return name
    return None


def table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cursor.fetchall()]
    return column in cols


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, column_sql: str) -> None:
    if not table_has_column(conn, table, column):
        cursor = conn.cursor()
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")
        conn.commit()
        logger.info("Added column %s to %s", column, table)


def migrate_dwell_column(db_path: str) -> Tuple[str, int]:
    """Perform migration on the DB and return (table_name, migrated_rows).

    Returns a tuple of the table used and the number of rows migrated.
    """
    db_file = Path(db_path)
    if not db_file.exists():
        raise FileNotFoundError(f"Database file not found: {db_path}")

    with closing(sqlite3.connect(str(db_file))) as conn:
        table = find_target_table(conn)
        if table is None:
            logger.info("No target tables found (checked wash_events, carwash_audit). Nothing to do.")
            return ("", 0)

        logger.info("Using table: %s", table)

        if not table_has_column(conn, table, "dwell_time_mins"):
            logger.info("Table %s has no dwell_time_mins column. Nothing to migrate.", table)
            return (table, 0)

        # Ensure dwell_seconds exists
        add_column_if_missing(conn, table, "dwell_seconds", "dwell_seconds REAL")

        # Count rows to migrate
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE dwell_time_mins IS NOT NULL")
        to_migrate = cursor.fetchone()[0] or 0

        if to_migrate == 0:
            logger.info("No rows with dwell_time_mins to migrate in %s", table)
            return (table, 0)

        # Perform migration: dwell_seconds = dwell_time_mins * 60
        cursor.execute(f"UPDATE {table} SET dwell_seconds = dwell_time_mins * 60 WHERE dwell_time_mins IS NOT NULL")
        conn.commit()

        logger.info("Migrated %d rows in %s (dwell_time_mins -> dwell_seconds)", to_migrate, table)
        return (table, int(to_migrate))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate dwell_time_mins -> dwell_seconds in SQLite DB")
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "gatekeeper.db"), help="SQLite DB path")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        table, migrated = migrate_dwell_column(args.db_path)
        if migrated:
            logger.info("Migration complete: %s rows migrated in table %s", migrated, table)
        else:
            logger.info("No migration necessary.")
        return 0
    except Exception as exc:
        logger.exception("Migration failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
