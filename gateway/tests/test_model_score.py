import json

import gateway
import model_score
from room_camera import tidy_body

GATEWAY_ENV = {"PORT": "x", "YOLO_MODEL": "gateway/yolov8s.onnx", "DESK_AREA": "0.25,0.22,0.64,0.83", "DESK_PHOTO": "1",
               "DESK_LIGHT": "ssim", "DESK_THRESH": "25", "DESK_MOVE": "1", "ROOM_ITEMS": "remote:1",
               "VLM_URL": "http://127.0.0.1:8091"}
WINNER = ("YOLO_MODEL=/home/suyeon/models/yolov8s.onnx DESK_PHOTO=1 DESK_AREA=0.25,0.22,0.64,0.83 DESK_LIGHT=ssim "
          "DESK_THRESH=25 DESK_MOVE=1 ROOM_ITEMS=remote:1 VLM_NAME=qwen VLM_RULE=or")


def write(tmp_path, *records):
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records) + "{broken\n")
    return str(path)


def record(candidate, accuracy, at):
    return {"at": at, "candidate": candidate, "leftAccuracy": accuracy, "accuracy": 0.66, "cases": 47,
            "medianSeconds": 33.456, "serious": 6, "wrong": ["004"]}


def test_finds_latest_score_of_the_same_setup(tmp_path):
    path = write(tmp_path, record(WINNER, 0.85, "2026-11-12T09:00:00+09:00"),
                 record("YOLO_MODEL=gateway/yolov8s.onnx", 0.766, "2026-11-12T09:30:00+09:00"),
                 record(WINNER, 0.8723, "2026-11-12T10:16:00+09:00"))
    score = model_score.find(gateway.load_config(GATEWAY_ENV), gateway.load_config, path)
    assert score == {"setup": WINNER, "accuracy": 0.8723, "cases": 47, "secondsPerPhoto": 33.46,
                     "measuredAt": "2026-11-12T10:16:00+09:00"}
    assert "87.2%" in model_score.describe(score)


def test_no_score_when_setup_differs(tmp_path):
    path = write(tmp_path, record(WINNER, 0.8723, "2026-11-12T10:16:00+09:00"))
    no_vlm = {k: v for k, v in GATEWAY_ENV.items() if k != "VLM_URL"}
    assert model_score.find(gateway.load_config(no_vlm), gateway.load_config, path) is None
    other_thresh = {**GATEWAY_ENV, "DESK_THRESH": "35"}
    assert model_score.find(gateway.load_config(other_thresh), gateway.load_config, path) is None
    assert model_score.find(gateway.load_config(GATEWAY_ENV), gateway.load_config, str(tmp_path / "none")) is None
    assert "안 보내요" in model_score.describe(None)


def test_tidy_results_can_be_turned_off():
    assert gateway.load_config({"PORT": "x"}).tidy_results == "eval/tidy/results.jsonl"
    assert gateway.load_config({"PORT": "x", "TIDY_RESULTS": "off"}).tidy_results is None
    assert model_score.find(gateway.load_config({"PORT": "x"}), gateway.load_config, None) is None


def test_score_rides_with_the_tidy_check_only_when_known():
    check = {"reservationId": 37, "phase": "FIRST", "leftItems": {}, "trash": {}, "checkedAt": "2026-11-12T13:00:00+09:00"}
    assert "model" not in tidy_body(check, "camera-1", "e1")
    score = {"setup": WINNER, "accuracy": 0.8723, "cases": 47, "secondsPerPhoto": 33.46, "measuredAt": "x"}
    assert tidy_body(check, "camera-1", "e1", model=score)["model"] == score
