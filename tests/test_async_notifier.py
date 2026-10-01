"""
tests/test_async_notifier.py
Validates non-blocking alert queue behavior, retry backoff, and deduplication cooldown.
"""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from gatekeeper.notifier import SMSNotifier

SMS_RECIPIENT = "+10000000000"


class TestAsyncNotifier(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config = {
            "sandbox_mode": True,
            "sandbox_api_key": "fake_key",
            "dead_letter_path": os.path.join(self.temp_dir.name, "failed_alerts.json"),
            "africastalking": {
                "username": "sandbox",
                "recipient_phone": SMS_RECIPIENT,
                "api_key": "fake_key",
            },
            "sms_enabled": True,
            "alert_cooldown_seconds": 2,
        }

    @patch("gatekeeper.notifier.requests.post")
    def test_non_blocking_enqueue(self, mock_post):
        """Verify that enqueueing an alert returns immediately without blocking."""
        mock_post.return_value.status_code = 201
        mock_post.return_value.text = '{"SMSMessageData":{"Recipients":[{"status":"Success"}]}}'
        mock_post.return_value.json.return_value = {
            "SMSMessageData": {"Recipients": [{"status": "Success"}]}
        }

        notifier = SMSNotifier(config=self.config, dedupe_cooldown_seconds=2)
        self.addCleanup(notifier.shutdown)
        start_time = time.time()

        notifier.enqueue_alert(
            message="Vehicle #101 exceeded dwell threshold.",
            track_id=101,
            gate_id="Gate 1",
            alert_type="entry",
        )

        elapsed = time.time() - start_time
        self.assertLess(elapsed, 0.01)
        notifier.alert_queue.join()

    @patch("gatekeeper.notifier.requests.post")
    def test_deduplication_cooldown(self, mock_post):
        """Verify duplicate alert keys are suppressed within the cooldown window."""
        mock_post.return_value.status_code = 201
        mock_post.return_value.text = '{"SMSMessageData":{"Recipients":[{"status":"Success"}]}}'
        mock_post.return_value.json.return_value = {
            "SMSMessageData": {"Recipients": [{"status": "Success"}]}
        }

        notifier = SMSNotifier(config=self.config, dedupe_cooldown_seconds=2)
        self.addCleanup(notifier.shutdown)

        notifier.enqueue_alert(
            message="Vehicle #102 alert",
            track_id=102,
            gate_id="Gate 1",
            alert_type="entry",
        )
        notifier.enqueue_alert(
            message="Vehicle #102 alert duplicate",
            track_id=102,
            gate_id="Gate 1",
            alert_type="entry",
        )

        notifier.alert_queue.join()
        self.assertEqual(mock_post.call_count, 1)

    def test_queue_overflow_drops_oldest_low_priority_alert(self):
        notifier = SMSNotifier(config=self.config, dedupe_cooldown_seconds=2)
        notifier.shutdown()
        notifier.alert_queue.maxsize = 2

        notifier._enqueue_job({"alert_key": "old-entry", "alert_type": "entry", "message": "old"})
        notifier._enqueue_job({"alert_key": "new-entry", "alert_type": "entry", "message": "new"})

        accepted = notifier._enqueue_job({"alert_key": "critical-service", "alert_type": "service", "message": "critical"})

        queued_keys = [job["alert_key"] for job in list(notifier.alert_queue.queue)]
        self.assertTrue(accepted)
        self.assertNotIn("old-entry", queued_keys)
        self.assertIn("new-entry", queued_keys)
        self.assertIn("critical-service", queued_keys)

    def test_queue_overflow_persists_critical_alert_when_only_critical_remain(self):
        notifier = SMSNotifier(config=self.config, dedupe_cooldown_seconds=2)
        notifier.shutdown()
        notifier.alert_queue.maxsize = 1

        notifier._enqueue_job({"alert_key": "existing-critical", "alert_type": "service", "message": "existing"})
        accepted = notifier._enqueue_job({"alert_key": "failed-critical", "alert_type": "critical", "message": "persist me"})

        self.assertFalse(accepted)
        with open(self.config["dead_letter_path"], "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload[0]["alert_key"], "failed-critical")
        self.assertEqual(payload[0]["dead_letter_reason"], "queue_full")


if __name__ == "__main__":
    unittest.main()
