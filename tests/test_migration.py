import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing

from scripts.migrate_dwell_column import migrate_dwell_column


class TestDwellMigration(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name

    def tearDown(self):
        if os.path.exists(self.db_path):
            for _ in range(20):
                try:
                    os.remove(self.db_path)
                    break
                except PermissionError:
                    time.sleep(0.1)

    def test_migration_converts_values_in_carwash_audit(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE carwash_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gate_id TEXT,
                    vehicle_type TEXT,
                    entry_time TEXT,
                    exit_time TEXT,
                    dwell_time_mins REAL
                )
                """
            )
            cur.executemany(
                "INSERT INTO carwash_audit (gate_id, vehicle_type, entry_time, exit_time, dwell_time_mins) VALUES (?, ?, ?, ?, ?)",
                [
                    ("Gate A", "CAR", "2026-01-01 10:00:00", "2026-01-01 10:05:00", 5.0),
                    ("Gate A", "CAR", "2026-01-01 11:00:00", "2026-01-01 11:02:30", 2.5),
                    ("Gate A", "CAR", "2026-01-01 12:00:00", "2026-01-01 12:00:30", None),
                ],
            )
            conn.commit()

        table, migrated = migrate_dwell_column(self.db_path)
        self.assertEqual(table, "carwash_audit")
        self.assertEqual(migrated, 2)

        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(carwash_audit)")
            columns = [r[1] for r in cur.fetchall()]
            self.assertIn("dwell_seconds", columns)

            cur.execute("SELECT dwell_seconds FROM carwash_audit ORDER BY id")
            rows = [r[0] for r in cur.fetchall()]
            # First two rows migrated: 5.0min -> 300s, 2.5min -> 150s, third is None -> None
            self.assertAlmostEqual(rows[0], 300.0)
            self.assertAlmostEqual(rows[1], 150.0)
            self.assertIsNone(rows[2])

    def test_no_migration_when_no_dwell_time_column(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE carwash_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gate_id TEXT
                )
                """
            )
            conn.commit()

        table, migrated = migrate_dwell_column(self.db_path)
        self.assertEqual(table, "carwash_audit")
        self.assertEqual(migrated, 0)


if __name__ == "__main__":
    unittest.main()

