"""공간 등록: 새 방에 카메라를 단 뒤 5분 동안 빈 방을 여러 번 보고 방 비품(ROOM_ITEMS)을 자동으로 정한다.

지금은 방마다 `ROOM_ITEMS=remote:1`처럼 사람이 직접 적는다. 이걸 대신한다.
- 몇 초마다 한 장씩 찍어 정리 확인과 같은 방법(detect_detailed: 전체 + 조각 + DESK_AREA·FLOOR_AREA)으로 물건을 센다
- 사람이 보이는 장은 쓰지 않는다 (사람이 든 휴대폰·가방을 비품으로 넣지 않게)
- 쓸 수 있는 장의 SHARE(기본 80%) 이상에서 n개 이상 보인 물건만 비품 n개로 정한다 (한두 장에서 잘못 본 것은 빠짐)
- 치울 것(컵·병·종이 …)은 비품으로 넣지 않고 "치우고 다시 등록하세요"라고 알려 준다
- 결과는 젯슨의 room_profile.json에만 쓴다. 사진은 저장하지 않고 서버로도 보내지 않는다
- 게이트웨이는 ROOM_ITEMS를 안 주면 이 파일의 비품을 쓴다 (ROOM_ITEMS를 주면 그게 먼저)

젯슨에서 (게이트웨이는 꺼 두고, 방은 비우고 정리된 상태로):
  cd ~/mspace-device && ROOM_CAMERA=2 YOLO_MODEL=gateway/yolov8s.onnx TILE_GRID=3 \\
      ~/mspace-venv/bin/python gateway/room_setup.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone

SETUP_MIN = 5          # 몇 분 동안 보나 (SETUP_MIN)
SETUP_EVERY = 10       # 몇 초마다 한 장 (SETUP_EVERY)
SETUP_SHARE = 0.8      # 쓸 수 있는 장 중 이 비율 이상에서 보여야 비품 (SETUP_SHARE)
MIN_FRAMES = 5         # 사람 없는 장이 이보다 적으면 등록하지 않는다
PROFILE_FILE = "room_profile.json"


def decide_room_items(frame_counts, share=SETUP_SHARE, trash=frozenset()):
    """장마다 센 물건 [{"remote": 1}, {"remote": 1, "cup": 1}, …] → (비품 {"remote": 1}, 치울 것 {"cup": 1}).

    물건마다 "n개 이상 보인 장이 share 이상"인 가장 큰 n을 고른다. 0이면 넣지 않는다.
    """
    frames = len(frame_counts)
    if frames == 0:
        return {}, {}
    labels = sorted({label for counts in frame_counts for label in counts})
    items, leftover = {}, {}
    for label in labels:
        seen = sorted((counts.get(label, 0) for counts in frame_counts), reverse=True)
        k = math.ceil(frames * share - 1e-9) - 1  # share 이상의 장에서 보인 개수 = 큰 쪽부터 그 순서의 값
        n = seen[max(0, min(k, frames - 1))]
        if n <= 0:
            continue
        (leftover if label in trash else items)[label] = n
    return items, leftover


def format_room_items(items):
    """{"remote": 1, "book": 3} → "book:3,remote:1" (ROOM_ITEMS 모양)."""
    return ",".join(f"{label}:{n}" for label, n in sorted(items.items()))


def write_profile(path, items, frames, skipped, minutes):
    profile = {"items": items, "frames": frames, "skippedPeople": skipped, "minutes": minutes,
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return profile


def load_profile_items(path):
    """room_profile.json의 비품. 없거나 못 읽으면 None."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f).get("items")
    except (OSError, ValueError, AttributeError) as e:
        print(f"공간 등록 파일을 못 읽어서 안 써요 ({path}: {e})")
        return None
    if not isinstance(items, dict):
        return None
    return {str(k): int(v) for k, v in items.items() if isinstance(v, int) and v > 0}


def watch(detect, grab, minutes, every, now=time.monotonic, sleep=time.sleep, log=print):
    """minutes 동안 every초마다 grab()한 장을 detect()로 세서 (장마다 센 것, 사람 때문에 건너뛴 장 수)를 돌려준다."""
    import room_camera
    counted, skipped = [], 0
    end = now() + minutes * 60
    while True:
        frame = grab()
        if frame is not None:
            dets = detect(frame)
            if any(d.label == "person" for d in dets):
                skipped += 1
                log("사람이 보여서 이 장은 안 써요")
            else:
                counts = {}
                for d in dets:
                    if d.label in room_camera.TRASH or d.label in room_camera.BELONGINGS:
                        counts[d.label] = counts.get(d.label, 0) + 1
                counted.append(counts)
                log(f"{len(counted)}장째: {room_camera.describe(counts) or '물건 없음'}")
        if now() + every > end:
            return counted, skipped
        sleep(every)


def main(env=None):
    env = os.environ if env is None else env
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gateway
    import room_camera
    config = gateway.load_config(dict(env))
    if not config.room_camera or not config.yolo_model:
        raise SystemExit("ROOM_CAMERA(웹캠 번호)와 YOLO_MODEL(모델 파일)을 같이 주세요.")
    minutes = gateway.parse_number(env, "SETUP_MIN", SETUP_MIN, 1, 60, float)
    every = gateway.parse_number(env, "SETUP_EVERY", SETUP_EVERY, 1, 600, float)
    share = gateway.parse_number(env, "SETUP_SHARE", SETUP_SHARE, 0.5, 1.0, float)
    path = env.get("ROOM_PROFILE") or PROFILE_FILE
    detector = room_camera.make_detector(config.yolo_model, config.desk_area, config.floor_area, config.tile_grid)
    cam = room_camera.open_camera(config.room_camera)

    def grab():
        for _ in range(3):  # 웹캠 버퍼에 남은 옛 장을 버린다
            cam.grab()
        ok, frame = cam.read()
        return frame if ok else None

    print(f"공간 등록: {minutes:g}분 동안 {every:g}초마다 빈 방을 봐요. 방을 비우고 정리된 상태로 두세요")
    try:
        counted, skipped = watch(detector.detect_detailed, grab, minutes, every)
    finally:
        cam.release()
    if len(counted) < MIN_FRAMES:
        raise SystemExit(f"사람 없는 장이 {len(counted)}장뿐이라 등록하지 않았어요 (최소 {MIN_FRAMES}장). "
                         "방을 비우고 다시 해 주세요.")
    items, leftover = decide_room_items(counted, share, room_camera.TRASH)
    if leftover:
        raise SystemExit(f"치울 것이 계속 보여요 ({room_camera.describe(leftover)}). 치우고 다시 등록해 주세요. "
                         "파일은 그대로 뒀어요.")
    write_profile(path, items, len(counted), skipped, minutes)
    print(f"등록 끝: {len(counted)}장 사용 (사람 때문에 {skipped}장 건너뜀)")
    print(f"방 비품: {room_camera.describe(items) or '없음'} → {path}에 적었어요")
    print(f"게이트웨이는 ROOM_ITEMS를 안 주면 이걸 써요. 직접 적으려면: ROOM_ITEMS={format_room_items(items)}")
    return items


if __name__ == "__main__":
    main()
