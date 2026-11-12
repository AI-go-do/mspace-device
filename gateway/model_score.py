"""지금 켠 정리 확인 설정의 시험 점수를 core에 같이 보낸다 (11/12 수연: 관리자 화면의 "모델 시험 정확도"를 하드코딩 대신).

젯슨의 채점 기록(eval/tidy/results.jsonl, tidy_eval score가 한 줄씩 적음)에서
게이트웨이와 같은 설정으로 채점한 가장 최근 줄을 찾는다. 숫자만 보낸다: 사진·문제 이름은 보내지 않는다.
같은 설정의 기록이 없으면 보내지 않는다 (다른 설정의 점수를 붙이면 화면이 틀린 말을 한다).
"""
import json
import os

DEFAULT_FILE = "eval/tidy/results.jsonl"


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
            tuple(sorted(config.room_items.items())), vlm,
            (config.vlm_rule, config.vlm_side, config.vlm_eq) if vlm else None)


def same_vlm(record, vlm_model):
    """채점 줄이 게이트웨이와 같은 VLM 모델로 채점한 것인가.

    새 줄: tidy_eval이 적은 vlmModel(서버가 알려 준 이름)이 같아야 한다.
    vlmModel이 없는 옛 줄(11/12 전): 그때 붙인 VLM_NAME(예: qwen)이 모델 이름 안에 있으면 같은 모델로 본다.
    다시 채점하지 않아도 10판 점수를 쓸 수 있게 (11/12 수연 "채점 오래 걸림").
    """
    if not vlm_model:
        return False
    if record.get("vlmModel"):
        return record["vlmModel"] == vlm_model
    name = candidate_env(record.get("candidate")).get("VLM_NAME", "").strip().lower()
    return bool(name) and name in vlm_model.lower()


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
                    if 0 <= score["accuracy"] <= 1 and score["cases"] > 0:
                        found = score  # 아래 줄일수록 최근. 숫자가 이상한 줄은 건너뛴다
                except (ValueError, TypeError, AttributeError, KeyError):
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
