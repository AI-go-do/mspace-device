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
채점 (게이트웨이와 같은 설정 이름을 쓴다: YOLO_MODEL, DESK_AREA, FLOOR_AREA, DESK_PHOTO, DESK_LIGHT, DESK_THRESH, DESK_MIN_AREA, LABEL_MODEL, ROOM_ITEMS):
    YOLO_MODEL=gateway/yolov8s.onnx DESK_PHOTO=1 DESK_AREA=... ~/mspace-venv/bin/python gateway/tidy_eval.py score
여러 후보 한 번에 비교 (eval/tidy/candidates.txt에 한 줄에 하나: `이름 | YOLO_MODEL=... DESK_PHOTO=1`):
    ~/mspace-venv/bin/python gateway/tidy_eval.py compare
    → 후보마다 따로 띄워 채점하고 정확도·심각 오류·시간(최대)·메모리·모델 올리기·라이선스·판단 표를 만든다
점수를 core로 보내기 (관리자 화면 "모델 시험 정확도"): score가 끝나면 숫자만 POST /api/rooms/{방}/model-scores.
    SERVER·DEVICE_TOKEN·ROOM_CAMERA_ROOM은 게이트웨이와 같다. SERVER가 없거나 DRY_RUN=1이면 보낼 내용만 찍는다.
    마지막 결과 다시 보내기: tidy_eval.py push-last [문제집 폴더] (같은 eventId라 core는 한 번만 저장)
"""
from __future__ import annotations

import csv
import json
import math
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

ANSWERS = ("CLEAN", "NEEDS_CLEANING", "LEFT_ITEMS")
KOREAN = {"CLEAN": "깨끗함", "NEEDS_CLEANING": "치울 것", "LEFT_ITEMS": "두고 간 물건"}
CHOICES = {"1": "CLEAN", "2": "NEEDS_CLEANING", "3": "LEFT_ITEMS"}
GOAL_ACCURACY = 0.95
GOAL_SECONDS = 120.0  # 정리 확인은 퇴실 뒤 한 번만 봐서 2분까지 괜찮음. 시간보다 정확도 우선 (11/11 수연, 10초 → 2분)
DEFAULT_DIR = "eval/tidy"
TIMEOUT_MIN_SEC = 1800.0  # 후보 하나 채점 제한의 최소 (30분). 기본은 문제 수에 맞춰 늘린다 (default_timeout)
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


def left_ok(answer, got):
    """깨끗함 / 뭔가 남음만 맞으면 정답. 사용자에게는 쓰레기와 두고 간 물건을 구별하지 않고 알려서 (11/11 수연)
    이 점수로 순위와 합격을 정한다. 세 가지까지 맞힌 정확도는 참고로 함께 적는다."""
    return (answer == "CLEAN") == (got == "CLEAN")


def main_accuracy(result):
    """순위·합격에 쓰는 정확도 (깨끗함/남음). 이 점수가 없던 옛 결과는 세 가지 정확도로 본다."""
    return result.get("leftAccuracy", result["accuracy"])


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

def judge(before, after, find, room_items=None, desk=None, labeler=None, clock=time.perf_counter, vlm=None):
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
    # 3시간 뒤 다시 확인(RECHECK)처럼 YOLO 개수만 봤으면 남음이었나 (물건이 있을 확률 표의 "recheck" 칸)
    check["evidence"]["count"] = watcher.check(found, "", base=base)["status"] != "CLEAN"
    if desk is not None:  # 채점 보기용: 사진 비교가 건너뛰었나(None) · 달라진 곳 몇 개였나. 판정에는 안 쓴다
        changes = desk.changes(after)
        check["deskChanges"] = None if changes is None else len(changes)
    if vlm is not None:  # VLM_URL (10판): 입실·퇴실 사진을 VLM에게 한 번 더 보여 준다
        new_item, vlm_seconds, _, yes = vlm.ask_with_yes(vlm.prepare(before), vlm.prepare(after))
        check["yoloStatus"] = check["status"]
        room_camera.apply_vlm(check, new_item, vlm.rule, yes)
        check["vlmYesRaw"] = yes  # 못 알아들은 답이어도 확률은 따로 남긴다 (채점 기록용)  # 게이트웨이와 같은 방법으로 합친다
        seconds += vlm_seconds  # 캐시에서 꺼냈어도 처음 물었을 때 걸린 시간을 더한다
    check["people"] = watcher.people(found)
    return check, seconds


def evidence_row(answer, check):
    """문제 하나에서 무엇이 찾았나와 정답 (물건이 있을 확률 표를 만드는 재료). 이름·사진은 안 남긴다."""
    import room_camera
    evidence = check.get("evidence") or {}
    return {"left": answer != "CLEAN", "found": check["status"] != "CLEAN",
            "key": room_camera.check_key({**check, "phase": "FIRST"}), "count": bool(evidence.get("count")),
            # 게이트웨이는 VLM을 묻기 전 결과가 남음일 때만 3시간 뒤 다시 확인한다 (recheck_base)
            "target": (check.get("yoloStatus") or check["status"]) != "CLEAN"}


def score(cases, run_case):
    """run_case(case) → (결과 dict, 초). 정확도·심각한 오류·지연과 틀린 문제를 모은다."""
    rows = []
    for case in cases:
        check, seconds = run_case(case)
        got = check["status"]
        rows.append({"case": case.name, "answer": case.answer, "got": got, "seconds": round(seconds, 3),
                     "ok": got == case.answer, "leftOk": left_ok(case.answer, got), "serious": is_serious(case.answer, got),
                     "trash": check.get("trash", {}), "leftItems": check.get("leftItems", {}),
                     "people": check.get("people", 0), "memo": case.memo, "desk": check.get("deskChanges", "off"),
                     "yolo": check.get("yoloStatus"), "vlm": check.get("vlm"), "vlmYes": check.get("vlmYesRaw"),
                     "evidence": evidence_row(case.answer, check)})
    n = len(rows)
    right = sum(r["ok"] for r in rows)
    left_right = sum(r["leftOk"] for r in rows)
    left_accuracy = left_right / n if n else 0.0
    serious = sum(r["serious"] for r in rows)
    false_alarm = sum(r["serious"] and r["answer"] == "CLEAN" for r in rows)  # 깨끗한데 신고
    median = statistics.median(r["seconds"] for r in rows) if rows else 0.0
    slowest = max((r["seconds"] for r in rows), default=0.0)
    by_answer = {a: [sum(r["ok"] for r in rows if r["answer"] == a), sum(r["answer"] == a for r in rows)]
                 for a in ANSWERS}
    return {"cases": n, "right": right, "accuracy": right / n if n else 0.0,
            "leftRight": left_right, "leftAccuracy": left_accuracy, "serious": serious,
            "falseAlarm": false_alarm, "missed": serious - false_alarm,
            "medianSeconds": round(median, 3), "maxSeconds": round(slowest, 3), "byAnswer": by_answer, "rows": rows,
            "passed": bool(n) and left_accuracy >= GOAL_ACCURACY and serious == 0 and median <= GOAL_SECONDS}


def candidate_name(env):
    """채점한 설정을 한 줄로 (README 기록용). 게이트웨이 설정 이름 그대로."""
    keys = ("YOLO_MODEL", "DESK_PHOTO", "DESK_AREA", "FLOOR_AREA", "DESK_LIGHT", "DESK_THRESH", "DESK_MIN_AREA",
            "TILE_GRID", "DESK_MOVE", "DESK_MOVE_SAME", "DESK_KEEP_SMALL", "DESK_FURNITURE_SMALL", "DESK_FURNITURE_SEEN", "DESK_MOVE_RENAME", "LABEL_MODEL", "ROOM_ITEMS", "VLM_NAME", "VLM_RULE", "VLM_SIDE", "VLM_EQ")
    if env.get("VLM_URL") and not (env.get("VLM_NAME") or env.get("VLM_RULE")):
        env = {**env, "VLM_RULE": "or"}  # VLM_URL만 줘도 VLM을 켠 것 (기본 규칙 or). 주소는 안 적는다
    parts = [f"{k}={env[k]}" for k in keys if env.get(k)]
    return " ".join(parts) or "(설정 없음)"


def report(result, candidate, out=print):
    out(f"후보: {candidate}")
    out(f"문제 {result['cases']}개 중 {result['leftRight']}개 맞음: 정확도 (깨끗함/남음) {result['leftAccuracy']:.1%} "
        f"(목표 {GOAL_ACCURACY:.0%}), 세 가지까지 {result['right']}개 {result['accuracy']:.1%}, 심각한 오류 {result['serious']}개 (목표 0, 잘못 신고 {result['falseAlarm']} · 놓침 {result['missed']}), "
        f"사진당 {result['medianSeconds']:.2f}초 (목표 {GOAL_SECONDS:.0f}초 안)")
    for a in ANSWERS:
        ok, total = result["byAnswer"][a]
        if total:
            out(f"  정답 {KOREAN[a]}: {total}개 중 {ok}개 맞음")
    wrong = [r for r in result["rows"] if not r["ok"]]
    for r in wrong:
        found = {**r["trash"], **r["leftItems"]}
        seen = ", ".join(f"{k} {v}" for k, v in sorted(found.items())) or "찾은 것 없음"
        mark = " [심각]" if r["serious"] else (" [종류만]" if r["leftOk"] else "")
        person = f" (사람 {r['people']}명 보임: 다시 찍어요)" if r["people"] else ""
        out(f"  틀림{mark} {r['case']}: 정답 {KOREAN[r['answer']]} → {KOREAN[r['got']]} ({seen}){person}{desk_note(r)}"
            f"{' - ' + r['memo'] if r['memo'] else ''}")
    skipped = [r["case"] for r in result["rows"] if r.get("desk", "off") is None]
    if any(r.get("desk", "off") != "off" for r in result["rows"]):
        out(f"사진 비교 건너뜀 (화면이 크게 달라 조명이 바뀐 것으로 봄): {len(skipped)}쌍"
            f"{' (' + ' '.join(skipped) + ')' if skipped else ''}")
    for line in vlm_yes_lines(result["rows"]):
        out(line)
    out("통과" if result["passed"] else "아직 기준 못 넘음")
    names = " ".join(r["case"] for r in wrong) or "없음"
    out("README 실험 기록에 붙일 줄:")
    out(f"| {datetime.now(KST):%m/%d} | 정리 확인 | {candidate} | {result['leftAccuracy']:.1%} (세 가지 {result['accuracy']:.1%}) "
        f"| {result['serious']} "
        f"| {result['medianSeconds']:.2f}초 | (바꾼 것) | 문제 {result['cases']}개, 틀림 {names} |")


def desk_note(row):
    """틀린 문제 줄 끝에 사진 비교(DESK_PHOTO)가 어땠는지. 꺼져 있으면 빈 글자."""
    desk = row.get("desk", "off")
    if desk == "off":
        return ""
    return " [사진 비교 건너뜀]" if desk is None else f" [사진 비교 {desk}곳]"


def vlm_yes_lines(rows, limit=0.8):
    """VLM이 "새 물건 있음"이라 할 확률을 정답별로 (11/12 지영: camera-warn 켜기 전 깨끗한 방 숫자)."""
    have = [r for r in rows if r.get("vlmYes") is not None]
    if not have:
        return []
    lines = [f"VLM 있음 확률 (사진 {len(have)}쌍, 경고 기준 {limit:.0%}):"]
    for name, pick in (("깨끗함", lambda r: r["answer"] == "CLEAN"), ("물건 남음", lambda r: r["answer"] != "CLEAN")):
        group = sorted((r for r in have if pick(r)), key=lambda r: r["vlmYes"])
        if not group:
            continue
        over = sum(r["vlmYes"] >= limit for r in group)
        lines.append(f"  정답 {name} {len(group)}쌍: " + ", ".join(f"{r['case']} {r['vlmYes']:.1%}" for r in group))
        lines.append(f"    {limit:.0%} 이상 {over}쌍, 가장 낮음 {group[0]['vlmYes']:.1%}, 가장 높음 {group[-1]['vlmYes']:.1%}")
    return lines


def save_result(folder, result, candidate, compare=False):
    """results.jsonl에 한 줄 더한다. core로 보낼 때 쓰는 eventId도 같이 적어, 다시 보내도 한 번만 저장되게 한다."""
    record = {"at": datetime.now(KST).isoformat(timespec="seconds"), "eventId": str(uuid.uuid4()), "candidate": candidate,
              **{k: v for k, v in result.items() if k != "rows"},
              "wrong": [r["case"] for r in result["rows"] if not r["ok"]],
              "vlmYes": [{"case": r["case"], "answer": r["answer"], "yes": r["vlmYes"]}
                         for r in result["rows"] if r.get("vlmYes") is not None],
              "evidence": [r["evidence"] for r in result["rows"] if r.get("evidence")]}
    if compare:  # compare 후보 줄. push-last가 다시 보낼 대상에서 뺀다 (core에는 고른 score 결과만)
        record["compare"] = True
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "results.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


# ---------- 점수를 core로 (관리자 화면 "모델 시험 정확도") ----------

def model_name(candidate):
    """채점 줄의 YOLO_MODEL 파일 이름에서 확장자를 뺀 것 (예: gateway/yolov8s.onnx → yolov8s)."""
    for part in (candidate or "").split():
        key, _, value = part.partition("=")
        if key == "YOLO_MODEL" and value:
            return os.path.splitext(os.path.basename(value))[0]
    return None


def score_body(record):
    """채점 기록 한 줄 → core POST body. 숫자만 보낸다 (사진·문제 이름·후보 설정은 안 보낸다). 정확도는 0~1 소수."""
    return {"eventId": record["eventId"], "kind": "TIDY", "model": model_name(record.get("candidate")),
            "accuracy": round(main_accuracy(record), 4), "accuracy3": round(record["accuracy"], 4),
            "cases": record["cases"], "serious": record["serious"], "medianSeconds": record["medianSeconds"],
            "measuredAt": record["at"]}


def http_post(loopback):
    """보낼 함수. 내 컴퓨터(loopback)로 보낼 때는 환경 프록시(HTTP_PROXY·ALL_PROXY 등)를 아예 안 읽는 세션을 쓴다.

    proxies={"http": None, "https": None}만 주면 ALL_PROXY는 그대로 골라져서 토큰이 프록시로 샐 수 있다.
    """
    import requests
    if not loopback:
        return requests.post
    session = requests.Session()
    session.trust_env = False
    return session.post


def push_score(record, env, out=print, post=None):
    """점수를 core로 보낸다. 실패해도 채점은 실패로 치지 않고 한 줄만 찍는다. 반환: HTTP 상태 (안 보냈으면 None)."""
    import gateway
    body = score_body(record)
    config = gateway.load_config({**env, "PORT": env.get("PORT") or "0"})  # PORT는 안 쓰지만 없으면 USB 포트를 찾으려 해서
    if config.dry_run or not env.get("SERVER"):
        out("[DRY_RUN] POST /api/rooms/%d/model-scores %s" % (config.room_camera_room, json.dumps(body, ensure_ascii=False)))
        return None
    if not gateway.is_safe_server(config.server):
        out("점수 보내기 안 함: SERVER가 https가 아니에요 (장치 토큰이 실려서 https만)")
        return None
    if not config.device_token:
        out("점수 보내기 안 함: DEVICE_TOKEN이 없어요 (.env를 읽고 실행해요)")
        return None
    url = f"{config.server}/api/rooms/{config.room_camera_room}/model-scores"
    if post is None:
        post = http_post(gateway.is_loopback(config.server))
    try:
        r = post(url, json=body, headers={"Authorization": f"Bearer {config.device_token}"},
                 timeout=(gateway.CONNECT_TIMEOUT, gateway.TIMEOUT), allow_redirects=False)
    except Exception as e:  # noqa: BLE001 - 채점 결과는 이미 저장됨. 오류 이름만 한 줄
        out(f"점수 보내기 실패: {e.__class__.__name__} (채점 기록은 그대로, push-last로 다시 보내요)")
        return None
    hint = {401: "토큰 확인", 403: "토큰·방 번호 확인", 404: "core에 아직 API가 없음"}.get(r.status_code, "")
    if 200 <= r.status_code < 300:
        out(f"점수를 core로 보냄 ({r.status_code}): {body['accuracy']:.1%}, 문제 {body['cases']}개")
    else:
        out(f"점수 보내기 실패 ({r.status_code}{', ' + hint if hint else ''}). 채점 기록은 그대로, push-last로 다시 보내요")
    return r.status_code


def last_record(folder):
    """results.jsonl에서 compare 후보가 아닌 마지막 줄. eventId가 없는 옛 줄이면 그 자리에서 정해 보낸다 (그 줄은 고치지 않는다)."""
    path = os.path.join(folder, "results.jsonl")
    if not os.path.exists(path):
        raise SystemExit(f"{path}가 없어요. 먼저 score를 돌려요")
    with open(path, encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]
    lines = [line for line in lines if not json.loads(line).get("compare")]
    if not lines:
        raise SystemExit(f"{path}에 보낼 score 결과가 없어요 (compare 후보 줄만 있음)")
    record = json.loads(lines[-1])
    record.setdefault("eventId", str(uuid.uuid5(uuid.NAMESPACE_URL, "mspace-tidy-score:" + lines[-1].strip())))
    return record


def peak_memory_mb():
    """이 프로그램이 지금까지 쓴 메모리 최대 (MB). 젯슨은 GPU와 메모리를 같이 써서 모델 메모리도 여기 들어간다."""
    try:
        import resource
    except ImportError:  # 윈도우
        return None
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(kb / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)  # 맥은 바이트, 리눅스는 KB


def run_score(folder, env, out=print, as_json=False, clock=time.perf_counter, push=True):
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
    detector = room_camera.make_detector(config.yolo_model, config.desk_area, config.floor_area, config.tile_grid)
    find = getattr(detector, "detect_detailed", detector.detect)
    use_desk = config.desk_photo and config.desk_area
    if config.desk_photo and not config.desk_area:
        out("DESK_PHOTO=1은 DESK_AREA가 있어야 해요. 책상 사진 비교 없이 채점해요")
    labeler = room_camera.make_labeler(detector, config.label_model) if use_desk else None
    import vlm_check
    vlm = vlm_check.from_env(env)
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
        desk = desk_diff.DeskCompare(config.desk_area, **desk_diff.settings(config)) if use_desk else None  # 메모리에만 (파일 안 씀)
        return judge(before, after, timed_find, config.room_items, desk, labeler, vlm=vlm)

    run_case(cases[0])  # 처음 한 번은 모델 준비로 느리다. 시간에 넣지 않고, 첫 장까지를 "모델 올리기"로 둔다
    load_seconds = first_seen[0] - loading
    result = score(cases, run_case)
    result["loadSeconds"] = round(load_seconds, 1)
    result["skipped"] = problems  # 건너뛴 문제. 비교표에 같이 적어 문제집이 다른 걸 알아보게 한다
    result["memoryMB"] = peak_memory_mb()
    result["vlmModel"] = vlm.model if vlm is not None else None  # 게이트웨이가 같은 VLM 모델의 점수만 쓰게 (model_score)
    candidate = candidate_name(env)
    report(result, candidate, out)
    record = save_result(folder, result, candidate, compare=as_json)
    if as_json:  # compare가 읽는 줄
        summary = {k: v for k, v in result.items() if k != "rows"}
        summary["wrong"] = [r["case"] for r in result["rows"] if not r["ok"]]
        out(RESULT_MARK + json.dumps(summary, ensure_ascii=False))
    elif push:  # compare가 후보마다 부를 때는 안 보낸다 (고른 결과만 화면에)
        push_score(record, env, out)
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


def default_timeout(folder):
    """후보 하나 채점 제한 기본값 (초). 문제마다 입실 전·퇴실 뒤 두 장을 사진당 목표 시간까지 봐도 끝나게 + 10분 여유."""
    cases, _ = read_answers(folder)
    return max(TIMEOUT_MIN_SEC, len(cases) * 2 * GOAL_SECONDS + 600)


def run_one(folder, candidate, base_env, run=None):
    """후보 하나를 따로 띄운 프로그램에서 채점한다 (모델 올리기 시간·메모리를 후보마다 깨끗하게 재려고).

    TIDY_TIMEOUT(초, 기본은 default_timeout: 47쌍이면 약 3시간 18분)이 지나도 안 끝나면 그 후보는 실패로 두고 다음으로.
    """
    import subprocess
    run = run or subprocess.run
    env = {**base_env, **candidate.env}
    fallback = default_timeout(folder)
    try:
        timeout = float(env.get("TIDY_TIMEOUT") or fallback)
    except ValueError:
        timeout = fallback
    if not math.isfinite(timeout) or timeout <= 0:  # 0 이하·NaN·inf면 기본값
        timeout = fallback
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
    if main_accuracy(result) < GOAL_ACCURACY:
        why.append("정확도")
    if result["serious"]:
        why.append("심각 오류")
    if result["medianSeconds"] > GOAL_SECONDS:
        why.append("시간")
    return "후보" if not why else "탈락 (" + ", ".join(why) + ")"


def rank(rows):
    """기준을 넘은 것 → 정확도 (깨끗함/남음) 높은 순 → 심각 오류 적은 순 → 세 가지 정확도 높은 순 → 빠른 순.

    맨 위를 채택으로 바꾼다. 아무도 기준을 못 넘었어도 맨 위를 "채택 (기준 미달: …)"로 둔다 (지금 쓸 것 + 다음 개선의 출발점).
    """
    def key(row):
        r = row["result"]
        if "error" in r:
            return (2, 0, 0, 0, 0)
        return (0 if row["verdict"] == "후보" else 1, -main_accuracy(r), r["serious"], -r["accuracy"], r["medianSeconds"])
    rows = sorted(rows, key=key)
    if rows and rows[0]["verdict"] == "후보":
        rows[0]["verdict"] = "채택"
    elif rows and "error" not in rows[0]["result"]:
        rows[0]["verdict"] = "채택 (기준 미달: " + rows[0]["verdict"].removeprefix("탈락 (")
    return rows


def compare_table(rows):
    """비교표 (마크다운). README 실험 기록·포트폴리오에 그대로 붙인다."""
    head = ("| 순위 | 판단 | 후보 | 정확도 (깨끗함/남음) | 세 가지 정확도 (깨끗함 / 치울 것 / 두고 감) "
            "| 심각 오류 (잘못 신고 / 놓침) | 사진당 시간 (최대) | 메모리 최대 | 모델 올리기 | 라이선스 |")
    lines = [head, "|---|---|---|---|---|---|---|---|---|---|"]
    for i, row in enumerate(rows, 1):
        r = row["result"]
        if "error" in r:
            lines.append(f"| {i} | 실패 | {row['name']} | {r['error']} | | | | | | {row['license']} |")
            continue
        parts = []
        for a in ANSWERS:
            ok, total = r["byAnswer"][a]
            parts.append(f"{ok / total:.0%}" if total else "-")
        memory = f"{r['memoryMB']:.0f}MB" if r.get("memoryMB") is not None else "-"
        name = row["name"] + (f" ({len(r['skipped'])}문제 건너뜀)" if r.get("skipped") else "")
        split = f" ({r['falseAlarm']} / {r['missed']})" if "falseAlarm" in r else ""
        lines.append(f"| {i} | {row['verdict']} | {name} | **{main_accuracy(r):.1%}** | {r['accuracy']:.1%} ({' / '.join(parts)}) "
                     f"| {r['serious']}{split} | {r['medianSeconds']:.2f}초 ({r['maxSeconds']:.2f}초) | {memory} "
                     f"| {r['loadSeconds']:.1f}초 | {row['license']} |")
    return "\n".join(lines)


def chosen_line(rows):
    """맨 위 한 줄 요약: 채택한 후보와 그 점수 (강사님 LAB 화면의 "지금 쓰는 방식"처럼)."""
    top = rows[0] if rows else None
    if top is None or "error" in top["result"]:
        return "**채택:** 없음 (모든 후보 실패)"
    r = top["result"]
    memory = f" · 메모리 {r['memoryMB']:.0f}MB" if r.get("memoryMB") is not None else ""
    return (f"**채택:** {top['name']} · 정확도 (깨끗함/남음) {main_accuracy(r):.1%} · 세 가지 {r['accuracy']:.1%} · 심각 오류 {r['serious']} "
            f"· 사진당 {r['medianSeconds']:.2f}초{memory}")


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
    text = (f"### {datetime.now(KST):%Y-%m-%d %H:%M} · 정리 확인 후보 비교 (문제 {cases}개, 후보 {len(rows)}개)\n\n"
            f"{chosen_line(rows)}\n\n"
            f"목표: 정확도 (깨끗함/남음) {GOAL_ACCURACY:.0%} 이상 · 심각 오류 0 · 사진당 {GOAL_SECONDS:.0f}초 안\n\n{table}\n")
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
    if not argv or argv[0] not in ("capture", "score", "compare", "push-last"):
        raise SystemExit("사용법: tidy_eval.py capture|score|compare|push-last [문제집 폴더, 기본 eval/tidy]")
    args = [a for a in argv[1:] if not a.startswith("--")]
    folder = args[0] if args else DEFAULT_DIR
    if argv[0] == "capture":
        run_capture(folder, env)
    elif argv[0] == "compare":
        run_compare(folder, env)
    elif argv[0] == "push-last":
        push_score(last_record(folder), env)
    else:
        run_score(folder, env, as_json="--json" in argv)


if __name__ == "__main__":
    main()
