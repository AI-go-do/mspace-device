"""깨끗한 책상 사진과 지금 책상을 비교해서 달라진 곳(남은 물건 자리)을 찾는다 (OpenCV).

모델(80종)이 모르는 물건(종이, 보조배터리, 과자 봉지 …)도 "책상 위에 뭔가 남아 있음"으로 잡으려고 쓴다.
달라진 곳에는 room_camera가 모델을 한 번 더 돌려 이름을 붙이고, 안 붙으면 "모르는 물건"으로 둔다.

개인정보
  DESK_PHOTO=1 일 때만 켜진다(기본 꺼짐). 사람이 0명일 때 찍은 깨끗한 책상 사진 한 장만 젯슨 안에 둔다
  (DESK_AREA 부분만, 흑백·작게). 서버로 보내지 않는다. 퇴실 뒤 사진은 비교하고 바로 버린다.
"""
from __future__ import annotations

import hashlib
import json
import os

WORK_WIDTH = 640     # 비교는 책상을 이 폭으로 줄여서 한다 (빠르고 잔잡음이 줄어듦)
DIFF_THRESH = 35     # 밝기 차이가 이보다 크면 "달라짐" (0~255)
MIN_AREA = 0.0012    # 책상 넓이의 이 비율보다 작은 차이는 무시 (먼지·잡음). 640폭에서 약 22×22
MAX_AREA = 0.35      # 이보다 크면 물건이 아니라 조명·카메라가 바뀐 것으로 본다
LIGHT_BLUR = 0.05    # DESK_LIGHT=local: 책상 폭의 이 비율 크기로 흐린 사진(= 그 자리 밝기)으로 나눠서 비교한다
SSIM_SIGMA = 3.0     # DESK_LIGHT=ssim: 모양을 비교할 작은 창 크기 (가우스 시그마, 640폭 기준 약 15×15)
LIGHT_MODES = ("mean", "local", "ssim")
MOVE_SAME = 1.0      # DESK_MOVE=1: 기준 사진 쪽 무늬가 지금의 이 비율 이상이면 "원래 뭔가 있던 자리"(옮김·뒤집음·치움)로 본다
MOVE_SIZE = 2.5      # DESK_MOVE=1: 옮긴 짝으로 볼 두 자리의 넓이 차이 (배)
MOVE_CONTENT = 2.0   # DESK_MOVE=1: 옮긴 짝으로 볼 두 자리의 무늬 양 차이 (배)


def desk_box(width, height, area):
    """DESK_AREA 비율 (x1, y1, x2, y2) → 화면 픽셀 (x, y, w, h)."""
    x1, y1, x2, y2 = area
    x, y = int(width * x1), int(height * y1)
    return x, y, max(1, int(width * x2) - x), max(1, int(height * y2) - y)


def prepare(frame, area):
    """책상 부분만 잘라 흑백·작게·살짝 흐리게. 돌려주는 scale로 원래 화면 좌표로 되돌린다."""
    import cv2
    h, w = frame.shape[:2]
    x, y, dw, dh = desk_box(w, h, area)
    crop = frame[y:y + dh, x:x + dw]
    scale = WORK_WIDTH / dw
    small = cv2.resize(crop, (WORK_WIDTH, max(1, int(dh * scale))), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
    return cv2.GaussianBlur(gray, (5, 5), 0), (x, y, scale)


def match_light(base, now):
    """지금 사진의 전체 밝기를 기준 사진에 맞춘다 (불·햇빛이 조금 바뀐 정도는 차이로 안 보이게)."""
    import numpy as np
    mean_now = float(now.mean()) or 1.0
    return np.clip(now.astype(np.float32) * (float(base.mean()) / mean_now), 0, 255).astype(np.uint8)


def flatten_light(img):
    """자리마다 밝기를 고르게 한다 (블라인드·문틈 빛처럼 한쪽만 밝아진 것은 지우고 물건 가장자리는 남김)."""
    import cv2
    import numpy as np
    sigma = max(1.0, img.shape[1] * LIGHT_BLUR)
    glow = cv2.GaussianBlur(img.astype(np.float32), (0, 0), sigma) + 1.0
    return np.clip(img.astype(np.float32) / glow * 128.0, 0, 255).astype(np.uint8)


def ssim_diff(base, now, sigma=SSIM_SIGMA):
    """자리마다 "모양이 얼마나 다른가"를 0~255로 (SSIM). 밝기·대비가 통째로 바뀐 것은 거의 0으로 나온다.

    SSIM은 작은 창마다 평균 밝기·대비·무늬(상관)를 따로 비교한다. 빛이 세지거나 그림자가 져도 무늬가 같으면
    비슷하다고 보고, 물건이 생겨 무늬가 바뀐 곳만 크게 나온다. 값 = (1 - SSIM) / 2 × 255.
    """
    import cv2
    import numpy as np
    a, b = base.astype(np.float32), now.astype(np.float32)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2

    def blur(x):
        return cv2.GaussianBlur(x, (0, 0), sigma)

    mu_a, mu_b = blur(a), blur(b)
    var_a = blur(a * a) - mu_a * mu_a
    var_b = blur(b * b) - mu_b * mu_b
    cov = blur(a * b) - mu_a * mu_b
    ssim = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a * mu_a + mu_b * mu_b + c1) * (var_a + var_b + c2))
    return np.clip((1.0 - ssim) * 127.5, 0, 255).astype(np.uint8)


def edge_energy(img):
    """가장자리(무늬)가 얼마나 많은지 (소벨 크기 평균). 맨 책상은 작고, 물건이 있으면 크다."""
    import cv2
    import numpy as np
    if not img.size:
        return 0.0
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(img, cv2.CV_32F, 0, 1, ksize=3)
    return float(np.mean(np.abs(gx) + np.abs(gy)))


def box_energy(img, box):
    """상자 안 무늬 양 (가장자리 세기 × 넓이)."""
    x, y, w, h = box
    return edge_energy(img[y:y + h, x:x + w]) * w * h


def split_moved(base, now, boxes, same=MOVE_SAME, size=MOVE_SIZE, content=MOVE_CONTENT):
    """달라진 곳을 (새로 생긴 곳, 원래 있던 걸 옮긴 곳)으로 나눈다 (DESK_MOVE=1, 9판).

    1. 기준 사진 쪽에도 무늬가 비슷하게(same 배 이상) 있던 자리: 원래 있던 물건을 뒤집거나 살짝 옮겼거나 치운 자리.
    2. 새로 생긴 자리라도, 크기·무늬 양이 비슷한 "없어진 자리"가 따로 있으면 그 물건을 옮겨 온 것.
    이름(YOLO)을 몰라도 리모컨처럼 방에 원래 있던 걸 옮긴 것을 새 물건으로 세지 않는다.
    """
    kept, moved, gone = [], [], []
    for b in boxes:
        before, after = box_energy(base, b), box_energy(now, b)
        if before >= same * after:
            moved.append(b)
            if before > after:
                gone.append((b, before - after))
        else:
            kept.append((b, after - before))
    new = []
    for b, amount in kept:
        match = next((g for g in gone if max(b[2] * b[3], g[0][2] * g[0][3]) <= size * min(b[2] * b[3], g[0][2] * g[0][3])
                      and max(amount, g[1]) <= content * max(min(amount, g[1]), 1e-6)), None)
        if match is None:
            new.append(b)
        else:
            gone.remove(match)
            moved.append(b)
    return new, moved


def find_changes(base, now, thresh=DIFF_THRESH, min_area=MIN_AREA, max_area=MAX_AREA, light="mean",
                 move=False, moved=None, move_same=MOVE_SAME, keep_small=0.0):
    """두 책상 사진(prepare 결과)에서 달라진 곳 상자 (x, y, w, h) 목록. 조명이 통째로 바뀌었으면 None.

    light="mean": 사진 전체 밝기만 맞춘다 (원래 방식). "local": 자리마다 밝기를 고르게 한 뒤 비교한다.
    "ssim": 밝기 차이 대신 자리마다 모양(무늬)이 달라졌는지 본다 (빛·그림자에 가장 덜 예민, 조금 느림).
    move=True: 원래 있던 걸 옮기거나 뒤집은 자리는 빼고, 뺀 자리는 moved 목록에 넣는다 (DESK_MOVE=1).
    keep_small>0: 달라진 곳이 max_area를 넘어도 비교를 포기하지 않고, 사진 넓이의 keep_small보다 작은 곳만 남긴다
        (DESK_KEEP_SMALL. 화면 전체 비교에서 카메라 밝기가 조금 바뀌어도 물건 자리는 계속 본다).
    """
    import cv2
    import numpy as np
    if base.shape != now.shape:
        return None
    if light == "ssim":
        diff = ssim_diff(base, now)
    elif light == "local":
        diff = cv2.absdiff(flatten_light(base), flatten_light(now))
    else:
        diff = cv2.absdiff(base, match_light(base, now))
    _, mask = cv2.threshold(diff, thresh, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))       # 점 잡음 지우기
    mask = cv2.dilate(mask, np.ones((9, 9), np.uint8), iterations=2)                # 한 물건 조각 잇기
    total = base.shape[0] * base.shape[1]
    too_much = cv2.countNonZero(mask) > max_area * total
    if too_much and not keep_small:
        return None
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = join_near([tuple(int(v) for v in cv2.boundingRect(c)) for c in contours])
    boxes = [b for b in boxes if b[2] * b[3] >= min_area * total]
    if too_much:  # 큰 변화(조명·그림자)는 빼고 물건 크기만 남긴다
        boxes = [b for b in boxes if b[2] * b[3] <= keep_small * total]
    if move:
        boxes, out = split_moved(base, now, boxes, same=move_same)
        if moved is not None:
            moved.extend(out)
    return boxes


def join_near(boxes, gap=12):
    """가까운 상자를 하나로 합친다. 책상과 색이 비슷한 종이는 가장자리만 달라 보여서 두세 조각으로 나뉜다."""
    boxes = list(boxes)
    joined = True
    while joined:
        joined = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                ax, ay, aw, ah = boxes[i]
                bx, by, bw, bh = boxes[j]
                if ax - gap <= bx + bw and bx - gap <= ax + aw and ay - gap <= by + bh and by - gap <= ay + ah:
                    x, y = min(ax, bx), min(ay, by)
                    boxes[i] = (x, y, max(ax + aw, bx + bw) - x, max(ay + ah, by + bh) - y)
                    del boxes[j]
                    joined = True
                    break
            if joined:
                break
    return boxes


def to_frame(box, offset):
    """prepare 좌표 상자 → 원래 화면 좌표."""
    x0, y0, scale = offset
    bx, by, bw, bh = box
    return (x0 + bx / scale, y0 + by / scale, bw / scale, bh / scale)


def fingerprint(img):
    """사진 내용의 짧은 지문 (사진과 메타데이터가 같은 때 저장된 짝인지 확인용)."""
    return hashlib.sha1(img.tobytes()).hexdigest()


class DeskCompare:
    """깨끗한 책상 사진 한 장을 들고 있다가 퇴실 뒤 책상과 비교한다."""

    def __init__(self, area, path=None, thresh=DIFF_THRESH, min_area=MIN_AREA, light="mean", move=False,
                 move_same=MOVE_SAME, keep_small=0.0, furniture_small=0.0):
        self.area = area
        # DESK_THRESH·DESK_MIN_AREA·DESK_LIGHT·DESK_MOVE·DESK_MOVE_SAME·DESK_KEEP_SMALL
        self.tune = {"thresh": thresh, "min_area": min_area, "light": light, "move": move, "move_same": move_same,
                     "keep_small": keep_small}
        self.furniture_small = furniture_small  # DESK_FURNITURE_SMALL: room_camera.desk_items가 쓴다
        self.path = path              # 껐다 켜도 남게 저장할 파일 (.png). None이면 메모리에만
        self.base = None              # 깨끗한 책상 (prepare 결과)
        self.known = []               # 기준 때 모델이 찾은 물건 자리 (리모컨을 옮기면 빈 자리도 "달라짐"이라 뺀다)
        self.known_labels = []        # known과 같은 순서의 이름 (DESK_MOVE용). 파일에도 같이 적는다
        self.moved = []               # 마지막 비교에서 "옮긴 것"으로 뺀 자리 (원래 화면 좌표, DESK_MOVE용)
        self.load()

    @property
    def ready(self):
        return self.base is not None

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            import cv2
            img = cv2.imread(self.path, cv2.IMREAD_GRAYSCALE)
            if img is None:
                raise ValueError("이미지를 못 읽음")
            with open(self.path + ".json", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("sum") != fingerprint(img):
                raise ValueError("사진과 메타데이터 짝이 안 맞음")
            if list(meta["area"]) != list(self.area):
                print("깨끗한 책상 사진의 DESK_AREA가 달라서 새로 찍어요")
                return
            self.base, self.known = img, [tuple(b) for b in meta["known"]]
            labels = meta.get("labels") or []  # 이름을 안 적던 옛 파일이면 이름 없이
            self.known_labels = list(labels) if len(labels) == len(self.known) else []
            print("저장된 깨끗한 책상 사진 불러옴")
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"깨끗한 책상 사진을 못 읽어서 새로 찍어요 ({self.path}: {e})")

    def look(self, frame):
        """책상 부분만 흑백·작게 줄인 사진 (저장하지 않음). 나중에 set_prepared로 기준으로 삼을 수 있다."""
        return prepare(frame, self.area)[0]

    def set_base(self, frame, known_dets=()):
        """사람 0명인 깨끗한 방에서만 부른다."""
        known_dets = list(known_dets)
        self.set_prepared(self.look(frame), [d.box for d in known_dets], [d.label for d in known_dets])

    def set_prepared(self, img, known_boxes=(), known_labels=()):
        """look으로 미리 줄여 둔 사진을 기준으로 삼고 파일에 적는다 (사람 0명일 때 찍은 것만)."""
        self.base = img
        self.known = [tuple(float(v) for v in b) for b in known_boxes]
        self.known_labels = list(known_labels)
        if not self.path:
            return
        try:
            import cv2
            tmp = self.path + ".tmp.png"
            if not cv2.imwrite(tmp, self.base):
                raise OSError("저장 실패")
            os.replace(tmp, self.path)
            # 사진 지문을 같이 적어 둔다. 둘 중 하나만 바뀐 채로 꺼지면 load가 짝이 안 맞는 걸 알고 버린다
            meta_tmp = self.path + ".json.tmp"
            with open(meta_tmp, "w", encoding="utf-8") as f:
                json.dump({"area": list(self.area), "known": self.known, "labels": self.known_labels,
                           "sum": fingerprint(self.base)}, f)
            os.replace(meta_tmp, self.path + ".json")
        except OSError as e:
            print(f"깨끗한 책상 사진을 못 썼어요 ({self.path}: {e}). 다시 켜면 새로 찍어요")

    def clear(self):
        """기준 사진을 메모리와 파일에서 지운다 (예약 기준은 정리 확인이 끝나면 지운다)."""
        self.base, self.known, self.known_labels, self.moved = None, [], [], []
        for p in (self.path, self.path and self.path + ".json"):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError as e:
                    print(f"기준 사진 파일을 못 지웠어요 ({p}: {e})")

    def changes(self, frame):
        """달라진 곳을 원래 화면 좌표 상자로. 기준이 없거나 조명이 통째로 바뀌었으면 None."""
        if self.base is None:
            return None
        now, offset = prepare(frame, self.area)
        moved = []
        found = find_changes(self.base, now, moved=moved, **self.tune)
        self.moved = [to_frame(b, offset) for b in moved]
        if found is None:
            return None
        return [to_frame(b, offset) for b in found]


def settings(config):
    """게이트웨이 설정(DESK_THRESH, DESK_MIN_AREA, DESK_LIGHT, DESK_MOVE, DESK_MOVE_SAME, DESK_KEEP_SMALL,
    DESK_FURNITURE_SMALL) → DeskCompare에 넘길 값."""
    return {"thresh": config.desk_thresh, "min_area": config.desk_min_area, "light": config.desk_light,
            "move": config.desk_move, "move_same": config.desk_move_same,
            "keep_small": getattr(config, "desk_keep_small", 0.0),
            "furniture_small": getattr(config, "desk_furniture_small", 0.0)}
