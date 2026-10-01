#!/usr/bin/env python3
"""Prune stale database records and truncate oversized log files."""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_DB_PATH = Path("data") / "gatekeeper.db"
DEFAULT_LOG_DIR = Path("logs")
DEFAULT_MAX_LOG_SIZE_MB = 50.0


def prune_db_records(db_path: str | Path, retention_days: int = 30) -> int:
    """Delete stale dwell records older than retention_days and vacuum the DB."""
    db_file = Path(db_path)
    if not db_file.exists():
        print(f"Database file not found at {db_file}")
        return 0

    cutoff = datetime.now() - timedelta(days=max(0, retention_days))
    cutoff_sql = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(str(db_file)) as conn:
        cursor = conn.cursor()
        table_names = [row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        table_name = None
        if "dwell_logs" in table_names:
            table_name = "dwell_logs"
        elif "carwash_audit" in table_names:
            table_name = "carwash_audit"

        if table_name is None:
            return 0

        columns = [row[1] for row in cursor.execute(f"PRAGMA table_info({table_name})")]
        if "entry_time" not in columns:
            return 0

        cursor.execute(f"DELETE FROM {table_name} WHERE entry_time < ?", (cutoff_sql,))
        deleted = int(cursor.rowcount)
        conn.commit()
        conn.execute("VACUUM")
        return deleted


def prune_logs(log_dir: str | Path, max_size_mb: float = DEFAULT_MAX_LOG_SIZE_MB) -> int:
    """Truncate any .log file larger than max_size_mb in log_dir."""
    log_root = Path(log_dir)
    if not log_root.exists():
        return 0

    max_bytes = max_size_mb * 1024 * 1024
    pruned = 0
    for log_file in sorted(log_root.rglob("*.log")):
        if not log_file.is_file():
            continue
        if log_file.stat().st_size <= max_bytes:
            continue
        with log_file.open("w", encoding="utf-8") as handle:
            handle.truncate(0)
        pruned += 1
    return pruned


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune stale dwell records and oversized logs for Gatekeeper.")
    parser.add_argument("--db-path", type=str, default=str(DEFAULT_DB_PATH), help="Path to SQLite DB")
    parser.add_argument("--retention-days", type=int, default=30, help="Days of dwell history to retain")
    parser.add_argument("--log-dir", type=str, default=str(DEFAULT_LOG_DIR), help="Directory containing log files")
    parser.add_argument("--max-log-size-mb", type=float, default=DEFAULT_MAX_LOG_SIZE_MB, help="Maximum log size to keep before truncation")
    args = parser.parse_args()

    deleted = prune_db_records(args.db_path, retention_days=args.retention_days)
    pruned_logs = prune_logs(args.log_dir, max_size_mb=args.max_log_size_mb)
    print(f"Deleted {deleted} stale records from {args.db_path}")
    print(f"Truncated {pruned_logs} oversized log file(s) in {args.log_dir}")


if __name__ == "__main__":
    main()
