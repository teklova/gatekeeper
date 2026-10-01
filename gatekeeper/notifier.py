import logging
import json
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

MAX_ALERT_QUEUE_SIZE = 1000
DEFAULT_DEAD_LETTER_PATH = Path("data") / "failed_alerts.json"


class SMSNotifier:
    SANDBOX_URL = "https://api.sandbox.africastalking.com/version1/messaging"
    PRODUCTION_URL = "https://api.africastalking.com/version1/messaging"

    def __init__(
        self,
        username: Optional[str] = None,
        api_key: Optional[str] = None,
        recipient: Optional[str] = None,
        enabled: Optional[bool] = None,
        sandbox_mode: Optional[bool] = True,
        sandbox_api_key: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        dedupe_cooldown_seconds: float = 60.0,
    ) -> None:
        if isinstance(username, dict) and config is None:
            config = username
            username = None
        elif isinstance(api_key, dict) and config is None:
            config = api_key
            api_key = None

        cfg: Dict[str, Any] = config if isinstance(config, dict) else {}
        at_cfg = cfg.get("africastalking", {}) if isinstance(cfg.get("africastalking", {}), dict) else {}

        self.username = str(username or cfg.get("username") or at_cfg.get("username") or "sandbox").strip()
        self.api_key = str(api_key or cfg.get("api_key") or at_cfg.get("api_key") or "").strip()

        if recipient is None:
            recipient = (
                cfg.get("recipient_phone")
                or cfg.get("recipient")
                or at_cfg.get("recipient_phone")
                or at_cfg.get("recipient")
                or ""
            )
        self.sms_recipient = str(recipient or "").strip()

        if sandbox_mode is None:
            sandbox_mode = cfg.get("sandbox_mode", at_cfg.get("sandbox_mode", True))
        self.sandbox_mode = bool(sandbox_mode)

        if sandbox_api_key is None:
            sandbox_api_key = cfg.get("sandbox_api_key") or at_cfg.get("sandbox_api_key") or ""
        self.sandbox_api_key = str(sandbox_api_key or "").strip()

        if enabled is None:
            enabled = cfg.get("sms_enabled", cfg.get("enabled", True))
        self.sms_enabled = bool(enabled)

        self.sms_endpoint = self.SANDBOX_URL if self.sandbox_mode else str(at_cfg.get("sms_endpoint") or self.PRODUCTION_URL)

        self.alert_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=MAX_ALERT_QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._dedupe_cooldown_seconds = float(dedupe_cooldown_seconds)
        self._recent_alert_keys: "OrderedDict[str, float]" = OrderedDict()
        self.dead_letter_path = Path(cfg.get("dead_letter_path") or DEFAULT_DEAD_LETTER_PATH)
        self.dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
        self._dead_letter_lock = threading.Lock()
        self._worker_thread = threading.Thread(target=self._process_alert_queue, name="gatekeeper-alert-worker", daemon=True)
        self._worker_thread.start()

    def _build_alert_key(
        self,
        *,
        track_id: Optional[int] = None,
        gate_id: Optional[str] = None,
        message: Optional[str] = None,
        plate_number: Optional[str] = None,
        alert_type: str = "sms",
    ) -> str:
        if track_id is not None or gate_id is not None:
            return f"{track_id or 'unknown'}_{gate_id or 'unknown'}_{alert_type}"
        return f"{plate_number or 'unknown'}_{(message or '')[:80]}_{alert_type}"

    def _purge_dedupe_cache(self, now_ts: float) -> None:
        expired = [key for key, stamp in self._recent_alert_keys.items() if now_ts - stamp > self._dedupe_cooldown_seconds]
        for key in expired:
            self._recent_alert_keys.pop(key, None)

    def _is_duplicate_alert(self, alert_key: str) -> bool:
        now_ts = time.monotonic()
        self._purge_dedupe_cache(now_ts)
        if alert_key in self._recent_alert_keys:
            logger.info("Suppressing duplicate alert key=%s", alert_key)
            return True
        self._recent_alert_keys[alert_key] = now_ts
        return False

    def enqueue_alert(
        self,
        message: str,
        plate_number: Optional[str] = None,
        *,
        track_id: Optional[int] = None,
        gate_id: Optional[str] = None,
        alert_type: str = "sms",
    ) -> bool:
        if not self.sms_enabled or not self.sms_recipient:
            return False

        if plate_number and "Plate:" not in message:
            message = f"{message} Plate: {plate_number}"

        alert_key = self._build_alert_key(
            track_id=track_id,
            gate_id=gate_id,
            message=message,
            plate_number=plate_number,
            alert_type=alert_type,
        )
        if self._is_duplicate_alert(alert_key):
            return False

        job = {
            "alert_key": alert_key,
            "alert_type": alert_type,
            "message": message,
            "plate_number": plate_number,
            "track_id": track_id,
            "gate_id": gate_id,
            "created_at": time.time(),
        }
        if not self._enqueue_job(job):
            return False
        logger.info("Queued alert job key=%s queue_size=%s", alert_key, self.alert_queue.qsize())
        return True

    def shutdown(self):
        self._stop_event.set()
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3)

    def _is_critical_alert(self, job: Dict[str, Any]) -> bool:
        return str(job.get("alert_type") or "").lower() in {"service", "critical", "panic", "safety"}

    def _enqueue_job(self, job: Dict[str, Any]) -> bool:
        try:
            self.alert_queue.put_nowait(job)
            return True
        except queue.Full:
            return self._handle_queue_overflow(job)

    def _handle_queue_overflow(self, incoming_job: Dict[str, Any]) -> bool:
        removed_job = None
        with self.alert_queue.mutex:
            queued_items = list(self.alert_queue.queue)
            for index, existing_job in enumerate(queued_items):
                if not self._is_critical_alert(existing_job):
                    removed_job = queued_items.pop(index)
                    self.alert_queue.queue.clear()
                    self.alert_queue.queue.extend(queued_items)
                    self.alert_queue.unfinished_tasks = max(0, self.alert_queue.unfinished_tasks - 1)
                    self.alert_queue.not_full.notify()
                    break

        if removed_job is not None:
            logger.warning("Dropping low-priority alert due to full queue key=%s", removed_job.get("alert_key"))
            try:
                self.alert_queue.put_nowait(incoming_job)
                return True
            except queue.Full:
                pass

        if self._is_critical_alert(incoming_job):
            self._persist_dead_letter(incoming_job, reason="queue_full")
            logger.warning("Persisted critical alert to dead-letter queue key=%s", incoming_job.get("alert_key"))
        else:
            logger.warning("Dropped low-priority alert because queue is full key=%s", incoming_job.get("alert_key"))
        return False

    def _load_dead_letters(self) -> list:
        with self._dead_letter_lock:
            if not self.dead_letter_path.exists():
                return []
            try:
                payload = json.loads(self.dead_letter_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return []
            return payload if isinstance(payload, list) else []

    def _write_dead_letters(self, dead_letters: list) -> None:
        with self._dead_letter_lock:
            self.dead_letter_path.write_text(json.dumps(dead_letters, indent=2), encoding="utf-8")

    def _persist_dead_letter(self, job: Dict[str, Any], reason: str) -> None:
        record = dict(job)
        record["dead_letter_reason"] = reason
        record["persisted_at"] = time.time()
        dead_letters = self._load_dead_letters()
        dead_letters.append(record)
        self._write_dead_letters(dead_letters)

    def _requeue_dead_letters(self, limit: int = 10) -> None:
        if self.alert_queue.qsize() >= max(1, MAX_ALERT_QUEUE_SIZE // 2):
            return
        dead_letters = self._load_dead_letters()
        if not dead_letters:
            return

        remaining = []
        requeued = 0
        for job in dead_letters:
            if requeued >= limit:
                remaining.append(job)
                continue
            try:
                self.alert_queue.put_nowait(job)
                requeued += 1
            except queue.Full:
                remaining.append(job)
        if requeued:
            logger.info("Requeued %s dead-letter alerts", requeued)
        if len(remaining) != len(dead_letters):
            self._write_dead_letters(remaining)

    def _dispatch_job(self, job: Dict[str, Any]) -> bool:
        message = str(job.get("message") or "")
        if not message:
            return False
        if self.sandbox_mode:
            return self._send_sandbox_sms(message)
        return self._send_production_sms(message)

    def _process_alert_queue(self) -> None:
        while not self._stop_event.is_set() or not self.alert_queue.empty():
            self._requeue_dead_letters(limit=5)
            try:
                job = self.alert_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                attempts = 0
                delays = (2, 4, 8)
                success = False
                while attempts <= len(delays):
                    try:
                        success = self._dispatch_job(job)
                        if success:
                            break
                    except requests.RequestException as exc:
                        logger.warning("Alert worker request failure for key=%s: %s", job.get("alert_key"), exc)
                    if attempts >= len(delays):
                        break
                    sleep_for = delays[attempts]
                    logger.warning(
                        "Retrying alert key=%s in %ss (attempt=%s/%s)",
                        job.get("alert_key"),
                        sleep_for,
                        attempts + 1,
                        len(delays) + 1,
                    )
                    if self._stop_event.wait(sleep_for):
                        break
                    attempts += 1

                if not success:
                    logger.warning("Alert delivery failed permanently for key=%s", job.get("alert_key"))
                    if self._is_critical_alert(job):
                        self._persist_dead_letter(job, reason="delivery_failed")
            finally:
                self.alert_queue.task_done()

    @staticmethod
    def _is_placeholder(value: str) -> bool:
        normalized = str(value or "").strip()
        upper = normalized.upper()
        return (
            normalized == ""
            or upper.startswith("YOUR_")
            or "PLACEHOLDER" in upper
            or "CHANGE_ME" in upper
            or upper in {"TOKEN", "API_KEY", "NONE", "NULL"}
        )

    @staticmethod
    def _parse_response_json(response: requests.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except ValueError:
            return {}

    def _sandbox_headers(self) -> Dict[str, str]:
        return {
            "apiKey": self.sandbox_api_key,
            "Accept": "application/json",
        }

    def _production_headers(self) -> Dict[str, str]:
        return {
            "apiKey": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def _print_http_response(self, status_code: int, body_text: str) -> None:
        print(f"[SMS HTTP Response] Status: {status_code} | Body: {body_text}")

    def _send_sandbox_sms(self, message: str) -> bool:
        if self._is_placeholder(self.sandbox_api_key):
            return True

        payload = {
            "username": "sandbox",
            "to": self.sms_recipient,
            "message": message,
        }

        try:
            response = requests.post(self.SANDBOX_URL, data=payload, headers=self._sandbox_headers())
            body_text = response.text if isinstance(response.text, str) else str(response.text)
            self._print_http_response(response.status_code, body_text)

            data = self._parse_response_json(response)
            recipients = data.get("SMSMessageData", {}).get("Recipients", []) if isinstance(data, dict) else []
            first_status = ""
            if recipients and isinstance(recipients[0], dict):
                first_status = str(recipients[0].get("status", ""))

            delivered = response.status_code == 201 and first_status == "Success"
            if delivered:
                logger.info("[SMS Sent -> %s: 201 Created]", self.sms_recipient)
            else:
                logger.warning("[SMS Error -> %s: %s]", self.sms_recipient, first_status or response.status_code)
            return delivered
        except requests.RequestException as exc:
            logger.warning("[SMS Error -> %s: %s]", self.sms_recipient or "unknown", exc)
            return False

    def _send_production_sms(self, message: str) -> bool:
        if self._is_placeholder(self.api_key) or self._is_placeholder(self.username):
            return False

        payload = {
            "username": self.username,
            "to": self.sms_recipient,
            "message": message,
        }

        try:
            response = requests.post(self.sms_endpoint, data=payload, headers=self._production_headers())
            body_text = response.text if isinstance(response.text, str) else str(response.text)
            self._print_http_response(response.status_code, body_text)

            data = self._parse_response_json(response)
            recipients = data.get("SMSMessageData", {}).get("Recipients", []) if isinstance(data, dict) else []
            if not recipients:
                logger.warning("[SMS Error -> %s: missing recipients]", self.sms_recipient)
                return False
            if not all(int(item.get("statusCode", 0)) in {100, 101, 102} for item in recipients if isinstance(item, dict)):
                logger.warning("[SMS Error -> %s: recipient rejected]", self.sms_recipient)
                return False

            logger.info("[SMS Sent -> %s: %s]", self.sms_recipient, response.status_code)
            return response.status_code in (200, 201, 202)
        except requests.RequestException as exc:
            logger.warning("[SMS Error -> %s: %s]", self.sms_recipient or "unknown", exc)
            return False

    def send_alert(self, message: str, plate_number: Optional[str] = None) -> bool:
        if plate_number and "Plate:" not in message:
            message = f"{message} Plate: {plate_number}"

        if not self.sms_enabled or not self.sms_recipient:
            return False

        if self.sandbox_mode:
            return self._send_sandbox_sms(message)
        return self._send_production_sms(message)

    def send_alert_with_response(self, message: str, plate_number: Optional[str] = None):
        ok = self.send_alert(message, plate_number=plate_number)
        return (ok, {"ok": ok})
