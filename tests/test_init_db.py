import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing

from scripts.init_db import initialize_database


class TestInitDatabase(unittest.TestCase):
    def test_initialize_database_creates_required_schema(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
            db_path = handle.name

        try:
            result = initialize_database(db_path)
            self.assertEqual(result, db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='carwash_audit'")
                self.assertIsNotNone(cursor.fetchone())
                columns = [row[1] for row in cursor.execute("PRAGMA table_info(carwash_audit)")]
                self.assertIn("gate_id", columns)
                self.assertIn("vehicle_type", columns)
                self.assertIn("dwell_seconds", columns)
        finally:
            if os.path.exists(db_path):
                for _ in range(20):
                    try:
                        os.remove(db_path)
                        break
                    except PermissionError:
                        time.sleep(0.1)

    def test_initialize_database_force_recreates_schema(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
            db_path = handle.name

        try:
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "CREATE TABLE carwash_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, gate_id TEXT)"
                )
                conn.execute(
                    "INSERT INTO carwash_audit (gate_id) VALUES ('placeholder')"
                )
                conn.commit()

            initialize_database(db_path, force=True)
            with closing(sqlite3.connect(db_path)) as conn:
                cursor = conn.cursor()
                row_count = cursor.execute("SELECT COUNT(*) FROM carwash_audit").fetchone()[0]
                self.assertEqual(row_count, 0)
        finally:
            if os.path.exists(db_path):
                for _ in range(20):
                    try:
                        os.remove(db_path)
                        break
                    except PermissionError:
                        time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()

