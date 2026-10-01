import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from gatekeeper.config_loader import get_config

DB_NAME = str(get_config().get("db_path") or "./data/gatekeeper.db")


@contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        yield conn
    finally:
        conn.close()


def ensure_database(db_path: str = DB_NAME) -> str:
    """Create the SQLite database file and initialize the production schema if needed."""
    target_path = Path(db_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with _connect(str(target_path)) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS carwash_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_id INTEGER,
                tenant_id TEXT,
                site_id TEXT,
                vehicle_type TEXT,
                plate_number TEXT,
                gate_id TEXT,
                entry_time TEXT,
                exit_time TEXT,
                dwell_seconds REAL,
                sms_sent INTEGER DEFAULT 0,
                is_synced INTEGER DEFAULT 0,
                anomaly_type TEXT DEFAULT 'NORMAL',
                unpaid_flag INTEGER DEFAULT 1
            );
            """
        )

        columns = [row[1] for row in cursor.execute("PRAGMA table_info(carwash_audit)")]
        if "gate_id" not in columns and "bay_id" in columns:
            cursor.execute("ALTER TABLE carwash_audit ADD COLUMN gate_id TEXT")
            cursor.execute("UPDATE carwash_audit SET gate_id = bay_id WHERE gate_id IS NULL")

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entry_gate
            ON carwash_audit(entry_time, gate_id);
            """
        )

        columns = [row[1] for row in cursor.execute("PRAGMA table_info(carwash_audit)")]
        for column_name, column_sql in {
            "tenant_id": "ALTER TABLE carwash_audit ADD COLUMN tenant_id TEXT",
            "site_id": "ALTER TABLE carwash_audit ADD COLUMN site_id TEXT",
            "dwell_seconds": "ALTER TABLE carwash_audit ADD COLUMN dwell_seconds REAL",
            "sms_sent": "ALTER TABLE carwash_audit ADD COLUMN sms_sent INTEGER DEFAULT 0",
            "is_synced": "ALTER TABLE carwash_audit ADD COLUMN is_synced INTEGER DEFAULT 0",
            "anomaly_type": "ALTER TABLE carwash_audit ADD COLUMN anomaly_type TEXT DEFAULT 'NORMAL'",
            "unpaid_flag": "ALTER TABLE carwash_audit ADD COLUMN unpaid_flag INTEGER DEFAULT 1",
            "plate_number": "ALTER TABLE carwash_audit ADD COLUMN plate_number TEXT",
            "gate_id": "ALTER TABLE carwash_audit ADD COLUMN gate_id TEXT",
        }.items():
            if column_name not in columns:
                try:
                    cursor.execute(column_sql)
                except sqlite3.OperationalError:
                    continue
        conn.commit()

    return str(target_path)


def init_db(db_path: str = DB_NAME) -> str:
    """Backward-compatible alias for database initialization."""
    return ensure_database(db_path)


def log_wash_event(
    vehicle_id: Optional[int],
    vehicle_type: str,
    gate_id: str,
    entry_dt: datetime,
    exit_dt: datetime,
    dwell_seconds: float,
    anomaly: str,
    sms_status: int,
    db_path: str = DB_NAME,
    plate_number: Optional[str] = None,
) -> None:
    """Logs a completed vehicle wash record using dwell_seconds."""
    ensure_database(db_path)
    config = get_config()
    tenant_id = str(config.get("tenant_id") or config.get("client_id") or "").strip() or None
    site_id = str(config.get("site_id") or "").strip() or None
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        # Insert including optional plate_number column when available
        cursor.execute(
            """
            INSERT INTO carwash_audit (
                vehicle_id, tenant_id, site_id, vehicle_type, plate_number, gate_id, entry_time, exit_time,
                dwell_seconds, sms_sent, is_synced, anomaly_type, unpaid_flag
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vehicle_id,
                tenant_id,
                site_id,
                vehicle_type,
                plate_number,
                gate_id,
                entry_dt.strftime("%Y-%m-%d %H:%M:%S"),
                exit_dt.strftime("%Y-%m-%d %H:%M:%S"),
                round(dwell_seconds, 2),
                int(sms_status),
                0,
                anomaly,
                1,
            ),
        )
        conn.commit()


def log_vehicle_exit(
    vehicle_type: str,
    gate_id: str,
    entry_dt: datetime,
    exit_dt: datetime,
    dwell_seconds: float,
    plate_number: Optional[str] = None,
    anomaly: str = "NORMAL",
    sms_status: int = 0,
    db_path: str = DB_NAME,
) -> None:
    """Convenience wrapper to log a vehicle exit including an optional plate number."""
    # vehicle_id is not known here; pass None
    log_wash_event(None, vehicle_type, gate_id, entry_dt, exit_dt, dwell_seconds, anomaly, sms_status, db_path, plate_number)


def get_daily_summary(date_str: str, db_path: str = DB_NAME):
    """Retrieves aggregated wash metrics for a specific date (YYYY-MM-DD)."""
    ensure_database(db_path)
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT vehicle_type, COUNT(*), AVG(dwell_seconds),
                   SUM(CASE WHEN anomaly_type != 'NORMAL' THEN 1 ELSE 0 END)
            FROM carwash_audit
            WHERE DATE(entry_time) = DATE(?)
            GROUP BY vehicle_type
            """,
            (date_str,),
        )
        return cursor.fetchall()


def get_daily_vehicle_count(db_path: str = DB_NAME) -> int:
    """Return the number of audit rows recorded today."""
    ensure_database(db_path)
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM carwash_audit
            WHERE DATE(entry_time) = DATE('now')
            """,
        )
        row = cursor.fetchone()
        return int(row[0] or 0) if row else 0


def get_pending_audit_events(limit: int = 100, db_path: str = DB_NAME) -> list[dict]:
    """Return a bounded batch of audit rows that have not been acknowledged remotely."""
    ensure_database(db_path)
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, vehicle_id, tenant_id, site_id, vehicle_type, plate_number,
                   gate_id, entry_time, exit_time, dwell_seconds, anomaly_type, unpaid_flag
            FROM carwash_audit
            WHERE is_synced = 0
            ORDER BY id
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_audit_events_synced(event_ids: list[int], db_path: str = DB_NAME) -> int:
    """Mark acknowledged audit rows synced and return the number of affected rows."""
    if not event_ids:
        return 0

    ensure_database(db_path)
    with _connect(db_path) as conn:
        cursor = conn.executemany(
            "UPDATE carwash_audit SET is_synced = 1 WHERE id = ? AND is_synced = 0",
            [(int(event_id),) for event_id in event_ids],
        )
        conn.commit()
        return cursor.rowcount

