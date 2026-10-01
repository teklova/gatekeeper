from pathlib import Path
import shutil

from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent.parent
MODEL_NAME = "yolo11n.pt"
OPENVINO_DIR = ROOT / "models" / "yolo11n_openvino_model"

# 1. Download the base PyTorch model weights (yolo11n.pt) automatically.
model = YOLO(MODEL_NAME)

# 2. Convert PyTorch model into Intel OpenVINO FP16 (half-precision) format.
export_path = Path(model.export(format="openvino", half=True))

# 3. Keep exported artifacts under repo models/ for reproducible local and Docker runs.
OPENVINO_DIR.parent.mkdir(parents=True, exist_ok=True)
if export_path.resolve() != OPENVINO_DIR.resolve():
	if OPENVINO_DIR.exists():
		for item in OPENVINO_DIR.iterdir():
			if item.is_file():
				item.unlink()
			elif item.is_dir():
				shutil.rmtree(item)
		OPENVINO_DIR.rmdir()
	export_path.rename(OPENVINO_DIR)

print(f"--- OpenVINO Export Complete: {OPENVINO_DIR} ---")