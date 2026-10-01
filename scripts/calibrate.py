import argparse
import json
from pathlib import Path

import cv2
import numpy as np

WINDOW_NAME = "Tektally Gate Calibration"
DEFAULT_SOURCE = Path("tests") / "sample_gate.mp4"


def read_first_frame(source_path: Path):
    if not source_path.exists():
        raise FileNotFoundError(f"Source not found: {source_path}")

    capture = cv2.VideoCapture(str(source_path))
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open video source: {source_path}")

    try:
        for _ in range(60):
            ok, frame = capture.read()
            if ok and frame is not None:
                return frame
    finally:
        capture.release()

    raise RuntimeError(f"No valid frames found in source: {source_path}")


def polygon_is_sane(points):
    if len(points) < 3:
        return False

    x_coords = [float(x) for x, _ in points]
    y_coords = [float(y) for _, y in points]
    if min(x_coords) == max(x_coords) or min(y_coords) == max(y_coords):
        return False

    area = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    area = abs(area) / 2.0
    if area <= 0:
        return False
    return True


def draw_instruction_header(display, source_path, frame_shape, point_count):
    height, width = frame_shape[:2]
    header_h = 90
    header = display.copy()
    cv2.rectangle(header, (0, 0), (width, header_h), (15, 15, 20), -1)
    cv2.addWeighted(header, 0.78, display, 0.22, 0, display)

    if point_count >= 3:
        status = "READY TO SAVE"
        status_color = (50, 220, 140)
    elif point_count > 0:
        status = "DRAWING"
        status_color = (90, 190, 255)
    else:
        status = "DRAWING"
        status_color = (255, 190, 90)

    src_label = str(source_path)
    cv2.putText(display, f"Source: {src_label}", (18, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (245, 245, 245), 1, cv2.LINE_AA)
    cv2.putText(display, f"Frame: {width}x{height}", (18, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (220, 220, 220), 1, cv2.LINE_AA)
    cv2.putText(display, f"Points: {point_count} | Mode: {status}", (18, 76), cv2.FONT_HERSHEY_SIMPLEX, 0.48, status_color, 1, cv2.LINE_AA)

    key_text = "[Left Click] Add Point | [C] Clear | [Z] Undo | [S] Save & Exit | [Q] Quit"
    cv2.putText(display, key_text, (width - 600, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (245, 245, 245), 1, cv2.LINE_AA)


def draw_polygon_overlay(display, points):
    if len(points) < 3:
        return

    poly = np.array(points, dtype=np.int32)
    overlay = display.copy()
    cv2.fillPoly(overlay, [poly], (33, 205, 115))
    cv2.addWeighted(overlay, 0.30, display, 0.70, 0, display)
    cv2.polylines(display, [poly], True, (40, 230, 120), 2, cv2.LINE_AA)

    for index, (x, y) in enumerate(points, start=1):
        x = int(x)
        y = int(y)
        cv2.circle(display, (x, y), 11, (255, 255, 255), -1, cv2.LINE_AA)
        cv2.circle(display, (x, y), 14, (40, 230, 120), 2, cv2.LINE_AA)
        cv2.putText(display, str(index), (x + 12, y - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (30, 30, 30), 2, cv2.LINE_AA)

    if len(points) > 2:
        first = points[0]
        last = points[-1]
        cv2.line(display, (int(first[0]), int(first[1])), (int(last[0]), int(last[1])), (40, 230, 120), 2, cv2.LINE_AA)


def draw_scene(frame, ground_points, source_path):
    display = frame.copy()
    draw_instruction_header(display, source_path, frame.shape, len(ground_points))

    if ground_points:
        draw_polygon_overlay(display, ground_points)
    else:
        cv2.putText(
            display,
            "Click to place the gate boundary on the ground surface",
            (18, 110),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (240, 240, 240),
            2,
            cv2.LINE_AA,
        )

    return display


def save_calibration(config_path: Path, ground_points):
    config_path = config_path.resolve()
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            try:
                config = json.load(handle)
            except json.JSONDecodeError:
                config = {}
    else:
        config = {}

    if not isinstance(config, dict):
        config = {}

    normalized = [[int(round(float(x))), int(round(float(y)))] for x, y in ground_points]
    config.pop("gate_3d_bounds", None)
    config["gate_polygon"] = normalized

    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


def main():
    parser = argparse.ArgumentParser(description="Interactive gate calibration for arbitrary 2D ground-plane geometry.")
    parser.add_argument("--source", type=str, default=str(DEFAULT_SOURCE), help="Path to the video file or RTSP stream")
    parser.add_argument("--config", type=str, default="config.json", help="Path to the config JSON file")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    source_path = Path(args.source)
    if not source_path.is_absolute():
        source_path = (root_dir / source_path).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (root_dir / config_path).resolve()

    try:
        frame = read_first_frame(source_path)
    except Exception as exc:  # pragma: no cover - CLI safety guard
        print(f"[ERROR] {exc}")
        raise SystemExit(1)

    ground_points = []

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            ground_points.append([int(x), int(y)])
            print(f"[POINT {len(ground_points)}] ({x}, {y})")
        elif event == cv2.EVENT_RBUTTONDOWN:
            if ground_points:
                ground_points.pop()
                print("[UNDO] Removed last point")

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, 1280, 720)
    cv2.setMouseCallback(WINDOW_NAME, on_mouse)

    should_save = False
    while True:
        display = draw_scene(frame, ground_points, source_path)
        cv2.imshow(WINDOW_NAME, display)

        key = cv2.waitKey(20) & 0xFF
        if key in (ord("q"), ord("Q"), 27):
            break
        if key in (ord("c"), ord("C")):
            ground_points.clear()
            print("[RESET] Gate calibration cleared.")
        elif key in (ord("z"), ord("Z")):
            if ground_points:
                ground_points.pop()
                print("[UNDO] Removed last point")
        elif key in (ord("s"), ord("S")):
            if len(ground_points) < 3:
                print("[WARNING] Need at least 3 points before saving.")
                continue
            if not polygon_is_sane(ground_points):
                print("[WARNING] Polygon geometry is not valid enough for calibration. Use a non-self-intersecting area.")
                continue
            should_save = True
            break

    cv2.destroyAllWindows()

    if should_save:
        save_calibration(config_path, ground_points)
        print(f"[SUCCESS] Saved gate polygon to {config_path}")
    else:
        print("[CANCELLED] Calibration not saved.")


if __name__ == "__main__":
    main()

