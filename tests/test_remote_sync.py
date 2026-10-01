import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from hashlib import sha256
import hmac
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gatekeeper.db import get_pending_audit_events, log_vehicle_exit
from gatekeeper.remote_sync import RemoteSyncWorker
from gatekeeper.runner import GatekeeperRunner


class TestRemoteSyncWorker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache_path = Path(self.temp_dir.name) / "remote_config.json"
        self.db_path = str(Path(self.temp_dir.name) / "audit.db")

    @patch("gatekeeper.remote_sync.requests.post")
    @patch.dict(os.environ, {"DEVICE_SECRET": "test-signing-secret"}, clear=False)
    def test_audit_batch_is_marked_synced_after_successful_response(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.raise_for_status.return_value = None
        now = datetime.now()
        log_vehicle_exit("CAR", "Gate 1", now, now, 12.0, db_path=self.db_path)
        worker = RemoteSyncWorker(
            cache_path=str(self.cache_path),
            fleet_url="https://fleet.internal",
            db_path=self.db_path,
        )

        self.assertEqual(worker._flush_audit_queue(), 1)
        self.assertEqual(get_pending_audit_events(db_path=self.db_path), [])
        sent_payload = json.loads(mock_post.call_args.kwargs["data"])
        self.assertEqual(sent_payload["events"][0]["vehicle_type"], "CAR")
        self.assertTrue(mock_post.call_args.kwargs["headers"]["Idempotency-Key"])

    def test_collect_telemetry_uses_environment_device_id(self):
        with patch.dict(os.environ, {"GATEKEEPER_DEVICE_ID": "edge-42"}, clear=False):
            worker = RemoteSyncWorker(
                cache_path=str(self.cache_path),
                fleet_url="https://fleet.example",
                tenant_id="tenant-1",
                site_id="site-a",
            )
            telemetry = worker.collect_telemetry()

        self.assertEqual(telemetry["device_id"], "edge-42")
        self.assertEqual(telemetry["tenant_id"], "tenant-1")
        self.assertEqual(telemetry["site_id"], "site-a")
        self.assertEqual(telemetry["runtime"]["heartbeat"]["tenant_id"], "tenant-1")
        self.assertEqual(telemetry["runtime"]["heartbeat"]["site_id"], "site-a")
        self.assertIn("system", telemetry)
        self.assertIn("runtime", telemetry)

    def test_build_signed_headers_uses_device_secret(self):
        worker = RemoteSyncWorker(cache_path=str(self.cache_path), fleet_url="https://fleet.internal")
        body = '{"hello":"world"}'
        with patch.dict(os.environ, {"DEVICE_SECRET": "topsecret"}, clear=False):
            headers = worker._build_signed_headers(body, timestamp="1700000000", nonce="nonce-1")

        expected_signature = hmac.new(
            b"topsecret",
            b"1700000000{\"hello\":\"world\"}",
            sha256,
        ).hexdigest()
        self.assertEqual(headers["X-Gatekeeper-Timestamp"], "1700000000")
        self.assertEqual(headers["X-Gatekeeper-Nonce"], "nonce-1")
        self.assertEqual(headers["X-Gatekeeper-Signature"], expected_signature)

    @patch("gatekeeper.remote_sync.requests.post", side_effect=TimeoutError("network down"))
    @patch("gatekeeper.remote_sync.requests.get", side_effect=TimeoutError("network down"))
    def test_offline_fallback_uses_cached_config(self, _mock_get, _mock_post):
        self.cache_path.write_text(json.dumps({"yolo_confidence_threshold": 0.42, "dwell_threshold_seconds": 180}), encoding="utf-8")

        worker = RemoteSyncWorker(cache_path=str(self.cache_path), fleet_url="https://fleet.example")
        result = worker.sync_once()

        self.assertEqual(result["yolo_confidence_threshold"], 0.42)
        self.assertEqual(worker.latest_config["dwell_threshold_seconds"], 180)

    def test_backoff_is_capped_at_max_delay(self):
        worker = RemoteSyncWorker(cache_path=str(self.cache_path), fleet_url="https://fleet.example", max_backoff_seconds=300)
        self.assertEqual(worker._next_backoff_delay(0), 1)
        self.assertEqual(worker._next_backoff_delay(1), 2)
        self.assertEqual(worker._next_backoff_delay(5), 300)

    @patch("gatekeeper.remote_sync.requests.get")
    def test_invalid_config_payload_is_ignored(self, mock_get):
        self.cache_path.write_text(json.dumps({"yolo_confidence_threshold": 0.35, "dwell_threshold_seconds": 140}), encoding="utf-8")
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"unexpected": "payload"}

        worker = RemoteSyncWorker(cache_path=str(self.cache_path), fleet_url="https://fleet.example")
        result = worker.sync_once()

        self.assertEqual(result["yolo_confidence_threshold"], 0.35)
        self.assertEqual(worker.latest_config["dwell_threshold_seconds"], 140)

    @patch("gatekeeper.remote_sync.requests.get")
    @patch("gatekeeper.remote_sync.requests.post")
    def test_sync_once_generates_signed_headers_and_suspends_on_unpaid(self, mock_post, mock_get):
        mock_post.return_value.status_code = 200
        mock_post.return_value.raise_for_status.return_value = None
        mock_get.return_value.status_code = 200
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {
            "account_status": "unpaid",
            "config": {
                "yolo_confidence_threshold": 0.4,
                "dwell_threshold_seconds": 180,
                "roi_polygons": [[1, 2], [3, 4], [5, 6]],
                "active_cameras": ["cam-1"],
            },
        }

        with patch.dict(
            os.environ,
            {"DEVICE_SECRET": "signing-secret", "GATEKEEPER_DEVICE_ID": "edge-77"},
            clear=False,
        ):
            worker = RemoteSyncWorker(
                cache_path=str(self.cache_path),
                fleet_url="https://fleet.internal",
                tenant_id="tenant-77",
                site_id="site-9",
            )
            result = worker.sync_once()

        post_kwargs = mock_post.call_args.kwargs
        post_headers = post_kwargs["headers"]
        post_body = post_kwargs["data"]
        expected_signature = hmac.new(
            b"signing-secret",
            f"{post_headers['X-Gatekeeper-Timestamp']}{post_body}".encode("utf-8"),
            sha256,
        ).hexdigest()

        self.assertEqual(post_headers["Content-Type"], "application/json")
        self.assertEqual(post_headers["X-Gatekeeper-Signature"], expected_signature)
        self.assertTrue(post_headers["X-Gatekeeper-Nonce"])

        get_headers = mock_get.call_args.kwargs["headers"]
        self.assertIn("X-Gatekeeper-Timestamp", get_headers)
        self.assertIn("X-Gatekeeper-Nonce", get_headers)
        self.assertIn("X-Gatekeeper-Signature", get_headers)

        self.assertEqual(result["tenant_id"], "tenant-77")
        self.assertEqual(result["site_id"], "site-9")
        self.assertEqual(worker.control_plane_state["account_status"], "unpaid")
        self.assertTrue(worker.control_plane_state["alerts_suspended"])
        self.assertTrue(worker.control_plane_state["tracker_standby"])

    def test_runner_applies_suspension_state_to_notifier_and_tracker(self):
        runner = GatekeeperRunner(config_path="config.json")
        runner.notifier = SimpleNamespace(sms_enabled=True)

        standby_state = {"enabled": False}

        def set_standby_mode(value):
            standby_state["enabled"] = bool(value)

        runner.tracker = SimpleNamespace(set_standby_mode=set_standby_mode)

        runner._apply_control_plane_state(
            {
                "account_status": "suspended",
                "alerts_suspended": True,
                "tracker_standby": True,
            }
        )

        self.assertEqual(runner.control_plane_state["account_status"], "suspended")
        self.assertFalse(runner.notifier.sms_enabled)
        self.assertTrue(standby_state["enabled"])


if __name__ == "__main__":
    unittest.main()
