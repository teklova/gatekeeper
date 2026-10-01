import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


def check_disk_space(path=".", min_free_gb=2.0):
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)
        return {
            "path": path,
            "total_gb": round(usage.total / (1024 ** 3), 2),
            "used_gb": round(usage.used / (1024 ** 3), 2),
            "free_gb": round(free_gb, 2),
            "min_free_gb": float(min_free_gb),
            "warning": free_gb < min_free_gb,
        }
    except OSError:
        return {
            "path": path,
            "total_gb": 0.0,
            "used_gb": 0.0,
            "free_gb": 0.0,
            "min_free_gb": float(min_free_gb),
            "warning": False,
        }


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.server.is_probe_authorized(
            client_ip=self.client_address[0],
            authorization_header=self.headers.get("Authorization"),
        ):
            self._send_json(401, {"status": "unauthorized", "message": "missing or invalid bearer token"})
            return

        if self.path in {"/health", "/healthz"}:
            self._send_json(200, self.server.health_payload())
            return

        if self.path == "/readyz":
            status_code, payload = self.server.ready_status()
            self._send_json(status_code, payload)
            return

        self._send_json(404, {"status": "not_found", "message": "endpoint not found"})

    def log_message(self, format, *args):
        return

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HealthCheckServer(ThreadingHTTPServer):
    def __init__(
        self,
        host="127.0.0.1",
        port=8080,
        db_path="data/gatekeeper.db",
        last_frame_timestamp: Optional[float] = None,
        health_check_token: Optional[str] = None,
    ):
        self.db_path = db_path
        self.last_frame_timestamp = float(last_frame_timestamp if last_frame_timestamp is not None else time.time())
        self.health_check_token = health_check_token if health_check_token is not None else os.getenv("HEALTH_CHECK_TOKEN")
        self._tracker = None
        super().__init__((host, port), HealthCheckHandler)

    def set_tracker(self, tracker):
        self._tracker = tracker

    def _current_frame_timestamp(self) -> float:
        if self._tracker is not None:
            current = getattr(self._tracker, "last_frame_timestamp", None)
            if current is not None:
                return float(current)
        return float(self.last_frame_timestamp)

    def _db_read_check(self) -> bool:
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute("SELECT 1 FROM sqlite_master WHERE name='carwash_audit'")
                conn.execute("SELECT COUNT(*) FROM sqlite_master")
                return True
        except sqlite3.Error:
            return False

    @staticmethod
    def _is_local_client(client_ip: str) -> bool:
        return client_ip in {"127.0.0.1", "::1", "localhost"}

    def is_probe_authorized(self, client_ip: str, authorization_header: Optional[str]) -> bool:
        token = (self.health_check_token or "").strip()
        if not token:
            return True
        if self._is_local_client(client_ip):
            return True
        return authorization_header == f"Bearer {token}"

    @staticmethod
    def health_payload() -> dict:
        return {
            "status": "ALIVE",
            "version": os.getenv("GATEKEEPER_VERSION", "dev"),
            "commit": os.getenv("GATEKEEPER_COMMIT", "local"),
        }

    def ready_status(self):
        last_frame_age = time.time() - self._current_frame_timestamp()
        if last_frame_age >= 15.0:
            return 503, {"status": "STALE_FRAME_LOOP"}

        if not self._db_read_check():
            return 503, {"status": "DATABASE_UNHEALTHY"}

        disk = check_disk_space(path=".", min_free_gb=2.0)
        if disk["warning"]:
            return 503, {"status": "LOW_DISK_SPACE", "disk": disk}

        return 200, {"status": "READY"}


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger(__name__)
    server = HealthCheckServer()
    logger.info("Gatekeeper health server listening on http://127.0.0.1:8080")
    server.serve_forever()

