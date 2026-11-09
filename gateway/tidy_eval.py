"""정리 확인 채점: 정답을 적은 시험 사진 쌍으로 지금 설정(후보)이 몇 % 맞히는지 잰다.

멘토 의견(11/9): 기준을 정해 후보를 비교하고, 정확도 95% 이상·심각한 오류 0을 넘는 것을 고른다.
같은 문제집으로 모델·비교 방식·민감도를 바꿔 가며 채점하고, 결과를 README "실험 기록"에 남긴다.

문제집 (젯슨 안에만 둔다. git에도 서버에도 올리지 않는다)
    eval/tidy/
      answers.csv            case,answer,memo   (answer: CLEAN / NEEDS_CLEANING / LEFT_ITEMS)
      001/before.jpg         입실 전 빈 책상 (깨끗한 상태)
      001/after.jpg          퇴실 뒤 책상
      results.jsonl          채점할 때마다 한 줄씩 쌓인다

찍기 (사람이 보이면 저장하지 않는다):
    ROOM_CAMERA=2 YOLO_MODEL=gateway/yolov8s.onnx ~/mspace-venv/bin/python gateway/tidy_eval.py capture
채점 (게이트웨이와 같은 설정 이름을 쓴다: YOLO_MODEL, DESK_AREA, DESK_PHOTO, LABEL_MODEL, ROOM_ITEMS):
    YOLO_MODEL=gateway/yolov8s.onnx DESK_PHOTO=1 DESK_AREA=... ~/mspace-venv/bin/python gateway/tidy_eval.py score
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

ANSWERS = ("CLEAN", "NEEDS_CLEANING", "LEFT_ITEMS")
KOREAN = {"CLEAN": "깨끗함", "NEEDS_CLEANING": "치울 것", "LEFT_ITEMS": "두고 간 물건"}
CHOICES = {"1": "CLEAN", "2": "NEEDS_CLEANING", "3": "LEFT_ITEMS"}
GOAL_ACCURACY = 0.95
GOAL_SECONDS = 1.0
DEFAULT_DIR = "eval/tidy"
KST = timezone(timedelta(hours=9))


def is_serious(answer, got):
    """사람에게 실제 피해가 가는 틀림.

    깨끗한 방을 치울 것·두고 감으로 보면 죄 없는 예약자가 신고된다.
    치울 것·두고 감이 있는데 깨끗함으로 보면 다음 팀이 더러운 방을 받거나 물건을 잃는다.
    (치울 것 ↔ 두고 감 끼리 바뀐 것은 틀림이지만 심각하지는 않다: 어느 쪽이든 관리자가 본다)
    """
    return answer != got and "CLEAN" in (answer, got)


# ---------- 문제집 ----------

@dataclass
class Case:
    name: str
    answer: str
    memo: str = ""


def read_answers(folder):
    """answers.csv를 읽는다. 정답이 이상하거나 사진이 없는 줄은 빼고 이유를 알려 준다."""
    path = os.path.join(folder, "answers.csv")
    cases, problems = [], []
    if not os.path.exists(path):
        return cases, [f"{path}가 없어요. 먼저 capture로 찍어요"]
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("case") or "").strip()
            answer = (row.get("answer") or "").strip().upper()
            if not name:
                continue
            if answer not in ANSWERS:
                problems.append(f"{name}: 정답 {answer!r}는 {'/'.join(ANSWERS)} 중 하나여야 해요")
                continue
            missing = [p for p in ("before.jpg", "after.jpg") if not os.path.exists(os.path.join(folder, name, p))]
            if missing:
                problems.append(f"{name}: 사진이 없어요 ({', '.join(missing)})")
                continue
            cases.append(Case(name, answer, (row.get("memo") or "").strip()))
    return cases, problems


def append_answer(folder, case):
    path = os.path.join(folder, "answers.csv")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["case", "answer", "memo"])
        w.writerow([case.name, case.answer, case.memo])


def next_case_name(folder):
    used = [int(n) for n in os.listdir(folder) if n.isdigit()] if os.path.isdir(folder) else []
    return f"{max(used, default=0) + 1:03d}"


# ---------- 채점 ----------

def judge(before, after, find, room_items=None, desk=None, labeler=None, clock=time.perf_counter):
    """사진 한 쌍을 퇴실 모드와 같은 방법으로 판단한다. (결과 dict, 퇴실 사진 처리 초)를 돌려준다.

    find(frame): 정리 확인 때 쓰는 "자세히 찾기" (detect_detailed). before = 그 예약의 기준(입실 직전 빈 방).
    """
    import room_camera
    watcher = room_camera.RoomWatcher(0, room_items=room_items, labeler=labeler)
    seen = find(before)
    base = watcher.count(seen)
    if desk is not None:
        desk.set_base(before, watcher.objects(seen))
    started = clock()
    found = find(after)
    check = watcher.check(found, "", photo=(lambda: after) if desk is not None else None, base=base, desk=desk)
    seconds = clock() - started
    check["people"] = watcher.people(found)
    return check, seconds


def score(cases, run_case):
    """run_case(case) → (결과 dict, 초). 정확도·심각한 오류·지연과 틀린 문제를 모은다."""
    rows = []
    for case in cases:
        check, seconds = run_case(case)
        got = check["status"]
        rows.append({"case": case.name, "answer": case.answer, "got": got, "seconds": round(seconds, 3),
                     "ok": got == case.answer, "serious": is_serious(case.answer, got),
                     "trash": check.get("trash", {}), "leftItems": check.get("leftItems", {}),
                     "people": check.get("people", 0), "memo": case.memo})
    n = len(rows)
    right = sum(r["ok"] for r in rows)
    serious = sum(r["serious"] for r in rows)
    median = statistics.median(r["seconds"] for r in rows) if rows else 0.0
    by_answer = {a: [sum(r["ok"] for r in rows if r["answer"] == a), sum(r["answer"] == a for r in rows)]
                 for a in ANSWERS}
    return {"cases": n, "right": right, "accuracy": right / n if n else 0.0, "serious": serious,
            "medianSeconds": round(median, 3), "byAnswer": by_answer, "rows": rows,
            "passed": bool(n) and right / n >= GOAL_ACCURACY and serious == 0 and median <= GOAL_SECONDS}


def candidate_name(env):
    """채점한 설정을 한 줄로 (README 기록용). 게이트웨이 설정 이름 그대로."""
    keys = ("YOLO_MODEL", "DESK_PHOTO", "DESK_AREA", "LABEL_MODEL", "ROOM_ITEMS")
    parts = [f"{k}={env[k]}" for k in keys if env.get(k)]
    return " ".join(parts) or "(설정 없음)"


def report(result, candidate, out=print):
    out(f"후보: {candidate}")
    out(f"문제 {result['cases']}개 중 {result['right']}개 맞음: 정확도 {result['accuracy']:.1%} "
        f"(목표 {GOAL_ACCURACY:.0%}), 심각한 오류 {result['serious']}개 (목표 0), "
        f"사진당 {result['medianSeconds']:.2f}초 (목표 {GOAL_SECONDS:.0f}초 안)")
    for a in ANSWERS:
        ok, total = result["byAnswer"][a]
        if total:
            out(f"  정답 {KOREAN[a]}: {total}개 중 {ok}개 맞음")
    wrong = [r for r in result["rows"] if not r["ok"]]
    for r in wrong:
        found = {**r["trash"], **r["leftItems"]}
        seen = ", ".join(f"{k} {v}" for k, v in sorted(found.items())) or "찾은 것 없음"
        mark = " [심각]" if r["serious"] else ""
        person = f" (사람 {r['people']}명 보임: 다시 찍어요)" if r["people"] else ""
        out(f"  틀림{mark} {r['case']}: 정답 {KOREAN[r['answer']]} → {KOREAN[r['got']]} ({seen}){person}"
            f"{' - ' + r['memo'] if r['memo'] else ''}")
    out("통과" if result["passed"] else "아직 기준 못 넘음")
    names = " ".join(r["case"] for r in wrong) or "없음"
    out("README 실험 기록에 붙일 줄:")
    out(f"| {datetime.now(KST):%m/%d} | 정리 확인 | {candidate} | {result['accuracy']:.1%} | {result['serious']} "
        f"| {result['medianSeconds']:.2f}초 | (바꾼 것) | 문제 {result['cases']}개, 틀림 {names} |")


def save_result(folder, result, candidate):
    record = {"at": datetime.now(KST).isoformat(timespec="seconds"), "candidate": candidate,
              **{k: v for k, v in result.items() if k != "rows"},
              "wrong": [r["case"] for r in result["rows"] if not r["ok"]]}
    with open(os.path.join(folder, "results.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_score(folder, env, out=print):
    import cv2
    import desk_diff
    import room_camera

    import gateway
    cases, problems = read_answers(folder)
    for p in problems:
        out("건너뜀 " + p)
    if not cases:
        raise SystemExit("채점할 문제가 없어요")
    config = gateway.load_config(env)
    if not config.yolo_model:
        raise SystemExit("YOLO_MODEL을 넣어요. 예: YOLO_MODEL=gateway/yolov8s.onnx")
    detector = room_camera.make_detector(config.yolo_model, config.desk_area)
    find = getattr(detector, "detect_detailed", detector.detect)
    use_desk = config.desk_photo and config.desk_area
    if config.desk_photo and not config.desk_area:
        out("DESK_PHOTO=1은 DESK_AREA가 있어야 해요. 책상 사진 비교 없이 채점해요")
    labeler = room_camera.make_labeler(detector, config.label_model) if use_desk else None

    def run_case(case):
        before = cv2.imread(os.path.join(folder, case.name, "before.jpg"))
        after = cv2.imread(os.path.join(folder, case.name, "after.jpg"))
        desk = desk_diff.DeskCompare(config.desk_area) if use_desk else None  # 메모리에만 (파일 안 씀)
        return judge(before, after, find, config.room_items, desk, labeler)

    if cases:
        run_case(cases[0])  # 처음 한 번은 모델 준비로 느리다. 시간에 넣지 않게 한 번 돌려 둔다
    result = score(cases, run_case)
    candidate = candidate_name(env)
    report(result, candidate, out)
    save_result(folder, result, candidate)
    return result


# ---------- 찍기 ----------

def grab(cam, flush=5):
    """버퍼에 쌓인 옛 화면을 버리고 지금 화면 한 장."""
    frame = None
    for _ in range(flush):
        ok, frame = cam.read()
        if not ok:
            frame = None
    return frame


def capture_one(folder, cam, count_people, ask=input, out=print, save=None):
    """한 문제(빈 책상 + 퇴실 뒤 + 정답)를 찍는다. 그만하면 None."""
    import cv2
    save = save or cv2.imwrite
    name = next_case_name(folder)
    shots = {}
    for key, prompt in (("before", "책상을 깨끗하게(입실 전처럼) 두고 화면 밖으로 나와서 Enter"),
                        ("after", "쓰레기나 물건을 놓거나(또는 그대로 두고) 화면 밖으로 나와서 Enter")):
        while True:
            if ask(f"[{name}] {prompt} (q: 그만) ").strip().lower() == "q":
                return None
            frame = grab(cam)
            if frame is None:
                out("카메라 사진을 못 읽었어요. 다시 Enter")
                continue
            people = count_people(frame)
            if people:
                out(f"사람이 {people}명 보여서 저장하지 않았어요. 화면 밖으로 나와서 다시 Enter")
                continue
            shots[key] = frame
            break
    while True:
        choice = ask("정답? 1 깨끗함 / 2 치울 것 / 3 두고 간 물건 ").strip()
        if choice in CHOICES:
            break
    memo = ask("메모 (예: 종이컵 1, 창가 쪽, 저녁 조명. 없으면 Enter) ").strip()
    os.makedirs(os.path.join(folder, name), exist_ok=True)
    for key, frame in shots.items():
        save(os.path.join(folder, name, f"{key}.jpg"), frame)
    case = Case(name, CHOICES[choice], memo)
    append_answer(folder, case)
    out(f"저장: {folder}/{name} 정답 {KOREAN[case.answer]}")
    return case


def run_capture(folder, env):
    import room_camera

    import gateway
    config = gateway.load_config(env)
    detector = room_camera.make_detector(config.yolo_model, None)
    cam = room_camera.open_camera(config.room_camera or "0")
    os.makedirs(folder, exist_ok=True)
    print(f"시험 사진 찍기: {folder} (젯슨 안에만 저장, 사람이 보이면 저장 안 함)")
    print("깨끗한 문제도 꼭 섞어 주세요 (잘못 신고 검사용). 쓰레기 종류·자리·조명을 바꿔 가며 30~50개")
    try:
        while capture_one(folder, cam, lambda f: sum(1 for d in detector.detect(f) if d.label == "person")):
            pass
    finally:
        cam.release()


def main(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    if not argv or argv[0] not in ("capture", "score"):
        raise SystemExit("사용법: tidy_eval.py capture|score [문제집 폴더, 기본 eval/tidy]")
    folder = argv[1] if len(argv) > 1 else DEFAULT_DIR
    if argv[0] == "capture":
        run_capture(folder, env)
    else:
        run_score(folder, env)


if __name__ == "__main__":
    main()
