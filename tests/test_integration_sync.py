import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from gatekeeper.remote_sync import RemoteSyncWorker
from gatekeeper.tracker import Tracker


class TestIntegrationSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache_path = Path(self.temp_dir.name) / "remote_config.json"

    def test_config_update_propagates_to_tracker(self):
        tracker = Tracker(config_path="config.json")
        self.addCleanup(tracker.stop)
        worker = RemoteSyncWorker(cache_path=str(self.cache_path), fleet_url="https://fleet.example")
        worker.register_config_callback(tracker.apply_config_update)

        payload = {
            "yolo_confidence_threshold": 0.48,
            "dwell_threshold_seconds": 18,
            "roi_polygons": [[1, 2], [3, 2], [3, 4]],
            "active_cameras": ["cam-a"],
        }

        self.assertTrue(worker._config_callback(payload))
        self.assertEqual(tracker.confidence_threshold, 0.48)
        self.assertEqual(tracker.dwell_threshold_seconds, 18.0)
        self.assertEqual(tracker.active_cameras, ["cam-a"])
        worker.stop()

    def test_telemetry_supplier_exposes_runtime_metrics(self):
        tracker = Tracker(config_path="config.json")
        self.addCleanup(tracker.stop)
        tracker.current_fps = 14.5
        tracker.last_frame_timestamp = time.time()

        worker = RemoteSyncWorker(cache_path=str(self.cache_path), fleet_url="https://fleet.example")
        worker.register_telemetry_supplier(
            lambda: {
                "processing_fps": tracker.get_current_fps(),
                "frame_heartbeat_age_seconds": max(0.0, time.time() - tracker.get_last_frame_timestamp()),
                "queue_backlog": 3,
                "active_camera_state": "live",
            }
        )

        payload = worker.collect_telemetry()
        self.assertIn("runtime", payload)
        self.assertEqual(payload["runtime"]["processing_fps"], 14.5)
        self.assertEqual(payload["runtime"]["queue_backlog"], 3)
        worker.stop()

    def test_worker_shutdown_is_clean(self):
        worker = RemoteSyncWorker(cache_path=str(self.cache_path), fleet_url="https://fleet.example")
        worker.start()
        time.sleep(0.1)
        worker.stop()
        self.assertFalse(worker._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
