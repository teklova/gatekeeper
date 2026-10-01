import threading
import time
import unittest

import numpy as np

from gatekeeper.tracker import Tracker


class TestTrackerHotReload(unittest.TestCase):
    def test_valid_updates_apply_in_memory(self):
        tracker = Tracker(config_path="config.json")
        self.addCleanup(tracker.stop)
        original_confidence = tracker.confidence_threshold
        original_dwell = tracker.dwell_threshold_seconds

        updated = {
            "yolo_confidence_threshold": 0.42,
            "dwell_threshold_seconds": 15,
            "roi_polygons": [[10, 20], [30, 20], [30, 40]],
            "active_cameras": ["cam_1", "cam_2"],
        }

        self.assertTrue(tracker.apply_config_update(updated))
        self.assertEqual(tracker.confidence_threshold, 0.42)
        self.assertEqual(tracker.conf_threshold, 0.42)
        self.assertEqual(tracker.dwell_threshold_seconds, 15.0)
        self.assertEqual(tracker.dwell_threshold, 15.0)
        self.assertEqual(tracker.roi_polygons, [[10, 20], [30, 20], [30, 40]])
        self.assertEqual(tracker.active_cameras, ["cam_1", "cam_2"])

        tracker.apply_config_update({"yolo_confidence_threshold": original_confidence, "dwell_threshold_seconds": original_dwell})

    def test_invalid_values_are_rejected_without_mutation(self):
        tracker = Tracker(config_path="config.json")
        self.addCleanup(tracker.stop)
        original_confidence = tracker.confidence_threshold
        original_dwell = tracker.dwell_threshold_seconds
        original_roi = list(tracker.roi_polygons)

        invalid = {
            "yolo_confidence_threshold": 1.5,
            "dwell_threshold_seconds": -5,
            "roi_polygons": "not-a-list",
        }

        self.assertFalse(tracker.apply_config_update(invalid))
        self.assertEqual(tracker.confidence_threshold, original_confidence)
        self.assertEqual(tracker.dwell_threshold_seconds, original_dwell)
        self.assertEqual(tracker.roi_polygons, original_roi)

    def test_concurrent_updates_do_not_corrupt_runtime_state(self):
        tracker = Tracker(config_path="config.json")
        self.addCleanup(tracker.stop)
        threads = []

        def worker(value):
            for _ in range(25):
                tracker.apply_config_update({
                    "yolo_confidence_threshold": value,
                    "dwell_threshold_seconds": value * 10,
                })
                time.sleep(0.0005)

        for val in (0.3, 0.5, 0.7):
            t = threading.Thread(target=worker, args=(val,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertGreaterEqual(tracker.confidence_threshold, 0.0)
        self.assertLessEqual(tracker.confidence_threshold, 1.0)
        self.assertGreater(tracker.dwell_threshold_seconds, 0)

    def test_identical_frame_watchdog_flags_stream_freeze(self):
        tracker = Tracker(config_path="config.json")
        self.addCleanup(tracker.stop)
        frame = np.zeros((8, 8, 3), dtype=np.uint8)

        frozen = False
        for _ in range(tracker.freeze_frame_threshold):
            frozen = tracker._record_frame_signature(frame)

        self.assertTrue(frozen)
        self.assertEqual(tracker.freeze_events, 1)

    def test_stop_sets_event_and_clears_tracking_state(self):
        tracker = Tracker(config_path="config.json")
        tracker.active_tracks[1] = object()
        tracker._frame_signature_window.extend([1, 1, 1])

        tracker.stop()

        self.assertTrue(tracker._stop_event.is_set())
        self.assertEqual(len(tracker.active_tracks), 0)
        self.assertEqual(len(tracker._frame_signature_window), 0)


if __name__ == "__main__":
    unittest.main()
