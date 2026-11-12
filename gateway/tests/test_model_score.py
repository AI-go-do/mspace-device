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


QWEN = "Qwen3-VL-2B-Instruct-Q8_0.gguf"


def record(candidate, accuracy, at, vlm_model=QWEN):
    return {"at": at, "candidate": candidate, "leftAccuracy": accuracy, "accuracy": 0.66, "cases": 47,
            "medianSeconds": 33.456, "serious": 6, "wrong": ["004"], "vlmModel": vlm_model}


def find(env, path, vlm_model=QWEN):
    return model_score.find(gateway.load_config(env), gateway.load_config, path, vlm_model=vlm_model)


def test_finds_latest_score_of_the_same_setup(tmp_path):
    path = write(tmp_path, record(WINNER, 0.85, "2026-11-12T09:00:00+09:00"),
                 record("YOLO_MODEL=gateway/yolov8s.onnx", 0.766, "2026-11-12T09:30:00+09:00"),
                 record(WINNER, 0.8723, "2026-11-12T10:16:00+09:00"),
                 {**record(WINNER, None, "2026-11-12T11:00:00+09:00")},  # 숫자가 이상한 줄은 건너뛴다
                 record(WINNER, 0.99, "2026-11-12T11:30:00+09:00", vlm_model="other-vlm.gguf"))  # 다른 VLM 모델
    score = find(GATEWAY_ENV, path)
    assert score == {"setup": WINNER, "accuracy": 0.8723, "cases": 47, "secondsPerPhoto": 33.46,
                     "measuredAt": "2026-11-12T10:16:00+09:00", "presence": None}  # 옛 줄엔 경우별 결과가 없음
    assert "87.2%" in model_score.describe(score)


def test_bad_numbers_keep_the_last_good_score(tmp_path):
    good = record(WINNER, 0.8723, "2026-11-12T10:16:00+09:00")
    path = write(tmp_path, good,
                 {**good, "medianSeconds": float("nan")}, {**good, "medianSeconds": -1},
                 {**good, "accuracy": 0.9, "cases": float("inf")}, {**good, "leftAccuracy": float("nan")})
    assert find(GATEWAY_ENV, path)["accuracy"] == 0.8723


def test_no_score_when_setup_differs(tmp_path):
    path = write(tmp_path, record(WINNER, 0.8723, "2026-11-12T10:16:00+09:00"))
    no_vlm = {k: v for k, v in GATEWAY_ENV.items() if k != "VLM_URL"}
    assert find(no_vlm, path, vlm_model=None) is None
    assert find(GATEWAY_ENV, path, vlm_model=None) is None  # VLM_URL이 있어도 make_vlm이 껐으면 VLM 점수는 안 씀
    assert find({**GATEWAY_ENV, "DESK_THRESH": "35"}, path) is None
    assert find(GATEWAY_ENV, path, vlm_model="") is None  # 모델 이름을 못 읽으면 안 보냄
    assert find(GATEWAY_ENV, str(tmp_path / "none")) is None
    assert find(GATEWAY_ENV, str(tmp_path)) is None  # 폴더를 줘도 게이트웨이는 계속 켜진다


def test_old_records_match_by_vlm_name(tmp_path):
    old = {k: v for k, v in record(WINNER, 0.8723, "2026-11-12T10:16:00+09:00").items() if k != "vlmModel"}
    path = write(tmp_path, old)
    assert find(GATEWAY_ENV, path)["accuracy"] == 0.8723  # VLM_NAME=qwen 이 Qwen3-VL-... 안에 있음: 다시 채점 안 해도 됨
    assert find(GATEWAY_ENV, path, vlm_model="/home/suyeon/vlm/" + QWEN)["accuracy"] == 0.8723  # 서버가 경로로 알려 줘도
    assert find(GATEWAY_ENV, path, vlm_model="SmolVLM-500M.gguf") is None  # 다른 모델이면 안 씀
    assert find(GATEWAY_ENV, path, vlm_model="Qwen2-VL-2B-Instruct-Q8_0.gguf") is None  # 이름에 qwen이 있어도 다른 모델
    assert find(GATEWAY_ENV, path, vlm_model="Qwen3-VL-2B-Instruct-Q4_K_M.gguf") is None  # 양자화가 달라도 다른 모델
    no_name = {**old, "candidate": WINNER.replace(" VLM_NAME=qwen", "")}
    assert find(GATEWAY_ENV, write(tmp_path, no_name)) is None  # 이름도 없으면 어떤 모델인지 몰라서 안 씀


def test_unreadable_file_keeps_the_gateway_running(tmp_path, monkeypatch, capsys):
    path = write(tmp_path, record(WINNER, 0.8723, "x"))

    def broken(*a, **k):
        raise PermissionError("no")
    monkeypatch.setattr("builtins.open", broken)
    assert find(GATEWAY_ENV, path) is None
    assert "못 읽어서" in capsys.readouterr().out
    assert "안 보내요" in model_score.describe(None)


def test_tidy_results_can_be_turned_off():
    assert gateway.load_config({"PORT": "x"}).tidy_results == "eval/tidy/results.jsonl"
    assert gateway.load_config({"PORT": "x", "TIDY_RESULTS": "off"}).tidy_results is None
    assert find({"PORT": "x"}, None, vlm_model=None) is None


def test_score_rides_with_the_tidy_check_only_when_known():
    check = {"reservationId": 37, "phase": "FIRST", "leftItems": {}, "trash": {}, "checkedAt": "2026-11-12T13:00:00+09:00"}
    assert "model" not in tidy_body(check, "camera-1", "e1")
    score = {"setup": WINNER, "accuracy": 0.8723, "cases": 47, "secondsPerPhoto": 33.46, "measuredAt": "x"}
    assert tidy_body(check, "camera-1", "e1", model=score)["model"] == score


def rows(key, left, wrong, count=False):
    return ([{"left": True, "found": True, "key": key, "count": count}] * left
            + [{"left": False, "found": True, "key": key, "count": count}] * wrong)


def test_presence_table_is_the_rate_things_were_really_left():
    table = model_score.presence_table(
        rows("yolo+desk+vlm", 9, 1, count=True) + rows("vlm", 3, 0)  # VLM만: 3번 다 맞았지만 문제가 적다
        + rows("yolo+vlm-no", 4, 2, count=True) + [{"left": False, "found": False, "key": "none", "count": False}])
    assert table == {"yolo+desk+vlm": 0.9, "vlm": model_score.LOW, "yolo+vlm-no": 0.667, "recheck": 0.812}
    assert model_score.presence_table(None) is None and model_score.presence_table([]) is None
    assert model_score.presence_table(["bad", {"found": True}]) is None
    assert "yolo+desk+vlm 90%" in model_score.describe_presence(table)
    assert "예전처럼" in model_score.describe_presence(None)


def test_find_keeps_the_presence_table_of_that_record(tmp_path):
    path = write(tmp_path, {**record(WINNER, 0.8723, "2026-11-12T15:00:00+09:00"),
                            "evidence": rows("yolo+desk+vlm", 9, 1, count=True)})
    assert find(GATEWAY_ENV, path)["presence"] == {"yolo+desk+vlm": 0.9, "recheck": 0.9}


def test_presence_replaces_name_scores_with_the_chance_something_is_there():
    check = {"reservationId": 37, "phase": "FIRST", "leftItems": {}, "trash": {"cup": 1, "bottle": 1},
             "confidence": {"cup": [0.57], "bottle": [0.39]}, "evidence": {"yolo": True, "desk": True}, "vlm": True,
             "checkedAt": "2026-11-12T13:00:00+09:00"}
    table = {"yolo+desk+vlm": 0.9, "recheck": 0.85}
    body = tidy_body(check, "camera-1", "e1", presence=table)
    assert [i["confidence"] for i in body["items"]] == [0.9, 0.9] and {i["name"] for i in body["items"]} == {"cup", "bottle"}
    assert [i["confidence"] for i in tidy_body(check, "camera-1", "e1")["items"]] == [0.57, 0.39]  # 표가 없으면 예전대로
    unseen = {**check, "vlm": None}  # VLM이 못 알아들은 경우는 표에 없음 → 알림만
    assert {i["confidence"] for i in tidy_body(unseen, "camera-1", "e1", presence=table)["items"]} == {0.5}
    recheck = {**check, "phase": "RECHECK", "evidence": {"yolo": True, "desk": False}}
    recheck.pop("vlm")
    assert {i["confidence"] for i in tidy_body(recheck, "camera-1", "e1", presence=table)["items"]} == {0.85}


def test_evidence_keys():
    from room_camera import evidence_key
    assert evidence_key({"yolo": True, "desk": False}) == "yolo"
    assert evidence_key({"yolo": False, "desk": True}, vlm=False) == "desk+vlm-no"
    assert evidence_key({}, vlm=True) == "vlm"
    assert evidence_key({"yolo": True}, vlm=None) == "yolo+vlm-?"
    assert evidence_key({"yolo": True}, phase="RECHECK") == "recheck"
def test_null_vlm_model_is_not_an_old_record(tmp_path):
    """vlmModel 칸이 null이면 VLM 없이 채점한 줄이라, VLM_NAME=qwen이 붙어 있어도 VLM 켠 게이트웨이에 안 붙인다."""
    path = write(tmp_path, record(WINNER, 0.9, "2026-11-12T12:00:00+09:00", vlm_model=None))
    assert find(GATEWAY_ENV, path) is None


def test_vlm_url_only_records_count_as_vlm_on(tmp_path):
    """VLM_URL만 주고 채점해도 VLM 켠 설정으로 저장·조회된다."""
    import tidy_eval
    env = {k: v for k, v in (p.split("=", 1) for p in WINNER.split()) if k not in ("VLM_NAME", "VLM_RULE")}
    candidate = tidy_eval.candidate_name({**env, "VLM_URL": "http://127.0.0.1:8091"})
    assert "VLM_RULE=or" in candidate and "VLM_URL" not in candidate
    path = write(tmp_path, record(candidate, 0.9, "2026-11-12T12:00:00+09:00"))
    assert find(GATEWAY_ENV, path)["accuracy"] == 0.9
    assert find({k: v for k, v in GATEWAY_ENV.items() if k != "VLM_URL"}, path, vlm_model=None) is None


def test_recheck_counts_only_rooms_the_gateway_would_recheck():
    """첫 확인이 깨끗함이면(예: DESK_MOVE로 옮긴 비품을 뺌) 게이트웨이는 다시 확인하지 않으니 recheck에 안 센다."""
    moved = [{"left": False, "found": False, "key": "none", "count": True, "target": False}] * 5
    table = model_score.presence_table(rows("yolo", 5, 0, count=True) + moved)
    assert table["recheck"] == 1.0


def test_empty_table_is_not_an_old_record():
    """근거는 있는데 남음이 한 번도 없으면 빈 표: 옛 이름 점수 대신 모두 0.5(알림만)."""
    table = model_score.presence_table([{"left": False, "found": False, "key": "none", "count": False}])
    assert table == {}
    check = {"reservationId": 37, "phase": "RECHECK", "leftItems": {}, "trash": {"cup": 1},
             "confidence": {"cup": [0.95]}, "evidence": {"yolo": True, "desk": False}, "checkedAt": "x"}
    assert tidy_body(check, "camera-1", "e1", presence=table)["items"][0]["confidence"] == 0.5
    assert "모두 50%" in model_score.describe_presence(table)
