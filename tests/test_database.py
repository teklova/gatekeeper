import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime

from gatekeeper.db import (
    get_daily_vehicle_count,
    get_pending_audit_events,
    log_vehicle_exit,
    mark_audit_events_synced,
)
from gatekeeper.tracker import VehicleTracker


class TestCarwashDatabase(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            for _ in range(20):
                try:
                    os.remove(self.db_path)
                    break
                except PermissionError:
                    time.sleep(0.1)

    def _get_table_columns(self):
        cursor = self.conn.cursor()
        try:
            cursor.execute("PRAGMA table_info(carwash_audit)")
            return [row[1] for row in cursor.fetchall()]
        finally:
            cursor.close()

    def test_schema_columns_exist(self):
        tracker = VehicleTracker(config_path="config.json", db_path=self.db_path)
        self.addCleanup(tracker.stop)

        expected_columns = {"id", "tenant_id", "site_id", "gate_id", "vehicle_type", "entry_time", "exit_time", "dwell_seconds"}
        actual_columns = set(self._get_table_columns())

        self.assertTrue(
            expected_columns.issubset(actual_columns),
            f"Missing expected columns. Found: {actual_columns}",
        )

    def test_schema_migration_for_legacy_table(self):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE carwash_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gate_id TEXT,
                vehicle_type TEXT,
                entry_time TEXT,
                exit_time TEXT
            )
            """
        )
        self.conn.commit()

        tracker = VehicleTracker(config_path="config.json", db_path=self.db_path)
        self.addCleanup(tracker.stop)

        actual_columns = set(self._get_table_columns())
        self.assertIn("dwell_seconds", actual_columns, "Migration failed to add dwell_seconds column.")
        self.assertIn("tenant_id", actual_columns, "Migration failed to add tenant_id column.")
        self.assertIn("site_id", actual_columns, "Migration failed to add site_id column.")

    def test_audit_event_write_and_retrieval(self):
        tracker = VehicleTracker(config_path="config.json", db_path=self.db_path)
        self.addCleanup(tracker.stop)

        entry_time = datetime(2026, 8, 18, 17, 18, 0)
        exit_time = datetime(2026, 8, 18, 17, 18, 10)
        dwell_seconds = 10.256

        tracker._log_audit_event("CAR", entry_time, exit_time, dwell_seconds)

        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "SELECT gate_id, vehicle_type, entry_time, exit_time, dwell_seconds FROM carwash_audit"
            )
            row = cursor.fetchone()
        finally:
            cursor.close()

        self.assertIsNotNone(row, "No row inserted during audit log write.")
        self.assertEqual(row[0], "Gate 1")
        self.assertEqual(row[1], "CAR")
        self.assertEqual(row[2], "2026-08-18 17:18:00")
        self.assertEqual(row[3], "2026-08-18 17:18:10")
        self.assertEqual(row[4], 10.26)

    def test_daily_vehicle_count_matches_today_rows(self):
        tracker = VehicleTracker(config_path="config.json", db_path=self.db_path)
        self.addCleanup(tracker.stop)

        now = datetime.now()
        tracker._log_audit_event("CAR", now, now, 12.0)
        tracker._log_audit_event("SUV", now, now, 14.0)

        self.assertEqual(get_daily_vehicle_count(self.db_path), 2)

    def test_pending_audit_events_are_cleared_only_when_acknowledged(self):
        now = datetime.now()
        log_vehicle_exit("CAR", "Gate 1", now, now, 12.0, db_path=self.db_path)
        log_vehicle_exit("SUV", "Gate 2", now, now, 14.0, db_path=self.db_path)

        pending = get_pending_audit_events(limit=1, db_path=self.db_path)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["vehicle_type"], "CAR")
        self.assertEqual(mark_audit_events_synced([pending[0]["id"]], self.db_path), 1)
        self.assertEqual(mark_audit_events_synced([pending[0]["id"]], self.db_path), 0)

        remaining = get_pending_audit_events(limit=10, db_path=self.db_path)
        self.assertEqual([event["vehicle_type"] for event in remaining], ["SUV"])


if __name__ == "__main__":
    unittest.main()


