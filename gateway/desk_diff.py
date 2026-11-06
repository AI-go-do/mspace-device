"""깨끗한 책상 사진과 지금 책상을 비교해서 달라진 곳(남은 물건 자리)을 찾는다 (OpenCV).

모델(80종)이 모르는 물건(종이, 보조배터리, 과자 봉지 …)도 "책상 위에 뭔가 남아 있음"으로 잡으려고 쓴다.
달라진 곳에는 room_camera가 모델을 한 번 더 돌려 이름을 붙이고, 안 붙으면 "모르는 물건"으로 둔다.

개인정보
  DESK_PHOTO=1 일 때만 켜진다(기본 꺼짐). 사람이 0명일 때 찍은 깨끗한 책상 사진 한 장만 젯슨 안에 둔다
  (DESK_AREA 부분만, 흑백·작게). 서버로 보내지 않는다. 퇴실 뒤 사진은 비교하고 바로 버린다.
"""
from __future__ import annotations

import json
import os

WORK_WIDTH = 640     # 비교는 책상을 이 폭으로 줄여서 한다 (빠르고 잔잡음이 줄어듦)
DIFF_THRESH = 35     # 밝기 차이가 이보다 크면 "달라짐" (0~255)
MIN_AREA = 0.0012    # 책상 넓이의 이 비율보다 작은 차이는 무시 (먼지·잡음). 640폭에서 약 22×22
MAX_AREA = 0.35      # 이보다 크면 물건이 아니라 조명·카메라가 바뀐 것으로 본다


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


def find_changes(base, now, thresh=DIFF_THRESH, min_area=MIN_AREA, max_area=MAX_AREA):
    """두 책상 사진(prepare 결과)에서 달라진 곳 상자 (x, y, w, h) 목록. 조명이 통째로 바뀌었으면 None."""
    import cv2
    import numpy as np
    if base.shape != now.shape:
        return None
    diff = cv2.absdiff(base, match_light(base, now))
    _, mask = cv2.threshold(diff, thresh, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))       # 점 잡음 지우기
    mask = cv2.dilate(mask, np.ones((9, 9), np.uint8), iterations=2)                # 한 물건 조각 잇기
    total = base.shape[0] * base.shape[1]
    if cv2.countNonZero(mask) > max_area * total:
        return None
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = join_near([tuple(int(v) for v in cv2.boundingRect(c)) for c in contours])
    return [b for b in boxes if b[2] * b[3] >= min_area * total]


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


class DeskCompare:
    """깨끗한 책상 사진 한 장을 들고 있다가 퇴실 뒤 책상과 비교한다."""

    def __init__(self, area, path=None):
        self.area = area
        self.path = path              # 껐다 켜도 남게 저장할 파일 (.png). None이면 메모리에만
        self.base = None              # 깨끗한 책상 (prepare 결과)
        self.known = []               # 기준 때 모델이 찾은 물건 자리 (리모컨을 옮기면 빈 자리도 "달라짐"이라 뺀다)
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
            if list(meta["area"]) != list(self.area):
                print("깨끗한 책상 사진의 DESK_AREA가 달라서 새로 찍어요")
                return
            self.base, self.known = img, [tuple(b) for b in meta["known"]]
            print("저장된 깨끗한 책상 사진 불러옴")
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"깨끗한 책상 사진을 못 읽어서 새로 찍어요 ({self.path}: {e})")

    def set_base(self, frame, known_dets=()):
        """사람 0명인 깨끗한 방에서만 부른다."""
        self.base, _ = prepare(frame, self.area)
        self.known = [tuple(float(v) for v in d.box) for d in known_dets]
        if not self.path:
            return
        try:
            import cv2
            tmp = self.path + ".tmp.png"
            if not cv2.imwrite(tmp, self.base):
                raise OSError("저장 실패")
            os.replace(tmp, self.path)
            with open(self.path + ".json", "w", encoding="utf-8") as f:
                json.dump({"area": list(self.area), "known": self.known}, f)
        except OSError as e:
            print(f"깨끗한 책상 사진을 못 썼어요 ({self.path}: {e}). 다시 켜면 새로 찍어요")

    def changes(self, frame):
        """달라진 곳을 원래 화면 좌표 상자로. 기준이 없거나 조명이 통째로 바뀌었으면 None."""
        if self.base is None:
            return None
        now, offset = prepare(frame, self.area)
        found = find_changes(self.base, now)
        if found is None:
            return None
        return [to_frame(b, offset) for b in found]
