import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from scripts.bootstrap import bootstrap_node


class TestBootstrap(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.env_file = self.root / ".env"

    @staticmethod
    def _success_response(state="active"):
        return SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {
                "access_token": "temporary-jwt",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                "subscription_state": state,
            },
        )

    def test_bootstrap_json_checks_cloud_before_secure_env_update(self):
        bootstrap_file = self.root / "cloud-bootstrap.json"
        bootstrap_file.write_text(
            json.dumps(
                {
                    "node_id": "edge-node-42",
                    "token": "one-time-hardware-token",
                    "cloud_url": "https://gatekeeper.teklova.com",
                }
            ),
            encoding="utf-8",
        )
        self.env_file.write_text(
            "CAMERA_SOURCE=rtsp://camera/stream\nDEVICE_SECRET=old-token\n",
            encoding="utf-8",
        )
        post = Mock(return_value=self._success_response())

        result = bootstrap_node(
            bootstrap_file=bootstrap_file,
            env_file=self.env_file,
            post=post,
        )

        post.assert_called_once_with(
            "https://gatekeeper.teklova.com/api/v1/nodes/check-in",
            headers={"X-Hardware-Token": "one-time-hardware-token"},
            timeout=10,
            allow_redirects=False,
        )
        content = self.env_file.read_text(encoding="utf-8")
        self.assertIn("CAMERA_SOURCE=rtsp://camera/stream", content)
        self.assertIn('DEVICE_SECRET="one-time-hardware-token"', content)
        self.assertIn('GATEKEEPER_DEVICE_ID="edge-node-42"', content)
        self.assertEqual(result["node_id"], "edge-node-42")
        self.assertNotIn("temporary-jwt", content)
        if os.name != "nt":
            self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)

    def test_environment_credentials_are_supported(self):
        post = Mock(return_value=self._success_response("grace_period"))
        with patch.dict(
            os.environ,
            {"DEVICE_SECRET": "environment-token", "FLEET_URL": "https://cloud.example"},
            clear=False,
        ):
            bootstrap_node(env_file=self.env_file, post=post)

        self.assertIn('DEVICE_SECRET="environment-token"', self.env_file.read_text())
        self.assertIn('FLEET_URL="https://cloud.example"', self.env_file.read_text())

    def test_failed_handshake_does_not_modify_existing_env(self):
        original = "DEVICE_SECRET=existing\nCAMERA_SOURCE=rtsp://camera\n"
        self.env_file.write_text(original, encoding="utf-8")
        response = requests.Response()
        response.status_code = 403

        with self.assertRaises(requests.HTTPError):
            bootstrap_node(
                env_file=self.env_file,
                device_secret="rejected-token",
                fleet_url="https://gatekeeper.teklova.com",
                post=Mock(return_value=response),
            )

        self.assertEqual(self.env_file.read_text(encoding="utf-8"), original)

    def test_http_cloud_url_is_rejected_without_network_or_file_write(self):
        post = Mock()
        with self.assertRaises(ValueError):
            bootstrap_node(
                env_file=self.env_file,
                device_secret="hardware-token",
                fleet_url="http://gatekeeper.teklova.com",
                post=post,
            )
        post.assert_not_called()
        self.assertFalse(self.env_file.exists())


if __name__ == "__main__":
    unittest.main()