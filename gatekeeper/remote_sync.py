import hmac
import json
import logging
import os
import secrets
import sqlite3
import shutil
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from gatekeeper.config_loader import get_config
from gatekeeper.db import get_pending_audit_events, mark_audit_events_synced

try:
    import jsonschema
except ImportError:  # pragma: no cover - optional in lightweight environments.
    jsonschema = None

try:
    import psutil
except ImportError:  # pragma: no cover - dependency is installed in production, but we degrade cleanly.
    psutil = None

import requests


DEFAULT_FLEET_URL = os.environ.get("FLEET_MANAGEMENT_URL", "https://fleet.example.com")
DEFAULT_CACHE_PATH = Path("data") / "remote_config.json"
SCHEMA_PATH = Path(__file__).resolve().with_name("remote_config.schema.json")
logger = logging.getLogger(__name__)


def validate_config_schema(config: Dict[str, Any]) -> bool:
    """Validate a remote config payload using JSON Schema when available, else fall back to structural validation."""
    if not isinstance(config, dict):
        return False

    required_keys = {"yolo_confidence_threshold", "dwell_threshold_seconds"}
    if not required_keys.issubset(config.keys()):
        for key in required_keys:
            if key not in config:
                return False

    if jsonschema is not None and SCHEMA_PATH.exists():
        try:
            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            jsonschema.validate(instance=config, schema=schema)
            return True
        except (TypeError, ValueError, jsonschema.ValidationError):
            return False

    conf = config.get("yolo_confidence_threshold")
    dwell = config.get("dwell_threshold_seconds")
    if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
        return False
    if not isinstance(dwell, (int, float)) or float(dwell) <= 0:
        return False

    roi = config.get("roi_polygons", [])
    if roi is not None and not isinstance(roi, list):
        return False

    active = config.get("active_cameras", [])
    if active is not None and not isinstance(active, (list, dict)):
        return False

    telemetry_interval = config.get("telemetry_interval_seconds")
    if telemetry_interval is not None and (not isinstance(telemetry_interval, int) or telemetry_interval < 10):
        return False

    return True


class RemoteSyncWorker:
    """Background worker for telemetry reporting and remote config polling."""

    def __init__(
        self,
        fleet_url: str = DEFAULT_FLEET_URL,
        cache_path: str = str(DEFAULT_CACHE_PATH),
        telemetry_interval_seconds: int = 60,
        max_backoff_seconds: int = 300,
        config_callback: Optional[Any] = None,
        telemetry_supplier: Optional[Any] = None,
        tenant_id: Optional[str] = None,
        site_id: Optional[str] = None,
        db_path: Optional[str] = None,
        audit_batch_size: int = 100,
        audit_endpoint_path: str = "/api/v1/audit-events/batch",
    ):
        config = get_config()
        self.fleet_url = fleet_url.rstrip("/")
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.telemetry_interval_seconds = telemetry_interval_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.latest_config: Dict[str, Any] = self._load_local_config()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._backoff_seconds = 1
        self._last_successful_sync = 0.0
        self._config_callback = config_callback
        self._telemetry_supplier = telemetry_supplier
        self.tenant_id = str(tenant_id or config.get("tenant_id") or config.get("client_id") or "").strip()
        self.site_id = str(site_id or config.get("site_id") or "").strip()
        self.db_path = str(db_path or config.get("db_path") or "data/gatekeeper.db")
        self.audit_batch_size = max(1, int(audit_batch_size))
        self.audit_endpoint_path = "/" + audit_endpoint_path.strip("/")
        self.account_status = "active"
        self.control_plane_state: Dict[str, Any] = {
            "account_status": self.account_status,
            "alerts_suspended": False,
            "tracker_standby": False,
        }

    def register_config_callback(self, callback: Any) -> None:
        self._config_callback = callback

    def register_telemetry_supplier(self, supplier: Any) -> None:
        self._telemetry_supplier = supplier

    def _load_local_config(self) -> Dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            with self.cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_local_config(self, config: Dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, sort_keys=True)

    def _device_id(self) -> str:
        device_id = os.environ.get("DEVICE_ID") or os.environ.get("GATEKEEPER_DEVICE_ID")
        if device_id:
            return str(device_id)
        configured_device_id = str(self.latest_config.get("device_id") or "").strip()
        if configured_device_id:
            return configured_device_id
        return os.environ.get("HOSTNAME") or os.environ.get("COMPUTERNAME") or "gatekeeper-edge"

    def _request_signing_secret(self) -> str:
        return str(os.environ.get("DEVICE_SECRET") or "").strip()

    def _tenant_scope(self) -> Dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "site_id": self.site_id,
        }

    def _serialize_json_body(self, payload: Dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def _build_signed_headers(self, body_text: str, timestamp: Optional[str] = None, nonce: Optional[str] = None) -> Dict[str, str]:
        timestamp_value = timestamp or str(int(time.time()))
        nonce_value = nonce or secrets.token_hex(16)
        headers = {
            "X-Gatekeeper-Timestamp": timestamp_value,
            "X-Gatekeeper-Nonce": nonce_value,
        }
        secret = self._request_signing_secret()
        if secret:
            signature_input = f"{timestamp_value}{body_text}".encode("utf-8")
            signature = hmac.new(secret.encode("utf-8"), signature_input, sha256).hexdigest()
            headers["X-Gatekeeper-Signature"] = signature
        return headers

    def _control_plane_flags(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_status = str(payload.get("account_status") or "active").strip().lower() or "active"
        suspended = account_status in {"suspended", "unpaid"}
        return {
            "account_status": account_status,
            "alerts_suspended": suspended,
            "tracker_standby": suspended,
        }

    def _extract_effective_config(self, payload: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if not isinstance(payload, dict):
            return {}, self._control_plane_flags({})

        control_state = self._control_plane_flags(payload)
        config_payload = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        if not isinstance(config_payload, dict):
            return {}, control_state

        config_payload = dict(config_payload)
        config_payload.update({
            key: value for key, value in self._tenant_scope().items() if value
        })
        if "account_status" in payload:
            config_payload["account_status"] = payload["account_status"]
        return config_payload, control_state

    def _remote_backend_enabled(self) -> bool:
        candidate = (self.fleet_url or "").strip()
        if not candidate:
            return False
        parsed = urlparse(candidate)
        host = (parsed.hostname or candidate.split("://", 1)[-1].split("/", 1)[0]).lower()
        if not parsed.scheme or not host:
            return False
        if "example" in host:
            return False
        return True

    def _flush_audit_queue(self) -> int:
        if not self._remote_backend_enabled():
            return 0

        if not self._request_signing_secret():
            logger.critical("Audit sync is disabled because DEVICE_SECRET is not configured")
            return 0

        events = get_pending_audit_events(self.audit_batch_size, self.db_path)
        if not events:
            return 0

        payload = {
            "device_id": self._device_id(),
            **self._tenant_scope(),
            "events": events,
        }
        body_text = self._serialize_json_body(payload)
        event_ids = [int(event["id"]) for event in events]
        idempotency_key = sha256(body_text.encode("utf-8")).hexdigest()
        response = requests.post(
            f"{self.fleet_url}{self.audit_endpoint_path}",
            data=body_text,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
                **self._build_signed_headers(body_text),
            },
            timeout=5,
            allow_redirects=False,
        )
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise requests.HTTPError(f"Audit sync returned unexpected status {response.status_code}")
        return mark_audit_events_synced(event_ids, self.db_path)

    def collect_telemetry(self) -> Dict[str, Any]:
        now = time.time()
        if psutil is not None:
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage(".")
            system = {
                "cpu_percent": psutil.cpu_percent(interval=None),
                "memory_percent": memory.percent,
                "memory_used_mb": round(memory.used / (1024 * 1024), 2),
                "disk_free_gb": round(disk.free / (1024 ** 3), 2),
            }
        else:
            disk = shutil.disk_usage(".")
            system = {
                "cpu_percent": 0.0,
                "memory_percent": 0.0,
                "memory_used_mb": 0.0,
                "disk_free_gb": round(disk.free / (1024 ** 3), 2),
            }

        runtime = {
            "app_version": "0.1.2",
            "uptime_seconds": int(now - getattr(self, "_start_time", now)),
            "frame_heartbeat_age_seconds": 0.0,
            "active_camera_state": "unknown",
            "processing_fps": 0.0,
            "heartbeat": {
                "status": "alive",
                "timestamp": now,
            },
        }
        self._start_time = getattr(self, "_start_time", now)
        runtime["uptime_seconds"] = int(now - self._start_time)

        if callable(self._telemetry_supplier):
            supplied = self._telemetry_supplier()
            if isinstance(supplied, dict):
                runtime.update(supplied)

        telemetry = {
            "device_id": self._device_id(),
            **self._tenant_scope(),
            "system": system,
            "runtime": runtime,
            "timestamp": time.time(),
        }
        telemetry["runtime"]["heartbeat"].update(self._tenant_scope())
        return telemetry

    def _next_backoff_delay(self, retries: int) -> int:
        if retries <= 0:
            return 1
        if retries == 1:
            return 2
        if retries >= 5:
            return self.max_backoff_seconds
        return min(self.max_backoff_seconds, 2 ** retries)

    def _valid_remote_config(self, payload: Any) -> Dict[str, Any]:
        config_payload, _control_state = self._extract_effective_config(payload)
        if not isinstance(config_payload, dict):
            return {}
        required_keys = {"yolo_confidence_threshold", "dwell_threshold_seconds", "roi_polygons", "active_cameras"}
        if not required_keys.issubset(config_payload.keys()):
            if not validate_config_schema(config_payload):
                return {}
            return config_payload
        if not validate_config_schema(config_payload):
            return {}
        return config_payload

    def sync_once(self) -> Dict[str, Any]:
        device_id = self._device_id()
        if not self._remote_backend_enabled():
            local_config = self._load_local_config()
            if local_config and validate_config_schema(local_config):
                self.latest_config = local_config
                if callable(self._config_callback):
                    self._config_callback(local_config)
                return local_config
            return self.latest_config

        try:
            telemetry = self.collect_telemetry()
            telemetry_body = self._serialize_json_body(telemetry)
            response = requests.post(
                f"{self.fleet_url}/api/v1/telemetry",
                data=telemetry_body,
                headers={
                    "Content-Type": "application/json",
                    **self._build_signed_headers(telemetry_body),
                },
                timeout=3,
            )
            response.raise_for_status()
        except (requests.RequestException, TimeoutError, OSError):
            pass

        try:
            self._flush_audit_queue()
        except (requests.RequestException, TimeoutError, OSError, sqlite3.Error) as exc:
            logger.warning("Audit queue sync failed; pending rows retained: %s", exc)

        try:
            response = requests.get(
                f"{self.fleet_url}/api/v1/config?device_id={device_id}",
                headers=self._build_signed_headers(""),
                timeout=3,
            )
            response.raise_for_status()
            payload = response.json()
            _, control_state = self._extract_effective_config(payload)
            self.control_plane_state = control_state
            self.account_status = str(control_state.get("account_status") or "active")
            valid_config = self._valid_remote_config(payload)
            if valid_config:
                self.latest_config = valid_config
                self._write_local_config(valid_config)
                self._last_successful_sync = time.time()
                self._backoff_seconds = 1
                if callable(self._config_callback):
                    self._config_callback(valid_config)
                return valid_config
        except (requests.RequestException, TimeoutError, OSError, ValueError):
            pass

        local_config = self._load_local_config()
        if local_config and validate_config_schema(local_config):
            self.latest_config = local_config
            if callable(self._config_callback):
                self._config_callback(local_config)
            return local_config
        return self.latest_config

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run_loop(self) -> None:
        retry_count = 0
        while not self._stop_event.is_set():
            try:
                self.sync_once()
                retry_count = 0
            except Exception:
                retry_count += 1
            delay_seconds = self._next_backoff_delay(retry_count)
            if self._stop_event.wait(self.telemetry_interval_seconds if retry_count == 0 else delay_seconds):
                break


__all__ = ["RemoteSyncWorker"]
