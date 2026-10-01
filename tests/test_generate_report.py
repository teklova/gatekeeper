import csv
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing

from scripts.generate_report import generate_daily_report, export_summary_report


class TestGenerateDailyReport(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name

        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE carwash_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gate_id TEXT,
                    vehicle_type TEXT,
                    entry_time TEXT,
                    exit_time TEXT,
                    dwell_seconds REAL
                )
                """
            )
            cursor.executemany(
                """
                INSERT INTO carwash_audit (gate_id, vehicle_type, entry_time, exit_time, dwell_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("Gate 1", "CAR", "2026-08-18 08:00:00", "2026-08-18 08:10:00", 600.0),
                    ("Gate 1", "CAR", "2026-08-18 09:00:00", "2026-08-18 09:15:00", 900.0),
                    ("Gate 1", "TRUCK", "2026-08-18 10:00:00", "2026-08-18 10:45:00", 2700.0),
                ],
            )
            conn.commit()

    def tearDown(self):
        if os.path.exists(self.db_path):
            for _ in range(20):
                try:
                    os.remove(self.db_path)
                    break
                except PermissionError:
                    time.sleep(0.1)

    def test_generate_daily_report_returns_summary(self):
        report = generate_daily_report(self.db_path)

        self.assertIn("2026-08-18", report)
        self.assertEqual(report["2026-08-18"]["total_vehicles"], 3)
        self.assertAlmostEqual(report["2026-08-18"]["avg_dwell_seconds"], 1400.0, places=2)
        self.assertEqual(report["2026-08-18"]["by_vehicle"]["CAR"]["count"], 2)
        self.assertAlmostEqual(report["2026-08-18"]["by_vehicle"]["CAR"]["avg_dwell_seconds"], 750.0, places=2)

    def test_export_summary_report_creates_json_and_csv(self):
        report = generate_daily_report(self.db_path)

        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = export_summary_report(report, temp_dir, format="json")
            csv_path = export_summary_report(report, temp_dir, format="csv")

            self.assertTrue(os.path.exists(json_path))
            self.assertTrue(os.path.exists(csv_path))

            with open(json_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
                self.assertEqual(payload["2026-08-18"]["total_vehicles"], 3)

            with open(csv_path, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["date"], "2026-08-18")
                self.assertEqual(rows[0]["vehicle_type"], "CAR")


if __name__ == "__main__":
    unittest.main()

