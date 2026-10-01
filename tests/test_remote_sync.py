import hmac
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

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
        checkin_response = SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {
                "access_token": "node-jwt",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "subscription_state": "active",
            },
        )
        sync_response = SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"received": 1, "duplicates": 0},
        )
        mock_post.side_effect = [checkin_response, sync_response]
        now = datetime.now(UTC)
        log_vehicle_exit("CAR", "Gate 1", now, now, 12.0, db_path=self.db_path)
        worker = RemoteSyncWorker(
            cache_path=str(self.cache_path),
            fleet_url="https://fleet.internal",
            db_path=self.db_path,
        )

        self.assertEqual(worker._flush_audit_queue(), 1)
        self.assertEqual(get_pending_audit_events(db_path=self.db_path), [])
        sent_kwargs = mock_post.call_args_list[1].kwargs
        sent_payload = json.loads(sent_kwargs["data"])
        self.assertEqual(sent_payload["logs"][0]["details"]["vehicle_type"], "CAR")
        self.assertEqual(
            mock_post.call_args_list[1].args[0],
            "https://fleet.internal/api/v1/nodes/logs/sync",
        )
        self.assertTrue(sent_kwargs["headers"]["Idempotency-Key"])
        self.assertEqual(sent_kwargs["headers"]["Authorization"], "Bearer node-jwt")

    def test_incomplete_or_redirected_ack_keeps_audit_rows_pending(self):
        now = datetime.now(UTC)
        log_vehicle_exit("CAR", "Gate 1", now, now, 12.0, db_path=self.db_path)
        checkin = SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {
                "access_token": "node-jwt",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "subscription_state": "active",
            },
        )

        for status_code, acknowledgment in (
            (302, {"received": 1, "duplicates": 0}),
            (200, {"received": 0, "duplicates": 0}),
            (200, {"received": True, "duplicates": 0}),
        ):
            worker = RemoteSyncWorker(
                cache_path=str(self.cache_path),
                fleet_url="https://fleet.internal",
                db_path=self.db_path,
            )
            worker.hardware_token = "test-signing-secret"
            sync_response = SimpleNamespace(
                status_code=status_code,
                raise_for_status=lambda: None,
                json=lambda acknowledgment=acknowledgment: acknowledgment,
            )
            with (
                patch.dict(os.environ, {"DEVICE_SECRET": "test-signing-secret"}),
                patch(
                    "gatekeeper.remote_sync.requests.post",
                    side_effect=[checkin, sync_response],
                ) as mock_post,
                self.assertRaises(requests.HTTPError),
            ):
                worker._flush_audit_queue()
            self.assertFalse(mock_post.call_args_list[1].kwargs["allow_redirects"])
            self.assertEqual(len(get_pending_audit_events(db_path=self.db_path)), 1)

    def test_forbidden_cloud_response_clears_cached_token_and_enters_standby(self):
        observed_states = []
        worker = RemoteSyncWorker(
            cache_path=str(self.cache_path),
            fleet_url="https://fleet.internal",
            config_callback=lambda _config: observed_states.append(
                dict(worker.control_plane_state)
            ),
        )
        worker._access_token = "revoked-jwt"
        worker._access_token_expires_at = datetime.now(timezone.utc).timestamp() + 600

        def raise_forbidden():
            raise requests.HTTPError("forbidden")

        response = SimpleNamespace(status_code=403, raise_for_status=raise_forbidden)
        with self.assertRaises(requests.HTTPError):
            worker._raise_for_status(response)

        self.assertEqual(worker._access_token, "")
        self.assertEqual(worker._access_token_expires_at, 0.0)
        self.assertEqual(worker.account_status, "suspended")
        self.assertTrue(worker.control_plane_state["tracker_standby"])
        self.assertTrue(observed_states[-1]["alerts_suspended"])

    def test_http_cloud_url_is_rejected_before_using_cached_token(self):
        worker = RemoteSyncWorker(
            cache_path=str(self.cache_path), fleet_url="http://cloud.internal"
        )
        worker.hardware_token = "hardware-token"
        worker._access_token = "cached-jwt"
        worker._access_token_expires_at = datetime.now(timezone.utc).timestamp() + 600

        with (
            patch("gatekeeper.remote_sync.requests.post") as mock_post,
            self.assertRaises(PermissionError),
        ):
            worker.authenticate()

        mock_post.assert_not_called()

    def test_network_threshold_enters_standby_and_recovers_after_contact(self):
        observed_states = []
        worker = RemoteSyncWorker(
            cache_path=str(self.cache_path),
            fleet_url="https://fleet.internal",
            config_callback=lambda _config: observed_states.append(
                dict(worker.control_plane_state)
            ),
            sync_warning_threshold_seconds=30,
        )
        worker._last_successful_cloud_contact = 100.0

        with patch("gatekeeper.remote_sync.time.monotonic", return_value=129.9):
            worker._check_network_health()
        self.assertFalse(worker._network_offline)

        with patch("gatekeeper.remote_sync.time.monotonic", return_value=130.0):
            worker._check_network_health()
        self.assertTrue(worker._network_offline)
        self.assertTrue(worker.control_plane_state["network_offline"])
        self.assertTrue(worker.control_plane_state["tracker_standby"])
        self.assertTrue(worker.control_plane_state["alerts_suspended"])
        self.assertEqual(len(observed_states), 1)

        with patch("gatekeeper.remote_sync.time.monotonic", return_value=131.0):
            worker._check_network_health()
        self.assertEqual(len(observed_states), 1)

        with patch("gatekeeper.remote_sync.time.monotonic", return_value=132.0):
            worker._mark_cloud_success()
        self.assertFalse(worker._network_offline)
        self.assertFalse(worker.control_plane_state["network_offline"])
        self.assertFalse(worker.control_plane_state["tracker_standby"])
        self.assertEqual(len(observed_states), 2)

    def test_checkin_refreshes_cloud_heartbeat_before_jwt_expiration(self):
        worker = RemoteSyncWorker(
            cache_path=str(self.cache_path),
            fleet_url="https://fleet.internal",
            checkin_refresh_interval_seconds=60,
        )
        worker.hardware_token = "hardware-token"
        worker._access_token = "still-valid-jwt"
        worker._access_token_expires_at = datetime.now(timezone.utc).timestamp() + 600
        worker._last_successful_checkin = 10.0
        response = SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {
                "access_token": "refreshed-jwt",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "subscription_state": "active",
            },
        )

        with (
            patch("gatekeeper.remote_sync.time.monotonic", return_value=70.0),
            patch("gatekeeper.remote_sync.requests.post", return_value=response) as post,
        ):
            self.assertEqual(worker.authenticate(), "refreshed-jwt")

        self.assertEqual(
            post.call_args.args[0], "https://fleet.internal/api/v1/nodes/check-in"
        )
        self.assertEqual(worker._last_successful_checkin, 70.0)

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
        body = '{"hello":"world"}'
        with patch.dict(os.environ, {"DEVICE_SECRET": "topsecret"}, clear=False):
            worker = RemoteSyncWorker(
                cache_path=str(self.cache_path), fleet_url="https://fleet.internal"
            )
            headers = worker._build_signed_headers(body, timestamp="1700000000", nonce="nonce-1")

        expected_signature = hmac.new(
            sha256(b"topsecret").digest(),
            b"1700000000" + headers["X-Gatekeeper-Nonce"].encode("ascii") + b'{"hello":"world"}',
            sha256,
        ).hexdigest()
        self.assertEqual(headers["X-Gatekeeper-Timestamp"], "1700000000")
        self.assertEqual(headers["X-Gatekeeper-Nonce"], "nonce-1")
        self.assertEqual(headers["X-Gatekeeper-Signature"], expected_signature)

    @patch("gatekeeper.remote_sync.requests.post", side_effect=TimeoutError("network down"))
    @patch("gatekeeper.remote_sync.requests.get", side_effect=TimeoutError("network down"))
    def test_offline_fallback_uses_cached_config(self, _mock_get, _mock_post):
        self.cache_path.write_text(
            json.dumps({"yolo_confidence_threshold": 0.42, "dwell_threshold_seconds": 180}),
            encoding="utf-8",
        )

        worker = RemoteSyncWorker(
            cache_path=str(self.cache_path), fleet_url="https://fleet.example"
        )
        result = worker.sync_once()

        self.assertEqual(result["yolo_confidence_threshold"], 0.42)
        self.assertEqual(worker.latest_config["dwell_threshold_seconds"], 180)

    def test_backoff_is_capped_at_max_delay(self):
        worker = RemoteSyncWorker(
            cache_path=str(self.cache_path),
            fleet_url="https://fleet.example",
            max_backoff_seconds=300,
        )
        self.assertEqual(worker._next_backoff_delay(0), 1)
        self.assertEqual(worker._next_backoff_delay(1), 2)
        self.assertEqual(worker._next_backoff_delay(5), 300)

    @patch("gatekeeper.remote_sync.requests.get")
    def test_invalid_config_payload_is_ignored(self, mock_get):
        self.cache_path.write_text(
            json.dumps({"yolo_confidence_threshold": 0.35, "dwell_threshold_seconds": 140}),
            encoding="utf-8",
        )
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"unexpected": "payload"}

        worker = RemoteSyncWorker(
            cache_path=str(self.cache_path), fleet_url="https://fleet.example"
        )
        result = worker.sync_once()

        self.assertEqual(result["yolo_confidence_threshold"], 0.35)
        self.assertEqual(worker.latest_config["dwell_threshold_seconds"], 140)

    @patch("gatekeeper.remote_sync.requests.get")
    @patch("gatekeeper.remote_sync.requests.post")
    def test_sync_once_authenticates_and_fetches_cloud_config(self, mock_post, mock_get):
        mock_post.return_value.status_code = 200
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {
            "access_token": "node-jwt",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            "subscription_state": "grace_period",
        }
        mock_get.return_value.status_code = 200
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {
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
                db_path=self.db_path,
            )
            result = worker.sync_once()

        checkin_kwargs = mock_post.call_args.kwargs
        self.assertEqual(
            mock_post.call_args.args[0],
            "https://fleet.internal/api/v1/nodes/check-in",
        )
        self.assertEqual(checkin_kwargs["headers"]["X-Hardware-Token"], "signing-secret")
        get_kwargs = mock_get.call_args.kwargs
        self.assertEqual(
            mock_get.call_args.args[0],
            "https://fleet.internal/api/v1/nodes/config",
        )
        self.assertEqual(get_kwargs["headers"]["Authorization"], "Bearer node-jwt")
        self.assertTrue(get_kwargs["headers"]["X-Gatekeeper-Signature"])

        self.assertEqual(result["tenant_id"], "tenant-77")
        self.assertEqual(result["site_id"], "site-9")
        self.assertEqual(worker.control_plane_state["account_status"], "grace_period")
        self.assertFalse(worker.control_plane_state["alerts_suspended"])
        self.assertFalse(worker.control_plane_state["tracker_standby"])
        self.assertEqual(result["yolo_confidence_threshold"], 0.4)
        self.assertEqual(result["dwell_threshold_seconds"], 180)

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
