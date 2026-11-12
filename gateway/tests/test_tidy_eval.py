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
    # 책상 비교만 찾았고, 3시간 뒤처럼 YOLO 개수만 보면 깨끗함
    assert check["evidence"] == {"yolo": False, "desk": True, "count": False}


def test_judge_records_what_found_it_for_the_presence_table():
    find = finder({0: [det("remote")], 1: [det("remote"), det("cup", 60)]})
    check, _ = te.judge(frame(0), frame(1), find)
    assert check["evidence"] == {"yolo": True, "desk": False, "count": True}
    row = te.evidence_row("NEEDS_CLEANING", {**check, "vlm": False})
    assert row == {"left": True, "found": True, "key": "yolo+vlm-no", "count": True, "target": True}
    # VLM이 "없음"이라 CLEAN으로 바뀌어도 VLM 전 결과가 남음이면 3시간 뒤 다시 확인 대상
    assert te.evidence_row("CLEAN", {**check, "status": "CLEAN", "yoloStatus": "NEEDS_CLEANING"})["target"] is True
    assert te.evidence_row("CLEAN", {**check, "status": "CLEAN", "yoloStatus": "CLEAN"})["target"] is False


def test_score_counts_accuracy_serious_and_time():
    cases = [te.Case("001", "CLEAN"), te.Case("002", "NEEDS_CLEANING", "종이컵"), te.Case("003", "LEFT_ITEMS")]
    got = {"001": "CLEAN", "002": "CLEAN", "003": "NEEDS_CLEANING"}
    result = te.score(cases, lambda c: ({"status": got[c.name], "trash": {}, "leftItems": {}}, 0.4))
    assert result["right"] == 1 and result["serious"] == 1 and result["medianSeconds"] == 0.4
    assert result["byAnswer"]["NEEDS_CLEANING"] == [0, 1] and not result["passed"]
    lines = []
    te.report(result, "YOLO_MODEL=x", out=lines.append)
    text = "\n".join(lines)
    assert "정확도 (깨끗함/남음) 66.7%" in text and "세 가지까지 1개 33.3%" in text
    assert "틀림 [심각] 002" in text and "틀림 [종류만] 003" in text and "종이컵" in text
    assert "| 정리 확인 | YOLO_MODEL=x | 66.7% (세 가지 33.3%) | 1 |" in text and "틀림 002 003" in text


def test_left_accuracy_ignores_trash_vs_left_items():
    """쓰레기 ↔ 두고 감만 바뀐 것은 깨끗함/남음 정확도에서 맞음. 합격도 이 점수로."""
    cases = [te.Case(f"{i:03d}", "LEFT_ITEMS") for i in range(20)]
    result = te.score(cases, lambda c: ({"status": "NEEDS_CLEANING"}, 0.5))
    assert result["accuracy"] == 0.0 and result["leftAccuracy"] == 1.0 and result["leftRight"] == 20
    assert result["serious"] == 0 and result["passed"]
    assert te.verdict(result) == "후보"


def test_rank_uses_left_accuracy_then_three_way():
    rows = [{"name": "세 가지만 높음", "verdict": "탈락 (정확도)", "result": {**result_of(0.60), "leftAccuracy": 0.70}},
            {"name": "남음 높음", "verdict": "탈락 (정확도)", "result": {**result_of(0.50), "leftAccuracy": 0.80}},
            {"name": "남음 같고 세 가지 높음", "verdict": "탈락 (정확도)", "result": {**result_of(0.55), "leftAccuracy": 0.80}}]
    ranked = te.rank(rows)
    assert [r["name"] for r in ranked] == ["남음 같고 세 가지 높음", "남음 높음", "세 가지만 높음"]
    table = te.compare_table([{**r, "license": "-"} for r in ranked])
    assert "| 1 | 채택 (기준 미달: 정확도) | 남음 같고 세 가지 높음 | **80.0%** | 55.0% (100% / 95% / -) |" in table


def test_score_passes_only_when_all_goals_met():
    cases = [te.Case(f"{i:03d}", "CLEAN") for i in range(20)]
    ok = te.score(cases, lambda c: ({"status": "CLEAN"}, 0.5))
    slow = te.score(cases, lambda c: ({"status": "CLEAN"}, 120.5))
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
    assert record["evidence"] == [{"left": False, "found": True, "key": "none", "count": False, "target": True}]  # 이름·사진은 안 남김


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

    monkeypatch.setattr(room_camera, "make_detector", lambda model, area, floor=None: Fake())
    lines = []
    result = te.run_score(str(tmp_path), {"YOLO_MODEL": "m.onnx"}, out=lines.append)
    assert result["right"] == 2 and result["passed"] is True
    assert (tmp_path / "results.jsonl").exists() and any("통과" == line for line in lines)
    assert result["loadSeconds"] >= 0 and "maxSeconds" in result
    lines.clear()
    te.run_score(str(tmp_path), {"YOLO_MODEL": "m.onnx"}, out=lines.append, as_json=True)
    mark = [line for line in lines if line.startswith(te.RESULT_MARK)]
    assert len(mark) == 1 and json.loads(mark[0][len(te.RESULT_MARK):])["right"] == 2
    with pytest.raises(SystemExit):
        te.run_score(str(tmp_path), {}, out=lines.append)


def test_run_score_stops_on_broken_photo(tmp_path, monkeypatch):
    import room_camera
    (tmp_path / "001").mkdir()
    (tmp_path / "001" / "before.jpg").write_bytes(b"not a jpg")
    (tmp_path / "001" / "after.jpg").write_bytes(b"not a jpg")
    te.append_answer(str(tmp_path), te.Case("001", "CLEAN"))

    class Fake:
        finds_objects = True

        def detect(self, f):
            return []

    monkeypatch.setattr(room_camera, "make_detector", lambda model, area, floor=None: Fake())
    with pytest.raises(SystemExit, match="001/before.jpg"):
        te.run_score(str(tmp_path), {"YOLO_MODEL": "m.onnx"}, out=lambda s: None)


def result_of(accuracy, serious=0, median=0.5, by=None):
    return {"cases": 40, "accuracy": accuracy, "serious": serious, "medianSeconds": median, "maxSeconds": median * 2,
            "loadSeconds": 3.2, "memoryMB": 812.4,
            "byAnswer": by or {"CLEAN": [10, 10], "NEEDS_CLEANING": [19, 20], "LEFT_ITEMS": [0, 0]}}


def test_read_candidates(tmp_path):
    (tmp_path / "candidates.txt").write_text(
        "# 주석\nv8s 지금 | YOLO_MODEL=gateway/yolov8s.onnx\n\nv8s+책상 | YOLO_MODEL=a.onnx DESK_PHOTO=1\n이상한 줄\n",
        encoding="utf-8")
    found = te.read_candidates(str(tmp_path))
    assert [(c.name, c.env) for c in found] == [("v8s 지금", {"YOLO_MODEL": "gateway/yolov8s.onnx"}),
                                                ("v8s+책상", {"YOLO_MODEL": "a.onnx", "DESK_PHOTO": "1"})]
    with pytest.raises(SystemExit):
        te.read_candidates(str(tmp_path / "없음"))


def test_license_and_verdict():
    assert te.license_of({"YOLO_MODEL": "gateway/yolov8s.onnx"}) == "AGPL-3.0 (Ultralytics)"
    assert te.license_of({"YOLO_MODEL": "rtdetr-l.onnx"}) == "Apache-2.0"
    assert te.license_of({"YOLO_MODEL": "my.onnx"}) == "확인 필요"
    assert te.verdict(result_of(0.97)) == "후보"
    assert te.verdict(result_of(0.90, serious=2, median=130.0)) == "탈락 (정확도, 심각 오류, 시간)"
    assert te.verdict({"error": "x"}) == "실패"


def test_rank_picks_best_passing_candidate():
    rows = [{"name": "빠름", "verdict": "후보", "result": result_of(0.96, median=0.3)},
            {"name": "정확", "verdict": "후보", "result": result_of(0.98, median=0.8)},
            {"name": "더 정확하지만 심각 오류", "verdict": "탈락 (심각 오류)", "result": result_of(0.99, serious=1)},
            {"name": "고장", "verdict": "실패", "result": {"error": "모델 없음"}}]
    ranked = te.rank(rows)
    assert [r["name"] for r in ranked] == ["정확", "빠름", "더 정확하지만 심각 오류", "고장"]
    assert ranked[0]["verdict"] == "채택" and ranked[1]["verdict"] == "후보"
    assert te.rank([{"name": "x", "verdict": "탈락 (정확도)", "result": result_of(0.5)}])[0]["verdict"] == "채택 (기준 미달: 정확도)"


def test_rank_marks_best_even_when_nobody_passes():
    rows = [{"name": "낮음", "verdict": "탈락 (정확도)", "result": result_of(0.70)},
            {"name": "높음", "verdict": "탈락 (정확도)", "result": result_of(0.85)},
            {"name": "고장", "verdict": "실패", "result": {"error": "모델 없음"}}]
    ranked = te.rank(rows)
    assert [r["verdict"] for r in ranked] == ["채택 (기준 미달: 정확도)", "탈락 (정확도)", "실패"]
    assert te.rank([{"name": "고장", "verdict": "실패", "result": {"error": "x"}}])[0]["verdict"] == "실패"


def test_score_splits_serious_into_false_alarm_and_missed():
    cases = [te.Case("001", "CLEAN", ""), te.Case("002", "LEFT_ITEMS", ""), te.Case("003", "NEEDS_CLEANING", "")]
    got = {"001": "LEFT_ITEMS", "002": "CLEAN", "003": "LEFT_ITEMS"}
    result = te.score(cases, lambda c: ({"status": got[c.name]}, 0.1))
    assert (result["serious"], result["falseAlarm"], result["missed"]) == (2, 1, 1)
    row = {"name": "v8s", "result": {**result, "loadSeconds": 1.0, "memoryMB": None}, "verdict": "탈락", "license": "-"}
    assert "| 2 (1 / 1) |" in te.compare_table([row])


def test_compare_writes_table(tmp_path):
    (tmp_path / "candidates.txt").write_text("A | YOLO_MODEL=yolov8n.onnx\nB | YOLO_MODEL=yolo11s.onnx\n"
                                             "C | YOLO_MODEL=없는.onnx\n", encoding="utf-8")
    results = {"A": result_of(0.92), "B": result_of(0.975), "C": {"error": "모델 파일이 없어요"}}
    lines = []
    rows = te.run_compare(str(tmp_path), {}, out=lines.append, run_candidate=lambda c: results[c.name])
    assert [r["name"] for r in rows] == ["B", "A", "C"]
    text = "\n".join(lines)
    assert "**채택:** B · 정확도 (깨끗함/남음) 97.5% · 세 가지 97.5% · 심각 오류 0 · 사진당 0.50초 · 메모리 812MB" in text
    assert "| 1 | 채택 | B | **97.5%** | 97.5% (100% / 95% / -) | 0 | 0.50초 (1.00초) | 812MB | 3.2초 | AGPL-3.0" in text
    assert "| 2 | 탈락 (정확도) | A |" in text and "| 3 | 실패 | C | 모델 파일이 없어요 |" in text
    saved = list(tmp_path.glob("compare_*.md"))
    assert len(saved) == 1 and "문제 40개" in saved[0].read_text(encoding="utf-8")


def test_run_one_reads_result_line_from_child():
    calls = []

    class Done:
        def __init__(self, stdout, stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    def run(cmd, env, **kw):
        calls.append((cmd[-3:], env))
        return Done("채점 중\n" + te.RESULT_MARK + json.dumps({"accuracy": 1.0}) + "\n")

    cand = te.Candidate("A", {"YOLO_MODEL": "a.onnx"})
    assert te.run_one("eval/tidy", cand, {"PATH": "/bin"}, run=run) == {"accuracy": 1.0}
    assert calls[0][0] == ["score", "eval/tidy", "--json"] and calls[0][1] == {"PATH": "/bin", "YOLO_MODEL": "a.onnx"}
    broken = te.run_one("eval/tidy", cand, {}, run=lambda cmd, env, **kw: Done("", "Traceback\nFileNotFoundError: a", 1))
    assert broken == {"error": "FileNotFoundError: a"}
    # 결과 줄을 낸 뒤 끝에서 실패하면 그 결과를 믿지 않는다
    late = Done(te.RESULT_MARK + "{}\n", "Traceback\nOSError: 디스크", 1)
    assert te.run_one("eval/tidy", cand, {}, run=lambda cmd, env, **kw: late) == {"error": "OSError: 디스크"}

    def slow(cmd, env, timeout, **kw):
        import subprocess
        raise subprocess.TimeoutExpired(cmd, timeout)

    assert "60초 안에 안 끝남" in te.run_one("eval/tidy", cand, {"TIDY_TIMEOUT": "60"}, run=slow)["error"]


def test_capture_stops_when_photo_not_saved(tmp_path):
    class Cam:
        def read(self):
            return True, frame(0)

    answers = iter(["", "", "1", ""])
    with pytest.raises(SystemExit, match="저장하지 못했어요"):
        te.capture_one(str(tmp_path), Cam(), lambda f: 0, ask=lambda p: next(answers), out=lambda s: None,
                       save=lambda p, f: False)
    assert not (tmp_path / "answers.csv").exists()


def test_run_one_uses_default_timeout_for_bad_value(tmp_path):
    seen = []

    class Done:
        stdout, stderr, returncode = te.RESULT_MARK + '{"accuracy": 1.0}', "", 0

    def run(cmd, env, timeout, **kw):
        seen.append(timeout)
        return Done()

    for bad in ("abc", "0", "-5", "inf"):
        te.run_one(str(tmp_path / "없음"), te.Candidate("a", {"TIDY_TIMEOUT": bad}), {}, run=run)
    te.run_one(str(tmp_path / "없음"), te.Candidate("a", {"TIDY_TIMEOUT": "60"}), {}, run=run)
    assert seen == [1800.0, 1800.0, 1800.0, 1800.0, 60.0]  # 문제집이 없으면 최소 30분


def test_skipped_cases_show_in_result_and_table(tmp_path, monkeypatch):
    """건너뛴 문제는 결과와 비교표에 남는다. 모델 올리기는 첫 장을 볼 때까지만 잰다."""
    import cv2
    import room_camera
    (tmp_path / "001").mkdir()
    cv2.imwrite(str(tmp_path / "001" / "before.jpg"), frame(0))
    cv2.imwrite(str(tmp_path / "001" / "after.jpg"), frame(0))
    te.append_answer(str(tmp_path), te.Case("001", "CLEAN"))
    te.append_answer(str(tmp_path), te.Case("002", "CLEAN"))  # 사진 없음 → 건너뜀
    ticks = iter(range(100))

    class Fake:
        finds_objects = True

        def detect(self, f):
            return []

    monkeypatch.setattr(room_camera, "make_detector", lambda model, area, floor=None: Fake())
    result = te.run_score(str(tmp_path), {"YOLO_MODEL": "m.onnx"}, out=lambda s: None,
                          clock=lambda: next(ticks))
    assert len(result["skipped"]) == 1 and result["cases"] == 1
    assert result["loadSeconds"] == 1  # 시작 0 → 첫 장 1 (둘째 장까지 기다리지 않음)
    row = {"name": "v8s", "result": result, "verdict": "후보", "license": "AGPL-3.0 (Ultralytics)"}
    assert "v8s (1문제 건너뜀)" in te.compare_table([row])


def test_chosen_line_when_all_failed():
    assert te.chosen_line([{"name": "x", "verdict": "실패", "result": {"error": "e"}}]) == "**채택:** 없음 (모든 후보 실패)"


def test_default_timeout_grows_with_the_number_of_pairs(tmp_path):
    """사진당 2분까지 허용하니, 문제마다 두 장 × 2분 + 10분 여유 안에서는 실패로 끊지 않는다."""
    rows = ["case,answer,memo"]
    for i in range(1, 48):
        name = f"{i:03d}"
        (tmp_path / name).mkdir()
        for p in ("before.jpg", "after.jpg"):
            (tmp_path / name / p).write_bytes(b"x")
        rows.append(f"{name},CLEAN,")
    (tmp_path / "answers.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert te.default_timeout(str(tmp_path)) == 47 * 2 * te.GOAL_SECONDS + 600
    assert te.default_timeout(str(tmp_path / "없음")) == te.TIMEOUT_MIN_SEC
