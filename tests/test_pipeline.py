try:
    import cv2
except Exception:
    cv2 = None
import json
import os
import sqlite3
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

try:
    import numpy as np
except Exception:
    np = None

try:
    import supervision as sv
except Exception:
    sv = None

try:
    from ultralytics import YOLO
except Exception:
    YOLO = None

__test__ = False


class ByteTrackSmokeTests(unittest.TestCase):
    def test_tracker_initializes_byte_track_without_deprecation_path(self):
        try:
            from gatekeeper.tracker import Tracker
        except Exception as exc:  # pragma: no cover - import guard for optional runtime deps
            self.skipTest(f"Tracker unavailable in this environment: {exc}")

        tracker = Tracker(config_path="config.json", db_path="data/test_gatekeeper_unit.db")
        self.addCleanup(tracker.stop)
        self.assertIsNotNone(tracker)
        self.assertIsNotNone(tracker.tracker)
        self.assertTrue(hasattr(tracker.tracker, "update_with_detections"))


# 1. Configuration & Credentials
AT_API_KEY = os.getenv("TEST_AT_API_KEY", "sandbox_key_mock")
MANAGER_PHONE_NUMBER = os.getenv("TEST_MANAGER_PHONE", "+10000000000")

PRICING_TIERS = {
    "CAR": 500.0,
    "MOTORCYCLE": 200.0,
    "BUS": 1000.0,
    "TRUCK": 1200.0,
    "DEFAULT": 500.0,
}

# Production Anomaly Thresholds in Minutes
SHORT_DWELL_MINS = 3.0   # Less than 3 mins = suspicious quick rinse
LONG_DWELL_MINS = 25.0   # More than 25 mins = Gate blockage / worker slacking

# Test dwell threshold in seconds (Set to 480 for 8 mins in production)
TEST_DWELL_THRESHOLD_SECS = 1


def get_daily_summary(db_cursor):
    """Queries today's vehicle count and running revenue projection."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    db_cursor.execute(
        """
        SELECT vehicle_type, COUNT(*) 
        FROM carwash_audit 
        WHERE DATE(entry_time) = ? 
        GROUP BY vehicle_type
    """,
        (today_str,),
    )

    rows = db_cursor.fetchall()
    total_vehicles = sum(count for _, count in rows)
    total_revenue = sum(
        count * PRICING_TIERS.get(v_type.upper(), PRICING_TIERS["DEFAULT"])
        for v_type, count in rows
    )

    return total_vehicles, total_revenue


def send_sms_alert(
    vehicle_id, vehicle_type, dwell_seconds, today_count, today_revenue, anomaly_tag=""
):
    """Dispatches Compact Option 2 SMS alert via Africa's Talking REST API."""
    url = "https://api.sandbox.africastalking.com/version1/messaging"
    timestamp_str = datetime.now().strftime("%H:%M")
    dwell_mins = round(dwell_seconds / 60.0, 1)
    dwell_display = f"{dwell_mins}m" if dwell_mins >= 1 else f"{int(dwell_seconds)}s"
    
    anomaly_header = f" [{anomaly_tag}]" if anomaly_tag else ""

    message = (
        f"gatekeeper Alert{anomaly_header}: {vehicle_type} (#{vehicle_id}) finished in Gate 1 at {timestamp_str} (Dwell: {dwell_display}).\n\n"
        f"Today's Running Total (as of {timestamp_str}):\n"
        f"Washed: {today_count} cars | Est. Revenue: KES {today_revenue:,.0f}\n\n"
        f"gatekeeper by Teklova"
    )

    headers = {
        "apiKey": AT_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    payload = urllib.parse.urlencode(
        {"username": "sandbox", "to": MANAGER_PHONE_NUMBER, "message": message}
    ).encode("utf-8")

    try:
        req = urllib.request.Request(
            url, data=payload, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            print(
                f"ðŸ“± [SMS DISPATCHED] Status: {res_data['SMSMessageData']['Message']}"
            )
            return True
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(
            f"âš ï¸ [SMS API HTTP {e.code}] {error_body} (Verify Web Simulator status)"
        )
        print(f"ðŸ“² [SIMULATED SMS LOGGED]:\n{message}")
        return False
    except Exception as e:
        print(f"âš ï¸ [SMS DISPATCH ERROR] {e}")
        return False


def main():
    # 2. Database Initialization
    db_conn = sqlite3.connect("gatekeeper.db")
    db_cursor = db_conn.cursor()

    db_cursor.execute("""
        CREATE TABLE IF NOT EXISTS carwash_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER,
            vehicle_type TEXT,
            gate_id TEXT,
            entry_time TEXT,
            exit_time TEXT,
            dwell_seconds REAL,
            sms_sent INTEGER,
            is_synced INTEGER,
            anomaly_type TEXT DEFAULT 'NORMAL',
            unpaid_flag INTEGER DEFAULT 1
        )
    """)
    db_conn.commit()

    # Ensure schema compatibility if table already exists
    try:
        db_cursor.execute("ALTER TABLE carwash_audit ADD COLUMN anomaly_type TEXT DEFAULT 'NORMAL'")
        db_cursor.execute("ALTER TABLE carwash_audit ADD COLUMN unpaid_flag INTEGER DEFAULT 1")
        db_conn.commit()
    except sqlite3.OperationalError:
        pass

    # 3. Model & Tracker Setup
    model = YOLO("yolo11n_openvino_model/", task="detect")
    tracker = sv.ByteTrack()

    # 4. Polygon Zone & Annotators Setup
    polygon = np.array([[100, 100], [700, 100], [700, 500], [100, 500]])
    zone = sv.PolygonZone(polygon=polygon)
    zone_annotator = sv.PolygonZoneAnnotator(zone=zone, color=sv.Color.GREEN)
    box_annotator = sv.BoxAnnotator()
    label_annotator = sv.LabelAnnotator()

    active_vehicles = {}

    cap = cv2.VideoCapture("test_video.mp4")

    if not cap.isOpened():
        print("Error: Could not open test_video.mp4.")
        db_conn.close()
        return

    print("--- Starting gatekeeper Audit Pipeline (Option 2 SMS + Fraud Detection) ---")
    print("Press 'q' on the video window to quit.\n")

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            results = model(frame, conf=0.25, verbose=False)[0]
            detections = sv.Detections.from_ultralytics(results)

            detections = detections[np.isin(detections.class_id, [2, 3, 5, 7])]
            detections = tracker.update_with_detections(detections)

            is_inside = zone.trigger(detections=detections)

            if detections.tracker_id is not None and len(detections.tracker_id) > 0:
                for track_id, inside, class_id in zip(
                    detections.tracker_id, is_inside, detections.class_id
                ):
                    current_time = time.time()
                    vehicle_type = model.names[class_id].upper()

                    if inside:
                        if track_id not in active_vehicles:
                            entry_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            active_vehicles[track_id] = {
                                "start_time": current_time,
                                "entry_str": entry_str,
                                "vehicle_type": vehicle_type,
                                "alert_triggered": False,
                            }
                            print(
                                f"ðŸŸ¢ [ENTRY] Vehicle ID #{track_id} ({vehicle_type}) entered Gate 1 at {entry_str}."
                            )
                        else:
                            vehicle_data = active_vehicles[track_id]
                            elapsed = current_time - vehicle_data["start_time"]

                            if (
                                elapsed >= TEST_DWELL_THRESHOLD_SECS
                                and not vehicle_data["alert_triggered"]
                            ):
                                exit_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                dwell_seconds = round(elapsed, 2)
                                dwell_mins = round(dwell_seconds / 60.0, 2)

                                if dwell_mins < SHORT_DWELL_MINS:
                                    anomaly_type = "QUICK_RINSE_ALERT"
                                elif dwell_mins > LONG_DWELL_MINS:
                                    anomaly_type = "GATE_OVERSTAY_ALERT"
                                else:
                                    anomaly_type = "NORMAL"

                                db_cursor.execute(
                                    """
                                    INSERT INTO carwash_audit 
                                    (vehicle_id, vehicle_type, gate_id, entry_time, exit_time, dwell_seconds, sms_sent, is_synced, anomaly_type, unpaid_flag)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                    (
                                        int(track_id),
                                        vehicle_type,
                                        "Gate 1",
                                        vehicle_data["entry_str"],
                                        exit_str,
                                        dwell_seconds,
                                        0,
                                        0,
                                        anomaly_type,
                                        1,
                                    ),
                                )
                                db_conn.commit()

                                today_count, today_revenue = get_daily_summary(db_cursor)

                                anomaly_tag = "âš ï¸ QUICK RINSE" if anomaly_type == "QUICK_RINSE_ALERT" else (
                                    "âš ï¸ Gate OVERSTAY" if anomaly_type == "GATE_OVERSTAY_ALERT" else ""
                                )

                                sms_success = send_sms_alert(
                                    track_id,
                                    vehicle_type,
                                    elapsed,
                                    today_count,
                                    today_revenue,
                                    anomaly_tag=anomaly_tag,
                                )

                                if sms_success:
                                    db_cursor.execute(
                                        "UPDATE carwash_audit SET sms_sent = 1 WHERE vehicle_id = ? AND exit_time = ?",
                                        (int(track_id), exit_str),
                                    )
                                    db_conn.commit()

                                vehicle_data["alert_triggered"] = True
                    else:
                        if (
                            track_id in active_vehicles
                            and not active_vehicles[track_id]["alert_triggered"]
                        ):
                            del active_vehicles[track_id]

            labels = (
                [
                    f"ID #{track_id} {model.names[class_id]}"
                    for class_id, track_id in zip(
                        detections.class_id, detections.tracker_id
                    )
                ]
                if detections.tracker_id is not None
                else []
            )

            annotated_frame = box_annotator.annotate(scene=frame, detections=detections)
            annotated_frame = label_annotator.annotate(scene=annotated_frame, detections=detections, labels=labels)
            annotated_frame = zone_annotator.annotate(scene=annotated_frame)

            cv2.imshow("Car Wash CV Audit System", annotated_frame)

            if cv2.waitKey(30) & 0xFF == ord("q"):
                break
    finally:
        cap.release()
        db_conn.close()
        cv2.destroyAllWindows()
        print("--- Pipeline Stopped & DB Connection Closed ---")


if __name__ == "__main__":
    unittest.main()

