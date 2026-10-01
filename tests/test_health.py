import json
import os
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from time import sleep
from unittest.mock import patch

from gatekeeper.health import HealthCheckServer


class TestHealthEndpoint(unittest.TestCase):
    def test_default_host_binds_to_localhost(self):
        server = HealthCheckServer(port=0)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()

    def test_probe_auth_allows_local_without_header(self):
        server = HealthCheckServer(host="127.0.0.1", port=0, health_check_token="test-token")
        try:
            self.assertTrue(server.is_probe_authorized("127.0.0.1", None))
            self.assertTrue(server.is_probe_authorized("::1", None))
        finally:
            server.server_close()

    def test_probe_auth_requires_valid_bearer_for_non_local(self):
        server = HealthCheckServer(host="127.0.0.1", port=0, health_check_token="test-token")
        try:
            self.assertFalse(server.is_probe_authorized("10.10.10.10", None))
            self.assertFalse(server.is_probe_authorized("10.10.10.10", "Bearer wrong-token"))
            self.assertTrue(server.is_probe_authorized("10.10.10.10", "Bearer test-token"))
        finally:
            server.server_close()

    def test_health_endpoint_returns_alive(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
            db_path = handle.name
        try:
            server = HealthCheckServer(host="127.0.0.1", port=0, db_path=db_path)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            sleep(0.2)
            port = server.server_address[1]
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.request("GET", "/healthz")
                response = conn.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
            finally:
                conn.close()

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "ALIVE")

            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        finally:
            if os.path.exists(db_path):
                for _ in range(20):
                    try:
                        os.remove(db_path)
                        break
                    except PermissionError:
                        time.sleep(0.1)

    def test_readyz_returns_ready_when_tracker_is_fresh_and_db_is_healthy(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
            db_path = handle.name
        try:
            server = HealthCheckServer(host="127.0.0.1", port=0, db_path=db_path)
            server.last_frame_timestamp = time.time()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            sleep(0.2)
            port = server.server_address[1]
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                with patch(
                    "gatekeeper.health.check_disk_space",
                    return_value={
                        "path": ".",
                        "total_gb": 100.0,
                        "used_gb": 50.0,
                        "free_gb": 50.0,
                        "min_free_gb": 2.0,
                        "warning": False,
                    },
                ):
                    conn.request("GET", "/readyz")
                    response = conn.getresponse()
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                conn.close()

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "READY")

            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
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

