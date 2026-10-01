import logging
import os
import signal
import threading
import time
from pathlib import Path

from gatekeeper.config_loader import get_config
from gatekeeper.health import HealthCheckServer
from gatekeeper.notifier import SMSNotifier
from gatekeeper.remote_sync import RemoteSyncWorker
from gatekeeper.tracker import Tracker

logger = logging.getLogger("gatekeeper.runner")


def _health_host() -> str:
    if Path("/.dockerenv").exists():
        return "0.0.0.0"
    return "127.0.0.1"


class GatekeeperRunner:
    def __init__(self, config_path: str = "config.json") -> None:
        self.config_path = config_path
        self.config = get_config(config_path)
        self.tracker: Tracker | None = None
        self.notifier: SMSNotifier | None = None
        self.sync_worker: RemoteSyncWorker | None = None
        self.health_server: HealthCheckServer | None = None
        self.control_plane_state = {
            "account_status": "active",
            "alerts_suspended": False,
            "tracker_standby": False,
        }
        self._running = False
        self._shutdown_lock = threading.Lock()
        self._health_thread: threading.Thread | None = None

    def setup(self) -> None:
        logger.info("Initializing Gatekeeper runtime components...")
        self.notifier = SMSNotifier(self.config)
        self.tracker = Tracker(
            config_path=self.config_path, db_path=self.config.get("db_path", "data/gatekeeper.db")
        )
        self.tracker.notifier = self.notifier

        self.sync_worker = RemoteSyncWorker(
            fleet_url=(
                self.config.get("fleet_url")
                or self.config.get("fleet_management_url")
                or os.environ.get("FLEET_URL")
                or os.environ.get("FLEET_MANAGEMENT_URL")
                or "https://gatekeeper.teklova.com"
            ),
            cache_path="data/remote_config.json",
            telemetry_interval_seconds=int(self.config.get("telemetry_interval_seconds", 60)),
            config_callback=self._apply_remote_config,
            telemetry_supplier=self._live_telemetry,
            tenant_id=self.config.get("tenant_id") or self.config.get("client_id"),
            site_id=self.config.get("site_id"),
            db_path=self.config.get("db_path", "data/gatekeeper.db"),
            audit_batch_size=int(self.config.get("audit_batch_size", 100)),
            sync_warning_threshold_seconds=int(
                self.config.get("sync_warning_threshold_seconds", 180)
            ),
            checkin_refresh_interval_seconds=int(
                self.config.get("cloud_checkin_interval_seconds", 60)
            ),
        )

        self.health_server = HealthCheckServer(
            host=_health_host(),
            port=int(self.config.get("health_port", 8080)),
            db_path=self.config.get("db_path", "data/gatekeeper.db"),
        )
        self.health_server.set_tracker(self.tracker)

    def _apply_remote_config(self, new_config: dict) -> bool:
        if self.sync_worker is not None:
            self._apply_control_plane_state(getattr(self.sync_worker, "control_plane_state", {}))
        if self.tracker is None:
            return False
        return self.tracker.apply_config_update(new_config)

    def _apply_control_plane_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        self.control_plane_state = {
            "account_status": str(state.get("account_status") or "active"),
            "alerts_suspended": bool(state.get("alerts_suspended", False)),
            "tracker_standby": bool(state.get("tracker_standby", False)),
            "network_offline": bool(state.get("network_offline", False)),
        }
        if self.notifier is not None:
            self.notifier.sms_enabled = not self.control_plane_state["alerts_suspended"]
        if self.tracker is not None and hasattr(self.tracker, "set_standby_mode"):
            self.tracker.set_standby_mode(self.control_plane_state["tracker_standby"])

    def _live_telemetry(self) -> dict:
        if self.tracker is None:
            return {
                "processing_fps": 0.0,
                "frame_heartbeat_age_seconds": 0.0,
                "queue_backlog": 0,
                "active_camera_state": "offline",
            }

        queue_backlog = 0
        if self.notifier is not None and hasattr(self.notifier, "alert_queue"):
            queue_backlog = self.notifier.alert_queue.qsize()

        return {
            "processing_fps": self.tracker.get_current_fps(),
            "frame_heartbeat_age_seconds": max(
                0.0, time.time() - self.tracker.get_last_frame_timestamp()
            ),
            "queue_backlog": queue_backlog,
            "active_camera_state": "live",
            "heartbeat": {
                "account_status": self.control_plane_state["account_status"],
                "alerts_suspended": self.control_plane_state["alerts_suspended"],
                "tracker_standby": self.control_plane_state["tracker_standby"],
                "network_offline": self.control_plane_state["network_offline"],
            },
        }

    def start(
        self,
        source: str | None = None,
        mock_feed: bool = False,
        test_video: str | None = None,
        show_window: bool = False,
    ) -> None:
        self.setup()
        try:
            if self.sync_worker is None or not self.sync_worker._remote_backend_enabled():
                raise PermissionError("Gatekeeper Cloud endpoint is not configured")
            self.sync_worker.authenticate()
        except Exception:
            self.stop()
            raise
        self._running = True
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        if self.sync_worker is not None:
            self.sync_worker.start()
        if self.health_server is not None:
            self._health_thread = threading.Thread(
                target=self.health_server.serve_forever, daemon=True
            )
            self._health_thread.start()

        logger.info("Gatekeeper runtime started successfully")
        try:
            if self.tracker is not None:
                self.tracker.run(
                    source=source,
                    mock_feed=mock_feed,
                    test_video=test_video,
                    show_window=show_window,
                )
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received; stopping runtime")
        finally:
            self.stop()

    def _handle_signal(self, signum, frame) -> None:
        logger.info("Received shutdown signal %s; initiating graceful teardown", signum)
        self.stop()
        raise SystemExit(0)

    def stop(self) -> None:
        with self._shutdown_lock:
            if (
                not self._running
                and self.sync_worker is None
                and self.tracker is None
                and self.notifier is None
                and self.health_server is None
            ):
                return
            self._running = False
            logger.info("Stopping Gatekeeper runtime services...")

            if self.tracker is not None and hasattr(self.tracker, "stop"):
                self.tracker.stop()
            if self.notifier is not None and hasattr(self.notifier, "shutdown"):
                self.notifier.shutdown()
            if self.health_server is not None:
                if self._health_thread is not None:
                    self.health_server.shutdown()
                self.health_server.server_close()
            if self._health_thread is not None and self._health_thread.is_alive():
                self._health_thread.join(timeout=3)
                self._health_thread = None
            if self.sync_worker is not None:
                self.sync_worker.stop()

            logger.info("Shutdown complete.")


__all__ = ["GatekeeperRunner"]
