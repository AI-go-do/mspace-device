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
여러 후보 한 번에 비교 (eval/tidy/candidates.txt에 한 줄에 하나: `이름 | YOLO_MODEL=... DESK_PHOTO=1`):
    ~/mspace-venv/bin/python gateway/tidy_eval.py compare
    → 후보마다 따로 띄워 채점하고 정확도·심각 오류·시간(최대)·메모리·모델 올리기·라이선스·판단 표를 만든다
"""
from __future__ import annotations

import csv
import json
import math
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
RESULT_MARK = "TIDY_RESULT "
# 모델 파일 이름 → 라이선스 (포트폴리오·배포 때 확인용). 모르는 모델은 "확인 필요"
LICENSES = (("world", "AGPL-3.0 (Ultralytics)"), ("yolov8", "AGPL-3.0 (Ultralytics)"),
            ("yolo11", "AGPL-3.0 (Ultralytics)"), ("rtdetr", "Apache-2.0"), ("yolox", "Apache-2.0"))
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
    watcher = room_camera.RoomWatcher(0, check_objects=True, room_items=room_items, labeler=labeler)
    seen = find(before)
    base = watcher.count(seen)
    if desk is not None:
        desk.set_base(before, watcher.objects(seen))
    started = clock()
    found = find(after)
    check = watcher.check(found, "", photo=(lambda: after) if desk is not None else None, base=base, desk=desk)
    seconds = clock() - started
    if check is None:
        raise RuntimeError("정리 확인 결과가 비어 있어요 (RoomWatcher.check)")
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
    false_alarm = sum(r["serious"] and r["answer"] == "CLEAN" for r in rows)  # 깨끗한데 신고
    median = statistics.median(r["seconds"] for r in rows) if rows else 0.0
    slowest = max((r["seconds"] for r in rows), default=0.0)
    by_answer = {a: [sum(r["ok"] for r in rows if r["answer"] == a), sum(r["answer"] == a for r in rows)]
                 for a in ANSWERS}
    return {"cases": n, "right": right, "accuracy": right / n if n else 0.0, "serious": serious,
            "falseAlarm": false_alarm, "missed": serious - false_alarm,
            "medianSeconds": round(median, 3), "maxSeconds": round(slowest, 3), "byAnswer": by_answer, "rows": rows,
            "passed": bool(n) and right / n >= GOAL_ACCURACY and serious == 0 and median <= GOAL_SECONDS}


def candidate_name(env):
    """채점한 설정을 한 줄로 (README 기록용). 게이트웨이 설정 이름 그대로."""
    keys = ("YOLO_MODEL", "DESK_PHOTO", "DESK_AREA", "LABEL_MODEL", "ROOM_ITEMS")
    parts = [f"{k}={env[k]}" for k in keys if env.get(k)]
    return " ".join(parts) or "(설정 없음)"


def report(result, candidate, out=print):
    out(f"후보: {candidate}")
    out(f"문제 {result['cases']}개 중 {result['right']}개 맞음: 정확도 {result['accuracy']:.1%} "
        f"(목표 {GOAL_ACCURACY:.0%}), 심각한 오류 {result['serious']}개 (목표 0, 잘못 신고 {result['falseAlarm']} · 놓침 {result['missed']}), "
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
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "results.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def peak_memory_mb():
    """이 프로그램이 지금까지 쓴 메모리 최대 (MB). 젯슨은 GPU와 메모리를 같이 써서 모델 메모리도 여기 들어간다."""
    try:
        import resource
    except ImportError:  # 윈도우
        return None
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(kb / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)  # 맥은 바이트, 리눅스는 KB


def run_score(folder, env, out=print, as_json=False, clock=time.perf_counter):
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
    loading = clock()
    detector = room_camera.make_detector(config.yolo_model, config.desk_area)
    find = getattr(detector, "detect_detailed", detector.detect)
    use_desk = config.desk_photo and config.desk_area
    if config.desk_photo and not config.desk_area:
        out("DESK_PHOTO=1은 DESK_AREA가 있어야 해요. 책상 사진 비교 없이 채점해요")
    labeler = room_camera.make_labeler(detector, config.label_model) if use_desk else None
    first_seen = []

    def timed_find(img):  # 모델을 불러와 첫 장을 보기까지 걸린 시간을 잰다
        seen = find(img)
        if not first_seen:
            first_seen.append(clock())
        return seen

    def run_case(case):
        before = cv2.imread(os.path.join(folder, case.name, "before.jpg"))
        after = cv2.imread(os.path.join(folder, case.name, "after.jpg"))
        for key, img in (("before.jpg", before), ("after.jpg", after)):
            if img is None:
                raise SystemExit(f"{case.name}/{key}를 못 읽었어요 (파일이 깨졌을 수 있어요). 다시 찍거나 answers.csv에서 빼요")
        desk = desk_diff.DeskCompare(config.desk_area) if use_desk else None  # 메모리에만 (파일 안 씀)
        return judge(before, after, timed_find, config.room_items, desk, labeler)

    run_case(cases[0])  # 처음 한 번은 모델 준비로 느리다. 시간에 넣지 않고, 첫 장까지를 "모델 올리기"로 둔다
    load_seconds = first_seen[0] - loading
    result = score(cases, run_case)
    result["loadSeconds"] = round(load_seconds, 1)
    result["skipped"] = problems  # 건너뛴 문제. 비교표에 같이 적어 문제집이 다른 걸 알아보게 한다
    result["memoryMB"] = peak_memory_mb()
    candidate = candidate_name(env)
    report(result, candidate, out)
    save_result(folder, result, candidate)
    if as_json:  # compare가 읽는 줄
        summary = {k: v for k, v in result.items() if k != "rows"}
        summary["wrong"] = [r["case"] for r in result["rows"] if not r["ok"]]
        out(RESULT_MARK + json.dumps(summary, ensure_ascii=False))
    return result


# ---------- 여러 후보 한 번에 비교 ----------

@dataclass
class Candidate:
    name: str
    env: dict


def read_candidates(folder):
    """candidates.txt: 한 줄에 후보 하나. `이름 | YOLO_MODEL=... DESK_PHOTO=1`. #으로 시작하면 건너뜀."""
    path = os.path.join(folder, "candidates.txt")
    if not os.path.exists(path):
        raise SystemExit(f"{path}가 없어요. 예: v8s 지금 | YOLO_MODEL=gateway/yolov8s.onnx")
    found = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, settings = line.partition("|")
            pairs = [p.split("=", 1) for p in settings.split() if "=" in p]
            if name.strip() and pairs:
                found.append(Candidate(name.strip(), {k: v for k, v in pairs}))
            else:
                print(f"후보 줄 형식이 이상해서 건너뛰어요: {line!r}")
    return found


def license_of(env):
    models = " ".join(env.get(k, "") for k in ("YOLO_MODEL", "LABEL_MODEL")).lower()
    names = sorted({lic for key, lic in LICENSES if key in models})
    return ", ".join(names) or "확인 필요"


def run_one(folder, candidate, base_env, run=None):
    """후보 하나를 따로 띄운 프로그램에서 채점한다 (모델 올리기 시간·메모리를 후보마다 깨끗하게 재려고).

    TIDY_TIMEOUT(초, 기본 1800)이 지나도 안 끝나면 그 후보는 실패로 두고 다음 후보로 넘어간다.
    """
    import subprocess
    run = run or subprocess.run
    env = {**base_env, **candidate.env}
    try:
        timeout = float(env.get("TIDY_TIMEOUT") or 1800)
    except ValueError:
        timeout = 1800.0
    if not math.isfinite(timeout) or timeout <= 0:  # 0 이하·NaN·inf면 기본값
        timeout = 1800.0
    try:
        done = run([sys.executable, os.path.abspath(__file__), "score", folder, "--json"],
                   env=env, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"{timeout:.0f}초 안에 안 끝남 (TIDY_TIMEOUT으로 늘릴 수 있어요)"}
    if done.returncode == 0:
        for line in reversed(done.stdout.splitlines()):
            if line.startswith(RESULT_MARK):
                return json.loads(line[len(RESULT_MARK):])
    tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["알 수 없음"]
    return {"error": tail[0]}


def verdict(result):
    """채택(기준을 넘은 것 중 가장 좋음) / 후보(기준 넘음) / 탈락(이유)."""
    if "error" in result:
        return "실패"
    why = []
    if result["accuracy"] < GOAL_ACCURACY:
        why.append("정확도")
    if result["serious"]:
        why.append("심각 오류")
    if result["medianSeconds"] > GOAL_SECONDS:
        why.append("시간")
    return "후보" if not why else "탈락 (" + ", ".join(why) + ")"


def rank(rows):
    """기준을 넘은 것 → 정확도 높은 순 → 심각 오류 적은 순 → 빠른 순.

    맨 위를 채택으로 바꾼다. 아무도 기준을 못 넘었어도 맨 위를 "채택 (기준 미달: …)"로 둔다 (지금 쓸 것 + 다음 개선의 출발점).
    """
    def key(row):
        r = row["result"]
        if "error" in r:
            return (2, 0, 0, 0)
        return (0 if row["verdict"] == "후보" else 1, -r["accuracy"], r["serious"], r["medianSeconds"])
    rows = sorted(rows, key=key)
    if rows and rows[0]["verdict"] == "후보":
        rows[0]["verdict"] = "채택"
    elif rows and "error" not in rows[0]["result"]:
        rows[0]["verdict"] = "채택 (기준 미달: " + rows[0]["verdict"].removeprefix("탈락 (")
    return rows


def compare_table(rows):
    """비교표 (마크다운). README 실험 기록·포트폴리오에 그대로 붙인다."""
    head = ("| 판단 | 후보 | 정확도 (깨끗함 / 치울 것 / 두고 감) | 심각 오류 (잘못 신고 / 놓침) | 사진당 시간 (최대) "
            "| 메모리 최대 | 모델 올리기 | 라이선스 |")
    lines = [head, "|---|---|---|---|---|---|---|---|"]
    for row in rows:
        r = row["result"]
        if "error" in r:
            lines.append(f"| 실패 | {row['name']} | {r['error']} | | | | | {row['license']} |")
            continue
        parts = []
        for a in ANSWERS:
            ok, total = r["byAnswer"][a]
            parts.append(f"{ok / total:.0%}" if total else "-")
        memory = f"{r['memoryMB']:.0f}MB" if r.get("memoryMB") is not None else "-"
        name = row["name"] + (f" ({len(r['skipped'])}문제 건너뜀)" if r.get("skipped") else "")
        split = f" ({r['falseAlarm']} / {r['missed']})" if "falseAlarm" in r else ""
        lines.append(f"| {row['verdict']} | {name} | **{r['accuracy']:.1%}** ({' / '.join(parts)}) "
                     f"| {r['serious']}{split} | {r['medianSeconds']:.2f}초 ({r['maxSeconds']:.2f}초) | {memory} "
                     f"| {r['loadSeconds']:.1f}초 | {row['license']} |")
    return "\n".join(lines)


def run_compare(folder, env, out=print, run_candidate=None):
    candidates = read_candidates(folder)
    if not candidates:
        raise SystemExit("candidates.txt에 후보가 없어요")
    run_candidate = run_candidate or (lambda c: run_one(folder, c, dict(env)))
    rows = []
    for i, c in enumerate(candidates, 1):
        out(f"[{i}/{len(candidates)}] {c.name} 채점 중...")
        result = run_candidate(c)
        rows.append({"name": c.name, "result": result, "verdict": verdict(result), "license": license_of({**env, **c.env})})
    rows = rank(rows)
    cases = next((r["result"]["cases"] for r in rows if "error" not in r["result"]), 0)
    table = compare_table(rows)
    text = (f"### {datetime.now(KST):%Y-%m-%d %H:%M} · 정리 확인 후보 비교 (문제 {cases}개)\n\n"
            f"목표: 정확도 {GOAL_ACCURACY:.0%} 이상 · 심각 오류 0 · 사진당 {GOAL_SECONDS:.0f}초 안\n\n{table}\n")
    out(text)
    path = os.path.join(folder, f"compare_{datetime.now(KST):%m%d_%H%M}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    out(f"저장: {path} (README 9절 \"비교표 원본\" 맨 위에 붙여요. 최신이 위)")
    return rows


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
        path = os.path.join(folder, name, f"{key}.jpg")
        if save(path, frame) is False:  # 정답 줄을 쓰기 전에 멈춘다 (사진 없는 문제가 생기지 않게)
            raise SystemExit(f"사진을 저장하지 못했어요: {path} (폴더 권한·디스크 확인)")
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
    if not argv or argv[0] not in ("capture", "score", "compare"):
        raise SystemExit("사용법: tidy_eval.py capture|score|compare [문제집 폴더, 기본 eval/tidy]")
    args = [a for a in argv[1:] if not a.startswith("--")]
    folder = args[0] if args else DEFAULT_DIR
    if argv[0] == "capture":
        run_capture(folder, env)
    elif argv[0] == "compare":
        run_compare(folder, env)
    else:
        run_score(folder, env, as_json="--json" in argv)


if __name__ == "__main__":
    main()
