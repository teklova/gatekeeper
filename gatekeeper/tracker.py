import logging
import sqlite3
import threading
import time
import warnings
import zlib
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np

try:
    from ultralytics import YOLO
except ImportError:  # pragma: no cover - exercised in lightweight test envs without model packages.
    YOLO = None

from gatekeeper.config_loader import get_config, resolve_model_dir, save_gate_polygon
from gatekeeper.db import ensure_database, log_vehicle_exit
from gatekeeper.notifier import SMSNotifier

logger = logging.getLogger("gatekeeper.tracker")


class _StubYOLOModel:
    """Minimal stand-in used when YOLO is unavailable in test or dev environments."""

    def __init__(self):
        self.names = {0: "person", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

    def predict(self, *args, **kwargs):
        return [None]


class _FallbackByteTrack:
    """Compatibility tracker used when Supervision is unavailable in lightweight test runs."""

    def __init__(self):
        self._next_id = 1

    def update_with_detections(self, detections):
        if detections is None:
            return detections

        if getattr(detections, "tracker_id", None) is None:
            try:
                detections.tracker_id = np.array([self._next_id + i for i in range(len(detections))], dtype=int)
            except Exception:
                detections.tracker_id = []
        return detections


DEFAULT_TEST_VIDEO = "tests/sample_gate.mp4"
WINDOW_NAME = "Gatekeeper Feed & Calibration"
VEHICLE_CLASS_IDS = {2, 3, 5, 7}
STANDARD_VEHICLE_CLASSES = {"Car", "Motorbike", "SUV", "Van", "Truck"}
COCO_TO_STANDARD = {
    "car": "Car",
    "motorcycle": "Motorbike",
    "motorbike": "Motorbike",
    "truck": "Truck",
    "bus": "Van",
    "van": "Van",
    "suv": "SUV",
}

try:
    import supervision as sv

    SUPERVISION_AVAILABLE = True
except ImportError:
    sv = None
    SUPERVISION_AVAILABLE = False


@dataclass
class ActiveTrack:
    entry_dt: datetime
    last_seen_ts: float
    vehicle_type: str
    entry_notified: bool = False
    service_notified: bool = False


class Tracker:
    def __init__(
        self,
        config_path: Optional[str] = "config.json",
        db_path: Optional[str] = None,
        model_path: Optional[str] = None,
        confidence_threshold: float = 0.25,
        iou_threshold: float = 0.5,
        **_: Any,
    ):
        self.config_path = str(config_path or "config.json")
        self.config = get_config(self.config_path)
        self.db_path = db_path or self.config.get("db_path", "data/gatekeeper.db")
        self.tenant_id = str(self.config.get("tenant_id") or self.config.get("client_id") or "").strip()
        self.site_id = str(self.config.get("site_id") or "").strip()
        self.gate_id = self.config.get("gate_id") or self.config.get("bay_id", "Gate 1")
        self.dwell_threshold_seconds = float(self.config.get("dwell_threshold_seconds", 240))
        self.track_lost_timeout_seconds = float(self.config.get("track_lost_timeout_seconds", 2.5))
        self.confidence_threshold = confidence_threshold
        self.iou_threshold = iou_threshold
        self.frame_skip = max(1, int(self.config.get("frame_skip", 1)))

        selected_model_path = model_path or self.config.get("model_path", "models/yolo11n_openvino_model")
        resolved_model_path = resolve_model_dir(selected_model_path)
        logger.info("Loading model: %s", resolved_model_path)
        logging.getLogger("ultralytics").setLevel(logging.ERROR)
        logging.getLogger("supervision").setLevel(logging.ERROR)
        if YOLO is not None:
            self.model = YOLO(str(resolved_model_path), task="detect")
        else:
            self.model = _StubYOLOModel()

        self.tracker = None
        if SUPERVISION_AVAILABLE:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r".*ByteTrack.*deprecated.*",
                    category=FutureWarning,
                )
                try:
                    self.tracker = sv.ByteTrack(
                        track_thresh=0.25,
                        track_buffer=30,
                        match_thresh=0.8,
                        frame_rate=30,
                    )
                except TypeError:
                    try:
                        self.tracker = sv.ByteTrack()
                    except TypeError:
                        self.tracker = _FallbackByteTrack()
        else:
            self.tracker = _FallbackByteTrack()

        self.active_tracks: Dict[int, ActiveTrack] = {}
        self.notifier = SMSNotifier(self.config)
        self.pricing_tiers = self._load_pricing_tiers()

        self.active_polygon_points: List[Tuple[int, int]] = []
        self.active_polygon_array: Optional[np.ndarray] = None
        self.calibration_points: List[Tuple[int, int]] = []
        self.window_enabled = False
        self.show_window = False
        self.calibration_mode = False
        self.pause_for_calibration = False
        self._display_scale_x = 1.0
        self._display_scale_y = 1.0
        self._frame_width = 0
        self._frame_height = 0
        self._last_frame_timestamp_lock = threading.Lock()
        self._last_frame_timestamp = time.time()
        self.current_fps = 0.0
        self._config_lock = threading.Lock()
        self.conf_threshold = float(self.config.get("yolo_confidence_threshold", confidence_threshold))
        self.dwell_threshold = float(self.config.get("dwell_threshold_seconds", self.dwell_threshold_seconds))
        self.roi_polygons = list(self.config.get("roi_polygons", []))
        self.active_cameras = list(self.config.get("active_cameras", []))
        self.standby_mode = False
        self._stop_event = threading.Event()
        self._capture_lock = threading.Lock()
        self._active_capture: Optional[cv2.VideoCapture] = None
        self._frame_signature_window = deque(maxlen=30)
        self.freeze_frame_threshold = 30
        self.freeze_events = 0

        self._refresh_active_polygon(self.config.get("gate_polygon", []))
        ensure_database(self.db_path)

    def apply_config_update(self, new_config: dict) -> bool:
        """Apply validated runtime configuration updates without interrupting the video loop."""
        if not isinstance(new_config, dict):
            return False

        updates: Dict[str, Any] = {}

        if "yolo_confidence_threshold" in new_config:
            conf = new_config["yolo_confidence_threshold"]
            if isinstance(conf, (int, float)) and 0.0 <= float(conf) <= 1.0:
                updates["conf_threshold"] = float(conf)
            else:
                logger.warning("[CONFIG] Invalid yolo_confidence_threshold value: %s", conf)
                return False

        if "dwell_threshold_seconds" in new_config:
            dwell = new_config["dwell_threshold_seconds"]
            if isinstance(dwell, (int, float)) and float(dwell) > 0:
                updates["dwell_threshold"] = float(dwell)
            else:
                logger.warning("[CONFIG] Invalid dwell_threshold_seconds value: %s", dwell)
                return False

        if "roi_polygons" in new_config:
            roi = new_config["roi_polygons"]
            if isinstance(roi, list):
                updates["roi_polygons"] = roi
            else:
                logger.warning("[CONFIG] Invalid roi_polygons payload: %s", roi)
                return False

        if "active_cameras" in new_config:
            cameras = new_config["active_cameras"]
            if isinstance(cameras, (list, dict)):
                updates["active_cameras"] = cameras
            else:
                logger.warning("[CONFIG] Invalid active_cameras payload: %s", cameras)
                return False

        if "tenant_id" in new_config or "client_id" in new_config:
            tenant_id = str(new_config.get("tenant_id") or new_config.get("client_id") or "").strip()
            updates["tenant_id"] = tenant_id

        if "site_id" in new_config:
            updates["site_id"] = str(new_config.get("site_id") or "").strip()

        if not updates:
            return False

        with self._config_lock:
            for key, value in updates.items():
                old_value = getattr(self, key, None)
                setattr(self, key, value)
                logger.info("[CONFIG] Updated %s from %s to %s", key, old_value, value)

            if "conf_threshold" in updates:
                self.confidence_threshold = float(updates["conf_threshold"])
                self.config["yolo_confidence_threshold"] = self.confidence_threshold
            if "dwell_threshold" in updates:
                self.dwell_threshold_seconds = float(updates["dwell_threshold"])
                self.config["dwell_threshold_seconds"] = self.dwell_threshold_seconds
            if "roi_polygons" in updates:
                self._refresh_active_polygon(updates["roi_polygons"])
                self.config["roi_polygons"] = updates["roi_polygons"]
            if "active_cameras" in updates:
                self.config["active_cameras"] = updates["active_cameras"]
            if "tenant_id" in updates:
                self.tenant_id = str(updates["tenant_id"])
                self.config["tenant_id"] = self.tenant_id
                self.config["client_id"] = self.tenant_id
            if "site_id" in updates:
                self.site_id = str(updates["site_id"])
                self.config["site_id"] = self.site_id

        return True

    def set_standby_mode(self, standby: bool) -> None:
        self.standby_mode = bool(standby)

    def stop(self) -> None:
        self._stop_event.set()
        with self._capture_lock:
            cap = self._active_capture
            self._active_capture = None
        if cap is not None:
            try:
                cap.grab()
            except Exception:
                pass
            cap.release()
        self.active_tracks.clear()
        self._frame_signature_window.clear()

    def _register_capture(self, cap: Optional[cv2.VideoCapture]) -> None:
        with self._capture_lock:
            self._active_capture = cap

    def _release_capture(self, cap: Optional[cv2.VideoCapture]) -> None:
        if cap is None:
            return
        with self._capture_lock:
            if self._active_capture is cap:
                self._active_capture = None
        try:
            cap.grab()
        except Exception:
            pass
        cap.release()

    @staticmethod
    def _frame_signature(frame: np.ndarray) -> int:
        if frame is None:
            return -1
        reduced = frame
        try:
            reduced = cv2.resize(frame, (16, 16), interpolation=cv2.INTER_AREA)
        except Exception:
            pass
        return zlib.crc32(np.ascontiguousarray(reduced).tobytes())

    def _record_frame_signature(self, frame: np.ndarray) -> bool:
        signature = self._frame_signature(frame)
        self._frame_signature_window.append(signature)
        if len(self._frame_signature_window) < self.freeze_frame_threshold:
            return False
        frozen = len(set(self._frame_signature_window)) == 1
        if frozen:
            self.freeze_events += 1
        return frozen

    @property
    def last_frame_timestamp(self) -> float:
        with self._last_frame_timestamp_lock:
            return self._last_frame_timestamp

    @last_frame_timestamp.setter
    def last_frame_timestamp(self, value: float) -> None:
        with self._last_frame_timestamp_lock:
            self._last_frame_timestamp = float(value)

    def get_last_frame_timestamp(self) -> float:
        return self.last_frame_timestamp

    def get_current_fps(self) -> float:
        return float(self.current_fps)

    def _log_audit_event(self, vehicle_type: str, entry_dt: datetime, exit_dt: datetime, dwell_seconds: float) -> None:
        """Persist a completed vehicle audit row using the production carwash_audit schema."""
        ensure_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO carwash_audit (tenant_id, site_id, gate_id, vehicle_type, entry_time, exit_time, dwell_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.tenant_id or None,
                    self.site_id or None,
                    self.gate_id,
                    vehicle_type,
                    entry_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    exit_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    round(float(dwell_seconds), 2),
                ),
            )
            conn.commit()

    def _load_pricing_tiers(self) -> Dict[str, float]:
        raw_tiers = self.config.get("pricing_tiers", {}) or {}
        pricing = {
            "Car": float(raw_tiers.get("CAR", raw_tiers.get("SMALL_CAR", 500))),
            "Motorbike": float(raw_tiers.get("MOTORBIKE", raw_tiers.get("MOTORCYCLE", 200))),
            "SUV": float(raw_tiers.get("SUV", raw_tiers.get("LARGE_SUV", 700))),
            "Van": float(raw_tiers.get("VAN", raw_tiers.get("BUS", 800))),
            "Truck": float(raw_tiers.get("TRUCK", 1000)),
            "DEFAULT": float(raw_tiers.get("DEFAULT", 500)),
        }
        return pricing

    def _refresh_active_polygon(self, polygon_points: Any) -> None:
        valid_points: List[Tuple[int, int]] = []
        if isinstance(polygon_points, list):
            for pt in polygon_points:
                if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                    continue
                try:
                    valid_points.append((int(pt[0]), int(pt[1])))
                except (TypeError, ValueError):
                    continue

        self.active_polygon_points = valid_points
        if len(valid_points) >= 3:
            self.active_polygon_array = np.array(valid_points, dtype=np.int32)
        else:
            self.active_polygon_array = None

    def _vehicle_type_from_model(self, class_id: int) -> str:
        raw_name = str(self.model.names.get(class_id, "car")).strip().lower()
        if raw_name in COCO_TO_STANDARD:
            normalized = COCO_TO_STANDARD[raw_name]
            return normalized if normalized in STANDARD_VEHICLE_CLASSES else "Car"
        if "truck" in raw_name:
            return "Truck"
        if "suv" in raw_name or "jeep" in raw_name:
            return "SUV"
        if "bike" in raw_name or "motor" in raw_name:
            return "Motorbike"
        if "van" in raw_name or "bus" in raw_name:
            return "Van"
        return "Car"

    @staticmethod
    def _format_dwell(dwell_seconds: float) -> str:
        total = max(0, int(dwell_seconds))
        mins = total // 60
        secs = total % 60
        return f"{mins}m {secs:02d}s"

    @staticmethod
    def _format_dwell_compact(dwell_seconds: float) -> str:
        total = max(0, int(dwell_seconds))
        mins = total // 60
        secs = total % 60
        return f"{mins:02d}:{secs:02d}"

    def _fee_for_vehicle(self, vehicle_type: str) -> float:
        return float(self.pricing_tiers.get(vehicle_type, self.pricing_tiers.get("DEFAULT", 500.0)))

    def _build_summary_message(self, vehicle_type: str, track_id: int, dwell_seconds: float, serviced: bool) -> str:
        fee_value = self._fee_for_vehicle(vehicle_type)
        status = "Serviced (Washed)" if serviced else "In Service"
        fee_text = f"KES {fee_value:.0f}" if serviced else "Pending"
        return (
            f"Gatekeeper Alert: Vehicle [{vehicle_type} - Track {track_id}] entered {self.gate_id}. "
            f"Dwell Time: {self._format_dwell(dwell_seconds)} | Status: {status} | Fee: {fee_text}."
        )

    def _source_for_run(self, source: Optional[Union[str, int]], mock_feed: bool, test_video: Optional[str]) -> Union[str, int]:
        if mock_feed:
            return test_video or DEFAULT_TEST_VIDEO
        if source is not None:
            return source
        return self.config.get("camera_source", DEFAULT_TEST_VIDEO)

    def _is_stream(self, source: Union[str, int]) -> bool:
        return isinstance(source, str) and source.startswith(("rtsp://", "http://", "https://"))

    def _open_capture(self, source: Union[str, int]) -> cv2.VideoCapture:
        if isinstance(source, str) and not self._is_stream(source) and not Path(source).exists():
            raise FileNotFoundError(f"Video source not found: {source}")
        cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            raise RuntimeError(f"Unable to open source: {source}")
        return cap

    def _on_mouse_event(self, event: int, x: int, y: int, _flags: int, _param: Any) -> None:
        if not self.show_window:
            return
        if event == cv2.EVENT_LBUTTONDOWN:
            src_x = int(round(float(x) / max(self._display_scale_x, 1e-6)))
            src_y = int(round(float(y) / max(self._display_scale_y, 1e-6)))
            if self._frame_width > 0:
                src_x = max(0, min(self._frame_width - 1, src_x))
            if self._frame_height > 0:
                src_y = max(0, min(self._frame_height - 1, src_y))
            self.calibration_points.append((src_x, src_y))

    def _setup_window(self) -> None:
        if not self.show_window:
            return
        if not self.window_enabled:
            cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(WINDOW_NAME, 1280, 720)
            cv2.setMouseCallback(WINDOW_NAME, self._on_mouse_event)
            self.window_enabled = True
            logger.info("Calibration controls: click points, c=clear, s=save, q=quit")

    def _handle_window_keys(self, wait_delay_ms: int) -> bool:
        if not self.show_window:
            return False
        key = cv2.waitKey(max(1, int(wait_delay_ms))) & 0xFF
        if key == 255:
            return False

        if key == ord("q"):
            return True

        if key == ord("c"):
            self.calibration_points.clear()
            self.calibration_mode = True
            self.pause_for_calibration = True
            logger.info("Calibration polygon cleared")
            return False

        if key == ord("s"):
            if len(self.calibration_points) >= 4:
                updated = save_gate_polygon(self.calibration_points, path=self.config_path)
                self.config = updated
                self._refresh_active_polygon(updated.get("gate_polygon", []))
                self.calibration_points.clear()
                self.calibration_mode = False
                self.pause_for_calibration = False
                logger.info("Calibration polygon saved to config.json")
            else:
                logger.warning("Calibration save skipped: need at least 4 points")
        return False

    def _filter_detections_inside_polygon(self, detections: Any) -> Any:
        if detections is None or not hasattr(detections, "xyxy"):
            return detections
        if self.active_polygon_array is None:
            if len(detections) == 0:
                return detections
            mask = np.zeros(len(detections), dtype=bool)
            return detections[mask]

        boxes = detections.xyxy
        if boxes is None or len(boxes) == 0:
            return detections

        poly = self.active_polygon_array.astype(np.float32)
        keep = np.zeros(len(boxes), dtype=bool)
        for idx, box in enumerate(boxes):
            x1, y1, x2, y2 = box[:4]
            x_center = float((x1 + x2) / 2.0)
            y_bottom = float(y2)
            keep[idx] = cv2.pointPolygonTest(poly, (x_center, y_bottom), False) >= 0
        return detections[keep]

    def _run_inference(self, frame: np.ndarray):
        result = self.model.predict(
            source=frame,
            conf=self.confidence_threshold,
            iou=self.iou_threshold,
            device="cpu",
            batch=1,
            imgsz=640,
            verbose=False,
        )[0]

        if not SUPERVISION_AVAILABLE:
            return result

        detections = sv.Detections.from_ultralytics(result)
        if detections.class_id is not None:
            mask = np.isin(detections.class_id, list(VEHICLE_CLASS_IDS))
            detections = detections[mask]

        detections = self._filter_detections_inside_polygon(detections)

        if self.tracker is not None:
            detections = self.tracker.update_with_detections(detections)
        return detections

    def _warmup_model(self) -> None:
        dummy_frame = np.zeros((640, 640, 3), dtype=np.uint8)
        try:
            self.model.predict(
                source=dummy_frame,
                conf=self.confidence_threshold,
                iou=self.iou_threshold,
                device="cpu",
                batch=1,
                imgsz=640,
                verbose=False,
            )
            logger.info("Model warmup complete")
        except Exception:
            logger.exception("Model warmup failed; continuing without warmup")

    def _emit_entry_event(self, track_id: int, info: ActiveTrack) -> bool:
        if self.standby_mode:
            logger.info("[Standby] Entry alert suppressed for id=%s", track_id)
            return False
        message = self._build_summary_message(info.vehicle_type, track_id, 0, serviced=False)
        logger.info("[Vehicle Entry] id=%s type=%s gate=%s", track_id, info.vehicle_type, self.gate_id)
        return bool(self.notifier.enqueue_alert(message, track_id=track_id, gate_id=self.gate_id, alert_type="entry"))

    def _emit_service_event(self, track_id: int, info: ActiveTrack, dwell_seconds: float) -> bool:
        if self.standby_mode:
            logger.info("[Standby] Service alert suppressed for id=%s", track_id)
            return False
        message = self._build_summary_message(info.vehicle_type, track_id, dwell_seconds, serviced=True)
        logger.info(
            "[Vehicle Serviced] id=%s type=%s gate=%s dwell=%s fee=KES %.0f",
            track_id,
            info.vehicle_type,
            self.gate_id,
            self._format_dwell(dwell_seconds),
            self._fee_for_vehicle(info.vehicle_type),
        )
        return bool(self.notifier.enqueue_alert(message, track_id=track_id, gate_id=self.gate_id, alert_type="service"))

    def _flush_exited_tracks(self, now_ts: float) -> None:
        stale_ids = []
        for track_id, info in self.active_tracks.items():
            if (now_ts - info.last_seen_ts) > self.track_lost_timeout_seconds:
                stale_ids.append(track_id)

        for track_id in stale_ids:
            info = self.active_tracks.pop(track_id)
            exit_dt = datetime.now()
            dwell_seconds = max(1.0, (exit_dt - info.entry_dt).total_seconds())
            was_serviced = dwell_seconds >= self.dwell_threshold_seconds
            sms_ok = False
            if was_serviced and not info.service_notified:
                sms_ok = self._emit_service_event(track_id, info, dwell_seconds)

            logger.info(
                "[Vehicle Exit] id=%s type=%s gate=%s dwell=%s status=%s",
                track_id,
                info.vehicle_type,
                self.gate_id,
                self._format_dwell(dwell_seconds),
                "Serviced" if was_serviced else "Unserviced",
            )
            log_vehicle_exit(
                vehicle_type=info.vehicle_type,
                gate_id=self.gate_id,
                entry_dt=info.entry_dt,
                exit_dt=exit_dt,
                dwell_seconds=dwell_seconds,
                anomaly="SERVICED" if was_serviced else "UNSERVICED",
                sms_status=int(sms_ok),
                db_path=self.db_path,
            )

    def _track_gate_events(self, detections: Any) -> None:
        if not SUPERVISION_AVAILABLE or not hasattr(detections, "tracker_id"):
            return

        now = datetime.now()
        now_ts = time.time()
        tracker_ids = detections.tracker_id if detections.tracker_id is not None else []
        class_ids = detections.class_id if detections.class_id is not None else []

        for idx, track_id in enumerate(tracker_ids):
            if track_id is None:
                continue

            tid = int(track_id)
            class_id = int(class_ids[idx]) if len(class_ids) > idx else -1
            vehicle_type = self._vehicle_type_from_model(class_id) if class_id >= 0 else "Car"

            if tid not in self.active_tracks:
                self.active_tracks[tid] = ActiveTrack(entry_dt=now, last_seen_ts=now_ts, vehicle_type=vehicle_type)
                info = self.active_tracks[tid]
                if not info.entry_notified:
                    self._emit_entry_event(track_id=tid, info=info)
                    info.entry_notified = True
            else:
                self.active_tracks[tid].last_seen_ts = now_ts

            info = self.active_tracks[tid]
            info.last_seen_ts = now_ts
            current_dwell = (now - info.entry_dt).total_seconds()
            if current_dwell >= self.dwell_threshold_seconds and not info.service_notified:
                self._emit_service_event(track_id=tid, info=info, dwell_seconds=current_dwell)
                info.service_notified = True

        self._flush_exited_tracks(now_ts)

    @staticmethod
    def _draw_corner_brackets(
        canvas: np.ndarray,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        color: Tuple[int, int, int],
        thickness: int = 1,
    ) -> None:
        width = max(1, x2 - x1)
        height = max(1, y2 - y1)
        corner = max(6, int(min(width, height) * 0.2))

        cv2.line(canvas, (x1, y1), (x1 + corner, y1), color, thickness, cv2.LINE_AA)
        cv2.line(canvas, (x1, y1), (x1, y1 + corner), color, thickness, cv2.LINE_AA)

        cv2.line(canvas, (x2, y1), (x2 - corner, y1), color, thickness, cv2.LINE_AA)
        cv2.line(canvas, (x2, y1), (x2, y1 + corner), color, thickness, cv2.LINE_AA)

        cv2.line(canvas, (x1, y2), (x1 + corner, y2), color, thickness, cv2.LINE_AA)
        cv2.line(canvas, (x1, y2), (x1, y2 - corner), color, thickness, cv2.LINE_AA)

        cv2.line(canvas, (x2, y2), (x2 - corner, y2), color, thickness, cv2.LINE_AA)
        cv2.line(canvas, (x2, y2), (x2, y2 - corner), color, thickness, cv2.LINE_AA)

    def _draw_compact_hud(self, frame: np.ndarray, fps_value: float, active_count: int, alerts: List[str]) -> None:
        lines = [
            f"FPS {fps_value:.1f}",
            f"Active {active_count}",
        ]
        if alerts:
            suffix = "" if len(alerts) <= 2 else ", ..."
            lines.append(f"Alert {', '.join(alerts[:2])}{suffix}")

        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.45
        thickness = 1
        line_gap = 16

        text_widths = [cv2.getTextSize(line, font, scale, thickness)[0][0] for line in lines]
        max_text_width = max(text_widths) if text_widths else 0

        padding_x = 12
        padding_y = 8
        box_width = max_text_width + (padding_x * 2)
        box_height = (line_gap * len(lines)) + (padding_y * 2)

        right_margin = 10
        top_margin = 10
        x1 = max(8, frame.shape[1] - box_width - right_margin)
        y1 = top_margin
        x2 = min(frame.shape[1] - 8, x1 + box_width)
        y2 = y1 + box_height

        overlay = frame.copy()
        radius = max(6, min(12, box_height // 3))

        cv2.rectangle(overlay, (x1 + radius, y1), (x2 - radius, y2), (20, 20, 20), -1)
        cv2.circle(overlay, (x1 + radius, y1 + radius), radius, (20, 20, 20), -1)
        cv2.circle(overlay, (x2 - radius, y1 + radius), radius, (20, 20, 20), -1)
        cv2.circle(overlay, (x1 + radius, y2 - radius), radius, (20, 20, 20), -1)
        cv2.circle(overlay, (x2 - radius, y2 - radius), radius, (20, 20, 20), -1)

        cv2.addWeighted(overlay, 0.58, frame, 0.42, 0, frame)

        text_y = y1 + padding_y + 11
        for idx, line in enumerate(lines):
            color = (230, 230, 230)
            if idx == len(lines) - 1 and alerts:
                color = (120, 220, 255)
            cv2.putText(frame, line, (x1 + padding_x, text_y), font, scale, color, thickness, cv2.LINE_AA)
            text_y += line_gap

    def _build_display_canvas(self, frame: np.ndarray) -> np.ndarray:
        max_width = int(self.config.get("display_max_width", 1280))
        src_h, src_w = frame.shape[:2]
        if src_w > max_width:
            scale = max_width / float(src_w)
            target_size = (max(1, int(src_w * scale)), max(1, int(src_h * scale)))
            resized = cv2.resize(frame, target_size, interpolation=cv2.INTER_AREA)
            self._display_scale_x = target_size[0] / float(src_w)
            self._display_scale_y = target_size[1] / float(src_h)
            return resized

        self._display_scale_x = 1.0
        self._display_scale_y = 1.0
        return frame.copy()

    def _render_window(self, frame: np.ndarray, detections: Optional[Any], fps_value: float) -> np.ndarray:
        annotated = self._build_display_canvas(frame)
        overlay = annotated.copy()

        scale_x = self._display_scale_x
        scale_y = self._display_scale_y

        def _scale_point(point: Tuple[int, int]) -> Tuple[int, int]:
            return (int(round(point[0] * scale_x)), int(round(point[1] * scale_y)))

        draw_points = self.calibration_points if len(self.calibration_points) >= 3 else self.active_polygon_points
        if len(draw_points) >= 3:
            poly = np.array([_scale_point(pt) for pt in draw_points], dtype=np.int32)
            poly_color = (0, 170, 255) if len(self.calibration_points) >= 3 else (80, 220, 80)
            cv2.fillPoly(overlay, [poly], poly_color)
            cv2.polylines(annotated, [poly], isClosed=True, color=poly_color, thickness=1)

        if self.calibration_points:
            for idx, pt in enumerate(self.calibration_points, start=1):
                scaled_pt = _scale_point(pt)
                cv2.circle(annotated, scaled_pt, 4, (0, 170, 255), -1)
                cv2.putText(
                    annotated,
                    str(idx),
                    (scaled_pt[0] + 6, scaled_pt[1] - 6),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (0, 200, 255),
                    1,
                    cv2.LINE_AA,
                )

        annotated = cv2.addWeighted(overlay, 0.12, annotated, 0.88, 0)

        if detections is not None and SUPERVISION_AVAILABLE and hasattr(detections, "xyxy") and detections.xyxy is not None:
            boxes = detections.xyxy
            tracker_ids = detections.tracker_id if detections.tracker_id is not None else [None] * len(boxes)

            for idx, box in enumerate(boxes):
                x1, y1, x2, y2 = [int(v) for v in box[:4]]
                x1 = int(round(x1 * scale_x))
                y1 = int(round(y1 * scale_y))
                x2 = int(round(x2 * scale_x))
                y2 = int(round(y2 * scale_y))
                track_id = tracker_ids[idx] if idx < len(tracker_ids) else None

                dwell_text = "00:00"
                if track_id is not None and int(track_id) in self.active_tracks:
                    dwell_seconds = (datetime.now() - self.active_tracks[int(track_id)].entry_dt).total_seconds()
                    dwell_text = self._format_dwell_compact(dwell_seconds)

                self._draw_corner_brackets(annotated, x1, y1, x2, y2, (90, 235, 90), thickness=1)
                label_id = int(track_id) if track_id is not None else -1
                label = f"#{label_id if label_id >= 0 else '-'} | {dwell_text}"
                cv2.putText(
                    annotated,
                    label,
                    (x1, max(14, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (210, 255, 210),
                    1,
                    cv2.LINE_AA,
                )

        active_count = len(self.active_tracks)
        dwell_alerts: List[str] = []
        now = datetime.now()
        for track_id, info in self.active_tracks.items():
            dwell_seconds = (now - info.entry_dt).total_seconds()
            if dwell_seconds >= self.dwell_threshold_seconds and not info.service_notified:
                dwell_alerts.append(f"#{track_id} {self._format_dwell_compact(dwell_seconds)}")

        self._draw_compact_hud(annotated, fps_value=fps_value, active_count=active_count, alerts=dwell_alerts)
        return annotated

    def _pause_on_calibration(self, frame: np.ndarray, wait_delay_ms: int, fps_value: float) -> bool:
        if not self.show_window:
            return False
        frozen = frame.copy()
        while self.pause_for_calibration and not self._stop_event.is_set():
            rendered = self._render_window(frozen, None, fps_value=fps_value)
            cv2.imshow(WINDOW_NAME, rendered)
            if self._handle_window_keys(wait_delay_ms):
                return True
        return False

    def run(
        self,
        source: Optional[Union[str, int]] = None,
        mock_feed: bool = False,
        test_video: Optional[str] = None,
        show_window: bool = False,
        **_: Any,
    ) -> None:
        self._stop_event.clear()
        self._frame_signature_window.clear()
        self.show_window = bool(show_window)
        self.calibration_mode = False
        self.pause_for_calibration = False

        source_to_use = self._source_for_run(source=source, mock_feed=mock_feed, test_video=test_video)
        is_stream = self._is_stream(source_to_use)
        mode_label = "Live" if is_stream and not mock_feed else "Mock"

        backoff = 1.0
        max_backoff = 30.0
        frame_index = 0

        logger.info("Pipeline initialized: source=%s mode=%s gate=%s", source_to_use, mode_label, self.gate_id)
        if self.show_window:
            logger.info("Live window monitoring enabled")
        else:
            logger.info("Headless processing enabled")

        if not SUPERVISION_AVAILABLE:
            logger.error("Tracking requires supervision package; aborting run")
            return

        self._warmup_model()

        try:
            display_fps = 0.0
            last_display_ts = time.perf_counter()
            while not self._stop_event.is_set():
                cap = None
                stream_disconnect_started = None
                try:
                    cap = self._open_capture(source_to_use)
                    self._register_capture(cap)
                    if is_stream:
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
                    logger.info("Tracking started from source=%s", source_to_use)
                    dropped_frames = 0

                    source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                    if source_fps <= 1.0 or source_fps > 240.0:
                        source_fps = 30.0
                    wait_delay_ms = max(1, int(round(1000.0 / source_fps)))

                    if self.show_window:
                        self._setup_window()

                    while cap.isOpened() and not self._stop_event.is_set():
                        ret, frame = cap.read()
                        if not ret:
                            if is_stream:
                                if stream_disconnect_started is None:
                                    stream_disconnect_started = time.monotonic()
                                stall_elapsed = time.monotonic() - stream_disconnect_started
                                dropped_frames += 1
                                if stall_elapsed >= 5.0:
                                    logger.warning(
                                        "Camera disconnect detected for %.1fs; flushing active tracking state before reconnect",
                                        stall_elapsed,
                                    )
                                    if self.active_tracks:
                                        self._flush_exited_tracks(time.time())
                                        self.active_tracks.clear()
                                    raise RuntimeError("stream_disconnect_detected")
                                continue

                            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                            continue

                        stream_disconnect_started = None
                        self.last_frame_timestamp = time.time()
                        if is_stream and self._record_frame_signature(frame):
                            logger.warning(
                                "Camera stream freeze detected after %s identical frames; reconnecting (freeze_events=%s)",
                                self.freeze_frame_threshold,
                                self.freeze_events,
                            )
                            raise RuntimeError("stream_freeze_detected")
                        self._frame_height, self._frame_width = frame.shape[:2]
                        dropped_frames = 0
                        if self.show_window and frame_index == 0 and self.active_polygon_array is None:
                            self.calibration_mode = True
                            self.pause_for_calibration = True

                        now_perf = time.perf_counter()
                        instantaneous = 1.0 / max(now_perf - last_display_ts, 1e-6)
                        display_fps = instantaneous if display_fps <= 0 else ((0.90 * display_fps) + (0.10 * instantaneous))
                        self.current_fps = float(display_fps)
                        last_display_ts = now_perf

                        frame_index += 1
                        if self.show_window and self.pause_for_calibration:
                            if self._pause_on_calibration(frame, wait_delay_ms=wait_delay_ms, fps_value=display_fps):
                                return

                        if frame_index % self.frame_skip != 0:
                            if self.show_window:
                                rendered = self._render_window(frame, None, fps_value=display_fps)
                                cv2.imshow(WINDOW_NAME, rendered)
                                if self._handle_window_keys(wait_delay_ms):
                                    return
                            continue

                        detections = self._run_inference(frame)
                        self._track_gate_events(detections)

                        if self.show_window:
                            rendered = self._render_window(frame, detections, fps_value=display_fps)
                            cv2.imshow(WINDOW_NAME, rendered)
                            if self._handle_window_keys(wait_delay_ms):
                                return

                    if self._stop_event.is_set() or not is_stream:
                        return
                except FileNotFoundError:
                    logger.error("Tracking stopped: local source missing (%s)", source_to_use)
                    return
                except Exception:
                    if self._stop_event.is_set():
                        return
                    if not is_stream:
                        logger.exception("Tracking stopped: local source failure")
                        return
                    logger.warning("Stream interrupted; reconnecting in %.1fs", backoff)
                    self._release_capture(cap)
                    if self.active_tracks:
                        self._flush_exited_tracks(time.time())
                        self.active_tracks.clear()
                    if self._stop_event.wait(backoff):
                        return
                    backoff = min(max_backoff, backoff * 2.0)
                    continue
                finally:
                    self._release_capture(cap)
        finally:
            if self.show_window and self.window_enabled:
                cv2.destroyWindow(WINDOW_NAME)


VehicleTracker = Tracker


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Tracker().run()
