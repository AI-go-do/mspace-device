import csv
import json

import desk_diff
import numpy as np
import pytest
import tidy_eval as te
from room_camera import Detection


def det(label, x=10):
    return Detection(label, 0.9, (x, 10, 20, 20))


def frame(value):
    return np.full((120, 160, 3), value, dtype=np.uint8)


def finder(table):
    """사진 밝기 값 → 찾은 것 목록 (모델 대신)."""
    return lambda f: table[int(f[0, 0, 0])]


@pytest.mark.parametrize(("answer", "got", "serious"), [
    ("CLEAN", "NEEDS_CLEANING", True),
    ("CLEAN", "LEFT_ITEMS", True),
    ("NEEDS_CLEANING", "CLEAN", True),
    ("LEFT_ITEMS", "CLEAN", True),
    ("NEEDS_CLEANING", "LEFT_ITEMS", False),
    ("CLEAN", "CLEAN", False),
])
def test_serious_errors(answer, got, serious):
    assert te.is_serious(answer, got) is serious


def test_judge_compares_with_before_photo():
    """입실 전부터 있던 리모컨은 빼고, 새로 생긴 컵만 치울 것으로 본다."""
    find = finder({0: [det("remote")], 1: [det("remote"), det("cup", 60)], 2: [det("remote", 70)]})
    check, seconds = te.judge(frame(0), frame(1), find)
    assert check["status"] == "NEEDS_CLEANING" and check["trash"] == {"cup": 1} and seconds >= 0
    assert te.judge(frame(0), frame(2), find)[0]["status"] == "CLEAN"  # 자리만 옮김
    left = finder({0: [], 1: [det("cell phone")]})
    assert te.judge(frame(0), frame(1), left)[0]["status"] == "LEFT_ITEMS"


def test_judge_with_desk_photo_finds_unknown_things():
    """모델이 못 찾아도 책상 사진이 달라졌으면 모르는 물건 = 치울 것."""
    before = np.full((240, 320, 3), 120, dtype=np.uint8)
    after = before.copy()
    after[100:140, 140:180] = 250
    desk = desk_diff.DeskCompare((0.0, 0.0, 1.0, 1.0))
    check, _ = te.judge(before, after, lambda f: [], desk=desk, labeler=lambda f, b: None)
    assert check["status"] == "NEEDS_CLEANING" and check["trash"] == {"unknown": 1}


def test_score_counts_accuracy_serious_and_time():
    cases = [te.Case("001", "CLEAN"), te.Case("002", "NEEDS_CLEANING", "종이컵"), te.Case("003", "LEFT_ITEMS")]
    got = {"001": "CLEAN", "002": "CLEAN", "003": "NEEDS_CLEANING"}
    result = te.score(cases, lambda c: ({"status": got[c.name], "trash": {}, "leftItems": {}}, 0.4))
    assert result["right"] == 1 and result["serious"] == 1 and result["medianSeconds"] == 0.4
    assert result["byAnswer"]["NEEDS_CLEANING"] == [0, 1] and not result["passed"]
    lines = []
    te.report(result, "YOLO_MODEL=x", out=lines.append)
    text = "\n".join(lines)
    assert "정확도 33.3%" in text and "틀림 [심각] 002" in text and "종이컵" in text
    assert "| 정리 확인 | YOLO_MODEL=x | 33.3% | 1 |" in text and "틀림 002 003" in text


def test_score_passes_only_when_all_goals_met():
    cases = [te.Case(f"{i:03d}", "CLEAN") for i in range(20)]
    ok = te.score(cases, lambda c: ({"status": "CLEAN"}, 0.5))
    slow = te.score(cases, lambda c: ({"status": "CLEAN"}, 1.5))
    assert ok["passed"] and ok["accuracy"] == 1.0 and not slow["passed"]
    assert not te.score([], lambda c: None)["passed"]


def test_answers_file_round_trip(tmp_path):
    folder = str(tmp_path)
    for name in ("001", "002"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "before.jpg").write_bytes(b"x")
    (tmp_path / "001" / "after.jpg").write_bytes(b"x")
    te.append_answer(folder, te.Case("001", "CLEAN", "저녁"))
    te.append_answer(folder, te.Case("002", "LEFT_ITEMS"))
    with open(tmp_path / "answers.csv", "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(["003", "dirty", ""])
    cases, problems = te.read_answers(folder)
    assert [(c.name, c.answer, c.memo) for c in cases] == [("001", "CLEAN", "저녁")]
    assert any("002" in p and "after.jpg" in p for p in problems) and any("003" in p for p in problems)
    assert te.next_case_name(folder) == "003"
    assert te.read_answers(str(tmp_path / "없음"))[1]


def test_capture_skips_frames_with_people(tmp_path):
    frames = iter([frame(1)] * 5 + [frame(0)] * 5 + [frame(2)] * 5)

    class Cam:
        def read(self):
            return True, next(frames)

    answers = iter(["", "", "", "2", "종이컵 1"])
    saved, said = {}, []
    case = te.capture_one(str(tmp_path), Cam(), lambda f: 1 if f[0, 0, 0] == 1 else 0,
                          ask=lambda p: next(answers), out=said.append,
                          save=lambda p, f: saved.update({p.rsplit("/", 1)[1]: int(f[0, 0, 0])}))
    assert case.answer == "NEEDS_CLEANING" and case.memo == "종이컵 1"
    assert saved == {"before.jpg": 0, "after.jpg": 2}  # 사람이 보인 사진은 저장 안 함
    assert any("사람이 1명" in s for s in said)
    with open(tmp_path / "answers.csv", encoding="utf-8") as f:
        assert "001,NEEDS_CLEANING,종이컵 1" in f.read()
    assert te.capture_one(str(tmp_path), Cam(), lambda f: 0, ask=lambda p: "q", out=said.append) is None


def test_save_result_and_candidate(tmp_path):
    env = {"YOLO_MODEL": "gateway/yolov8s.onnx", "DESK_PHOTO": "1", "PORT": "8080"}
    assert te.candidate_name(env) == "YOLO_MODEL=gateway/yolov8s.onnx DESK_PHOTO=1"
    assert te.candidate_name({}) == "(설정 없음)"
    result = te.score([te.Case("001", "CLEAN")], lambda c: ({"status": "LEFT_ITEMS"}, 0.1))
    te.save_result(str(tmp_path), result, "후보")
    record = json.loads((tmp_path / "results.jsonl").read_text(encoding="utf-8"))
    assert record["candidate"] == "후보" and record["wrong"] == ["001"] and "rows" not in record


def test_main_needs_a_command():
    with pytest.raises(SystemExit):
        te.main([], {})


def test_run_score_end_to_end(tmp_path, monkeypatch):
    """사진 파일을 읽어 채점하고 기록을 남긴다 (모델만 가짜)."""
    import cv2
    import room_camera
    for name, after in (("001", 0), ("002", 200)):
        (tmp_path / name).mkdir()
        cv2.imwrite(str(tmp_path / name / "before.jpg"), frame(0))
        cv2.imwrite(str(tmp_path / name / "after.jpg"), frame(after))
    te.append_answer(str(tmp_path), te.Case("001", "CLEAN"))
    te.append_answer(str(tmp_path), te.Case("002", "NEEDS_CLEANING"))

    class Fake:
        finds_objects = True

        def detect(self, f):
            return [det("cup")] if f[0, 0, 0] > 100 else []

    monkeypatch.setattr(room_camera, "make_detector", lambda model, area: Fake())
    lines = []
    result = te.run_score(str(tmp_path), {"YOLO_MODEL": "m.onnx"}, out=lines.append)
    assert result["right"] == 2 and result["passed"] is True
    assert (tmp_path / "results.jsonl").exists() and any("통과" == line for line in lines)
    with pytest.raises(SystemExit):
        te.run_score(str(tmp_path), {}, out=lines.append)
