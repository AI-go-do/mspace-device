"""지금 켠 정리 확인 설정의 시험 점수를 core에 같이 보낸다 (11/12 수연: 관리자 화면의 "모델 시험 정확도"를 하드코딩 대신).

젯슨의 채점 기록(eval/tidy/results.jsonl, tidy_eval score가 한 줄씩 적음)에서
게이트웨이와 같은 설정으로 채점한 가장 최근 줄을 찾는다. 숫자만 보낸다: 사진·문제 이름은 보내지 않는다.
같은 설정의 기록이 없으면 보내지 않는다 (다른 설정의 점수를 붙이면 화면이 틀린 말을 한다).
"""
import json
import math
import os

DEFAULT_FILE = "eval/tidy/results.jsonl"
# vlmModel을 적기 전(11/12 전) 채점 줄의 VLM_NAME → 그때 쓴 모델 파일 (README 10판: Qwen3-VL-2B Q8_0)
OLD_VLM_FILES = {"qwen": "Qwen3-VL-2B-Instruct-Q8_0.gguf"}


def candidate_env(candidate):
    """채점 기록의 candidate("YOLO_MODEL=... DESK_PHOTO=1 ...")를 환경변수 dict로."""
    env = {}
    for part in (candidate or "").split():
        key, sep, value = part.partition("=")
        if sep:
            env[key] = value
    if "VLM_NAME" in env or "VLM_RULE" in env:  # 채점 줄에는 VLM_URL이 안 적힌다. VLM을 켠 채점이면 둘 중 하나가 있다
        env["VLM_URL"] = "http://127.0.0.1"
    return env


def setup_key(config, vlm=None):
    """점수를 가르는 설정만 모은다. 모델 파일은 이름만 본다 (폴더가 달라도 같은 모델).

    vlm: VLM을 실제로 켰는지 (게이트웨이는 make_vlm 결과로 정한다. 없으면 VLM_URL로)
    """
    vlm = bool(config.vlm_url) if vlm is None else vlm
    base = lambda p: os.path.basename(p) if p else None
    return (base(config.yolo_model), base(config.label_model), config.desk_photo, config.desk_area, config.floor_area,
            config.desk_light, config.desk_thresh, config.desk_min_area, config.desk_move, config.desk_move_same,
            getattr(config, "desk_keep_small", 0.0), getattr(config, "desk_furniture_small", 0.0),
            getattr(config, "desk_furniture_seen", False),
            tuple(sorted(config.room_items.items())), vlm, config.tile_grid,
            (config.vlm_rule, config.vlm_side, config.vlm_eq) if vlm else None)


def same_vlm(record, vlm_model):
    """채점 줄이 게이트웨이와 같은 VLM 모델로 채점한 것인가.

    새 줄: tidy_eval이 적은 vlmModel(서버가 알려 준 이름)이 같아야 한다.
    vlmModel이 없는 옛 줄(11/12 전): 그때 붙인 VLM_NAME(예: qwen)으로 OLD_VLM_FILES의 모델 파일을 찾아
    게이트웨이 모델 파일 이름과 똑같을 때만 같은 모델로 본다 (Qwen2·다른 양자화는 다른 모델).
    다시 채점하지 않아도 10판 점수를 쓸 수 있게 (11/12 수연 "채점 오래 걸림").
    """
    if not vlm_model:
        return False
    if "vlmModel" in record:  # 새 줄: 칸이 있으면 그 이름만 본다 (null = VLM 없이 채점 → 다른 설정)
        return record["vlmModel"] == vlm_model
    name = candidate_env(record.get("candidate")).get("VLM_NAME", "").strip().lower()
    return OLD_VLM_FILES.get(name) == os.path.basename(vlm_model)


MIN_CASES = 5      # 이보다 적은 경우는 믿기 어려워서
LOW = 0.5          # 이 값보다 높게 주지 않는다 (core 경고 기준 0.8보다 낮게: 알림만, 잘못 경고하지 않게)


def presence_table(rows):
    """채점 줄의 evidence(문제마다 무엇이 찾았나·정답) → {열쇠: 물건이 있을 확률}. 없으면 None.

    열쇠는 room_camera.check_key와 같다 ("yolo+desk+vlm", 3시간 뒤는 "recheck").
    값 = 그 경우에 기기가 "남음"이라 했을 때 실제로 물건이 남아 있던 비율 (이름은 안 본다).
    문제가 MIN_CASES개보다 적은 경우는 LOW를 넘지 않게 낮춘다.
    """
    if not isinstance(rows, list) or not rows:
        return None
    counts, valid = {}, False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("left"), bool):
            continue  # 정답이 없는 줄은 셀 수 없다
        valid = True
        left = bool(row.get("left"))
        keys = []
        if row.get("found") and isinstance(row.get("key"), str):
            keys.append(row["key"])
        # 3시간 뒤 다시 확인은 첫 확인(VLM 전)이 남음이었던 예약만 한다 (checkout_check의 recheck_base)
        if row.get("count") and row.get("target", True):
            keys.append("recheck")
        for key in keys:
            hit, n = counts.get(key, (0, 0))
            counts[key] = (hit + left, n + 1)
    table = {}
    for key, (hit, n) in counts.items():
        rate = hit / n
        table[key] = round(rate if n >= MIN_CASES else min(rate, LOW), 3)
    return table if valid else None  # 근거는 있는데 남음이 한 번도 없으면 빈 표 (모든 경우 LOW)


def describe_presence(table):
    if table == {}:
        return f"물건이 있을 확률: 채점에서 남음이라 한 경우가 없어서 모두 {LOW:.0%}로 보내요 (알림만)"
    if not table:
        return "물건이 있을 확률: 채점 기록에 경우별 결과가 없어서 예전처럼 이름별 점수를 보내요 (tidy_eval score를 한 번 더 돌리면 생겨요)"
    parts = ", ".join(f"{k} {v:.0%}" for k, v in sorted(table.items(), key=lambda kv: -kv[1]))
    return f"물건이 있을 확률 (채점에서 실제로 남아 있던 비율): {parts} · 표에 없는 경우는 {LOW:.0%}"


def find(config, load_config, path=DEFAULT_FILE, vlm_model=None):
    """config와 같은 설정으로 채점한 가장 최근 점수. 없으면 None.

    load_config: gateway.load_config (채점 줄의 설정도 게이트웨이와 같은 기본값으로 읽으려고 받는다)
    vlm_model: 게이트웨이가 실제로 켠 VLM의 모델 이름 (VLM을 안 켰으면 None).
               VLM을 켰으면 같은 모델로 채점한 줄만 쓴다 (same_vlm)
    """
    if not path or not os.path.isfile(path):
        return None
    vlm_on = vlm_model is not None
    want, found = setup_key(config, vlm_on), None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                    env = {"PORT": "none", **candidate_env(record.get("candidate"))}
                    if setup_key(load_config(env)) != want:
                        continue
                    if vlm_on and not same_vlm(record, vlm_model):
                        continue
                    score = {"setup": record.get("candidate", ""), "accuracy": round(float(record["leftAccuracy"]), 4),
                             "cases": int(record["cases"]), "secondsPerPhoto": round(float(record["medianSeconds"]), 2),
                             "measuredAt": record.get("at")}
                    secs = score["secondsPerPhoto"]
                    if 0 <= score["accuracy"] <= 1 and score["cases"] > 0 and math.isfinite(secs) and secs >= 0:
                        score["presence"] = presence_table(record.get("evidence"))  # core에는 안 보냄 (gateway가 뺌)
                        found = score  # 아래 줄일수록 최근. 숫자가 이상한 줄(NaN·음수 포함)은 건너뛴다
                except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
                    continue
    except OSError as e:
        print(f"채점 기록을 못 읽어서 시험 점수는 안 보내요 ({path}: {e.__class__.__name__})")
        return None
    return found


def describe(score):
    if score is None:
        return "모델 시험 점수: 이 설정으로 채점한 기록이 없어서 core에 안 보내요 (tidy_eval score를 같은 설정으로 돌리면 생겨요)"
    return (f"모델 시험 점수: {score['accuracy']:.1%} (문제 {score['cases']}개, 사진당 {score['secondsPerPhoto']}초, "
            f"{score['measuredAt']}) · 정리 확인과 같이 core에 보내요")
