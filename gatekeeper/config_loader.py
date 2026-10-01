import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        env_path = Path(kwargs.get("dotenv_path", ".env") or ".env")
        override = bool(kwargs.get("override", False))
        if not env_path.exists():
            return False

        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = [part.strip() for part in line.split("=", 1)]
            parsed_value = value.strip().strip('"').strip("'")
            if override or key not in os.environ:
                os.environ[key] = parsed_value
        return True

load_dotenv(override=True)


def _default_db_path() -> str:
    if Path("/.dockerenv").exists():
        return "/app/data/gatekeeper.db"
    return "./data/gatekeeper.db"

DEFAULT_CONFIG: Dict[str, Any] = {
    "camera_source": "rtsp://admin:password@192.168.1.100:554/stream1",
    "tenant_id": "",
    "client_id": "",
    "site_id": "",
    "device_id": "",
    "gate_id": "Gate 1",
    "frame_skip": 3,
    "inference_resolution": [480, 288],
    "model_path": "models/yolo11n_openvino_model",
    "dwell_threshold_seconds": 240,
    "short_dwell_mins": 3.0,
    "long_dwell_mins": 25.0,
    "min_track_frames": 3,
    "min_dwell_threshold_seconds": 2,
    "max_dwell_threshold_seconds": 14400,
    "health_port": 8080,
    "sync_warning_threshold_seconds": 180,
    "cloud_checkin_interval_seconds": 60,
    "db_path": _default_db_path(),
    "gate_polygon": [[100, 200], [500, 200], [500, 600], [100, 600]],
    "pricing_tiers": {
        "CAR": 500.0,
        "MOTORBIKE": 200.0,
        "SUV": 700.0,
        "VAN": 800.0,
        "TRUCK": 1000.0,
        "DEFAULT": 500.0,
    },
    "pricing": {
        "CAR": 500,
        "SUV": 700,
        "TRUCK": 1000,
    },
    "ENABLE_LPR": True,
    "sandbox_mode": True,
    "sandbox_api_key": "",
    "africastalking": {
        "username": "sandbox",
        "api_key": "",
        "recipient_phone": "",
        "sender_id": "",
        "sms_endpoint": "https://api.sandbox.africastalking.com/version1/messaging",
        "sandbox_mode": True,
        "sandbox_api_key": "",
    },
}


def _coerce_env_value(value: str, expected_type: type) -> Any:
    if expected_type is int:
        return int(value)
    if expected_type is float:
        return float(value)
    if expected_type is bool:
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    return value


def _environment_overrides() -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    env_map: Iterable[Tuple[str, str, type]] = (
        ("TENANT_ID", "tenant_id", str),
        ("CLIENT_ID", "client_id", str),
        ("SITE_ID", "site_id", str),
        ("DEVICE_ID", "device_id", str),
        ("GATEKEEPER_DEVICE_ID", "device_id", str),
        ("RTSP_STREAM_URL", "camera_source", str),
        ("DB_PATH", "db_path", str),
        ("MIN_TRACK_FRAMES", "min_track_frames", int),
        ("MIN_DWELL_SECONDS", "min_dwell_threshold_seconds", int),
        ("MAX_DWELL_SECONDS", "max_dwell_threshold_seconds", int),
        ("HEALTH_PORT", "health_port", int),
        ("SYNC_WARNING_THRESHOLD_SECONDS", "sync_warning_threshold_seconds", int),
        ("CLOUD_CHECKIN_INTERVAL_SECONDS", "cloud_checkin_interval_seconds", int),
        ("AT_SANDBOX_MODE", "sandbox_mode", bool),
        ("AT_SANDBOX_API_KEY", "sandbox_api_key", str),
        ("NOTIFIER_RECIPIENT_PHONE", "recipient_phone", str),
        ("AFRICASTALKING_USERNAME", "africastalking.username", str),
        ("AFRICASTALKING_API_KEY", "africastalking.api_key", str),
        ("AFRICASTALKING_RECIPIENT_PHONE", "africastalking.recipient_phone", str),
        ("AFRICASTALKING_SENDER_ID", "africastalking.sender_id", str),
        ("AFRICASTALKING_SMS_ENDPOINT", "africastalking.sms_endpoint", str),
    )

    for env_name, config_key, expected_type in env_map:
        raw_value = os.environ.get(env_name)
        if raw_value is None or raw_value == "":
            continue
        if "." in config_key:
            top_key, sub_key = config_key.split(".", 1)
            if top_key not in overrides:
                overrides[top_key] = {}
            overrides[top_key][sub_key] = _coerce_env_value(raw_value, expected_type)
        else:
            overrides[config_key] = _coerce_env_value(raw_value, expected_type)

    africastalking_keys = {
        "AFRICASTALKING_USERNAME": "username",
        "AFRICASTALKING_API_KEY": "api_key",
        "AFRICASTALKING_RECIPIENT_PHONE": "recipient_phone",
        "NOTIFIER_RECIPIENT_PHONE": "recipient_phone",
        "AFRICASTALKING_SENDER_ID": "sender_id",
        "AFRICASTALKING_SMS_ENDPOINT": "sms_endpoint",
        "AT_SANDBOX_MODE": "sandbox_mode",
        "AT_SANDBOX_API_KEY": "sandbox_api_key",
    }
    at_overrides: Dict[str, Any] = {}
    for env_name, key in africastalking_keys.items():
        raw_value = os.environ.get(env_name)
        if raw_value is None or raw_value == "":
            continue
        at_overrides[key] = raw_value

    if at_overrides:
        overrides["africastalking"] = at_overrides

    sms_flag_raw = os.environ.get("AFRICASTALKING_ENABLED")
    if sms_flag_raw is None:
        sms_flag_raw = os.environ.get("SMS_ENABLED")
    if sms_flag_raw is not None and sms_flag_raw != "":
        overrides["sms_enabled"] = _coerce_env_value(sms_flag_raw, bool)

    return overrides


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalize_tenant_scope(config: Dict[str, Any]) -> Dict[str, Any]:
    tenant_id = str(config.get("tenant_id") or config.get("client_id") or "").strip()
    site_id = str(config.get("site_id") or "").strip()
    config["tenant_id"] = tenant_id
    if not config.get("client_id") and tenant_id:
        config["client_id"] = tenant_id
    config["site_id"] = site_id
    return config


def resolve_model_dir(model_path: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    candidates = []
    if model_path:
        candidates.append(Path(model_path))
        candidates.append(root / model_path)
        candidates.append(root / "models" / model_path)
    candidates.extend([
        root / "models" / "yolo11n_openvino_model",
        root / "yolo11n_openvino_model",
        root / "models" / "yolo11n_openvino",
        root / "models" / "yolo11n_openvino" / "yolo11n.xml",
    ])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    if model_path:
        return Path(model_path)
    return root / "models" / "yolo11n_openvino_model"


def load_config(path: str = "config.json") -> Dict[str, Any]:
    config_path = Path(path)
    config = deepcopy(DEFAULT_CONFIG)

    if not config_path.exists():
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return config

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (json.JSONDecodeError, OSError):
        loaded = {}

    config = _deep_merge(config, loaded)
    config = _deep_merge(config, _environment_overrides())
    config = _normalize_tenant_scope(config)
    if "bay_polygon" in config and "gate_polygon" not in config:
        config["gate_polygon"] = config["bay_polygon"]

    # Ensure .env values always override config.json defaults when set.
    for env_name, config_key, expected_type in (
        ("AT_SANDBOX_MODE", "sandbox_mode", bool),
        ("AT_SANDBOX_API_KEY", "sandbox_api_key", str),
        ("NOTIFIER_RECIPIENT_PHONE", "africastalking.recipient_phone", str),
        ("AFRICASTALKING_USERNAME", "africastalking.username", str),
        ("AFRICASTALKING_API_KEY", "africastalking.api_key", str),
        ("AFRICASTALKING_RECIPIENT_PHONE", "africastalking.recipient_phone", str),
        ("AFRICASTALKING_SENDER_ID", "africastalking.sender_id", str),
        ("AFRICASTALKING_SMS_ENDPOINT", "africastalking.sms_endpoint", str),
    ):
        raw_value = os.environ.get(env_name)
        if raw_value is None or raw_value == "":
            continue
        if "." in config_key:
            top_key, sub_key = config_key.split(".", 1)
            config.setdefault(top_key, {})[sub_key] = _coerce_env_value(raw_value, expected_type)
        else:
            config[config_key] = _coerce_env_value(raw_value, expected_type)

    sandbox_mode = bool(config.get("sandbox_mode", True))
    config["sandbox_mode"] = sandbox_mode

    sandbox_api_key = str(config.get("sandbox_api_key") or "").strip()
    config["sandbox_api_key"] = sandbox_api_key

    africastalking = config.get("africastalking", {}) or {}
    africastalking["sandbox_mode"] = sandbox_mode
    africastalking["sandbox_api_key"] = sandbox_api_key
    if not africastalking.get("username"):
        africastalking["username"] = "sandbox"
    if africastalking.get("username") == "sandbox" and not africastalking.get("sender_id"):
        africastalking["sender_id"] = "19191"
    if not africastalking.get("recipient_phone") and config.get("recipient_phone"):
        africastalking["recipient_phone"] = config["recipient_phone"]
    if os.environ.get("NOTIFIER_RECIPIENT_PHONE"):
        africastalking["recipient_phone"] = str(os.environ.get("NOTIFIER_RECIPIENT_PHONE") or "").strip()
        config["recipient_phone"] = africastalking["recipient_phone"]
    if sandbox_mode:
        africastalking["sms_endpoint"] = "https://api.sandbox.africastalking.com/version1/messaging"
    elif not africastalking.get("sms_endpoint"):
        africastalking["sms_endpoint"] = "https://api.africastalking.com/version1/messaging"

    # Keep top-level and nested dwell thresholds aligned for simplified operations.
    if not config.get("dwell_threshold_seconds"):
        config["dwell_threshold_seconds"] = 240
    config["africastalking"] = africastalking

    if not config.get("recipient_phone") and africastalking.get("recipient_phone"):
        config["recipient_phone"] = africastalking["recipient_phone"]

    return _normalize_tenant_scope(config)


def get_config(path: str = "config.json") -> Dict[str, Any]:
    return load_config(path)


def normalize_gate_polygon(points: Any) -> list:
    normalized = []
    if not isinstance(points, (list, tuple)):
        return normalized

    for pt in points:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        try:
            x = int(round(float(pt[0])))
            y = int(round(float(pt[1])))
        except (TypeError, ValueError):
            continue
        normalized.append([x, y])
    return normalized


def save_gate_polygon(points: Any, path: str = "config.json") -> Dict[str, Any]:
    config_path = Path(path)
    config = load_config(path)
    normalized = normalize_gate_polygon(points)
    if len(normalized) < 3:
        return config

    config["gate_polygon"] = normalized

    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                disk_config = json.load(handle)
        except (json.JSONDecodeError, OSError):
            disk_config = {}
    else:
        disk_config = {}

    if not isinstance(disk_config, dict):
        disk_config = {}

    merged = _deep_merge(disk_config, {"gate_polygon": normalized})
    config_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return config

