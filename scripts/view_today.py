import sys
import sqlite3
from pathlib import Path
from typing import List

# Ensure repo root is on sys.path so `gatekeeper` imports work when run from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "gatekeeper.db"

from gatekeeper.db import init_db


def fetch_today_rows(db_path: str = DB_PATH) -> List[tuple]:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, gate_id, vehicle_type, entry_time, exit_time, dwell_seconds, 
                   COALESCE(plate_number, '')
            FROM carwash_audit
            WHERE entry_time >= date('now', 'start of day')
            ORDER BY entry_time ASC
            """
        )
        return cursor.fetchall()


def render_table(rows: List[tuple]):
    if not rows:
        print("No activity recorded today.")
        return
    headers = ["ID", "Gate", "Type", "Entry", "Exit", "Dwell(s)", "Plate"]
    col_widths = [max(len(str(r[i])) for r in rows + [tuple(headers)]) for i in range(len(headers))]
    fmt = " | ".join(f"{{:{w}}}" for w in col_widths)
    sep = "-" * (sum(col_widths) + 3 * (len(headers) - 1))
    print(sep)
    print(fmt.format(*headers))
    print(sep)
    for row in rows:
        print(fmt.format(*[str(x) for x in row]))
    print(sep)


if __name__ == "__main__":
    # Ensure DB exists and migrations applied before querying
    init_db(DB_PATH)
    rows = fetch_today_rows()
    render_table(rows)


