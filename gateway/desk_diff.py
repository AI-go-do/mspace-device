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
LIGHT_MODES = ("mean", "local", "ssim", "dino")
DINO_MODEL = "gateway/dinov2_small.onnx"  # DESK_LIGHT=dino: DINOv2-small 특징 모델 (DESK_DINO_MODEL로 바꿈)
DINO_SIZE = (518, 448)  # 모델에 넣는 책상 크기 (폭, 높이). 14의 배수, 내보낼 때와 같아야 함 → 37×32칸
DINO_MEAN = (0.485, 0.456, 0.406)
DINO_STD = (0.229, 0.224, 0.225)
_dino_sessions = {}
GONE_RATIO = 1.5     # DESK_GONE=1: 기준 사진 쪽 가장자리가 지금보다 이만큼 더 많으면 "치운 자리"로 보고 뺀다


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


def dino_session(path):
    """DINOv2 ONNX 모델을 한 번만 불러 둔다 (젯슨 GPU가 되면 GPU, 아니면 CPU)."""
    if path not in _dino_sessions:
        if not os.path.exists(path):
            raise FileNotFoundError(f"DINOv2 모델 파일이 없어요: {path} (README 실험 기록의 내보내기 방법 참고)")
        import onnxruntime as ort
        providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
        _dino_sessions[path] = ort.InferenceSession(path, providers=providers)
    return _dino_sessions[path]


def dino_features(img, path):
    """책상 사진(흑백) → 14×14칸마다 "무엇이 있나" 특징 (길이 1로 맞춘 벡터). 모양 (32, 37, 384)."""
    import cv2
    import numpy as np
    w, h = DINO_SIZE
    x = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    x = np.repeat(x[None, None], 3, axis=1)
    x = (x - np.array(DINO_MEAN, np.float32)[None, :, None, None]) / np.array(DINO_STD, np.float32)[None, :, None, None]
    session = dino_session(path)
    feats = session.run(None, {session.get_inputs()[0].name: x})[0][0]
    feats = feats / (np.linalg.norm(feats, axis=-1, keepdims=True) + 1e-6)
    return feats.reshape(h // 14, w // 14, -1)


def dino_diff(base, now, path=DINO_MODEL):
    """자리마다 "무엇이 있나"가 얼마나 달라졌나를 0~255로 (DINOv2 특징의 코사인 거리).

    픽셀 밝기 대신 AI 특징을 비교한다 (ICRA 2025 "Robust Scene Change Detection Using Visual Foundation Models",
    CVPR 2025 GeSCF와 같은 방향). 불을 끄거나 그림자가 져도 같은 책상이면 특징이 비슷해서 덜 예민하다.
    """
    import cv2
    import numpy as np
    a, b = dino_features(base, path), dino_features(now, path)
    dist = np.clip((1.0 - (a * b).sum(-1)) * 255.0, 0, 255).astype(np.float32)
    return cv2.resize(dist, (base.shape[1], base.shape[0]), interpolation=cv2.INTER_LINEAR).astype(np.uint8)


def edge_energy(img):
    """가장자리(무늬)가 얼마나 많은지 (소벨 크기 평균). 맨 책상은 작고, 물건이 있으면 크다."""
    import cv2
    import numpy as np
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(img, cv2.CV_32F, 0, 1, ksize=3)
    return float(np.mean(np.abs(gx) + np.abs(gy))) if img.size else 0.0


def is_gone(base, now, box, ratio=GONE_RATIO):
    """달라진 곳이 "물건이 생김"이 아니라 "물건을 치움"인가.

    버려진/치워진 물건 구분 (Tian·Feris·Hampapur 2008 등 감시 카메라 연구의 방법): 그 자리에 무늬가 기준 사진 쪽에
    더 많으면 원래 있던 것이 없어진 것이다. 기준보다 지금 무늬가 많아야 "새로 남긴 물건"으로 본다.
    """
    x, y, w, h = box
    before, after = edge_energy(base[y:y + h, x:x + w]), edge_energy(now[y:y + h, x:x + w])
    return before > ratio * max(after, 1e-6)


def match_contrast(base, now):
    """지금 사진의 밝기와 대비(평균·표준편차)를 기준 사진에 맞춘다. 불을 꺼서 어둡고 흐릿해진 사진도
    기준과 같은 밝기로 펴서 비교한다 (자리마다 펴는 CLAHE는 잡음까지 키워서 전체로만 맞춘다)."""
    import numpy as np
    n = now.astype(np.float32)
    spread = float(n.std()) or 1.0
    return np.clip((n - float(n.mean())) / spread * float(base.std()) + float(base.mean()), 0, 255).astype(np.uint8)


def find_changes(base, now, thresh=DIFF_THRESH, min_area=MIN_AREA, max_area=MAX_AREA, light="mean",
                 gone=False, eq=False, model=DINO_MODEL):
    """두 책상 사진(prepare 결과)에서 달라진 곳 상자 (x, y, w, h) 목록. 조명이 통째로 바뀌었으면 None.

    light="mean": 사진 전체 밝기만 맞춘다 (원래 방식). "local": 자리마다 밝기를 고르게 한 뒤 비교한다.
    "ssim": 밝기 차이 대신 자리마다 모양(무늬)이 달라졌는지 본다 (빛·그림자에 덜 예민, 조금 느림).
    "dino": DINOv2 특징으로 "무엇이 있나"가 달라졌는지 본다 (model = ONNX 파일, 실험).
    gone=True: 물건을 치운 자리는 뺀다 (DESK_GONE=1). eq=True: 지금 사진의 밝기·대비를 기준에 먼저 맞춘다 (DESK_EQ=1).
    """
    import cv2
    import numpy as np
    if base.shape != now.shape:
        return None
    if eq:
        now = match_contrast(base, now)
    if light == "dino":
        diff = dino_diff(base, now, model)
    elif light == "ssim":
        diff = ssim_diff(base, now)
    elif light == "local":
        diff = cv2.absdiff(flatten_light(base), flatten_light(now))
    else:
        diff = cv2.absdiff(base, match_light(base, now))
    _, mask = cv2.threshold(diff, thresh, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))       # 점 잡음 지우기
    mask = cv2.dilate(mask, np.ones((9, 9), np.uint8), iterations=2)                # 한 물건 조각 잇기
    total = base.shape[0] * base.shape[1]
    if cv2.countNonZero(mask) > max_area * total:
        return None
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = join_near([tuple(int(v) for v in cv2.boundingRect(c)) for c in contours])
    boxes = [b for b in boxes if b[2] * b[3] >= min_area * total]
    if gone:
        boxes = [b for b in boxes if not is_gone(base, now, b)]
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

    def __init__(self, area, path=None, thresh=DIFF_THRESH, min_area=MIN_AREA, light="mean", gone=False, eq=False,
                 model=DINO_MODEL):
        self.area = area
        # DESK_THRESH·DESK_MIN_AREA·DESK_LIGHT·DESK_GONE·DESK_EQ·DESK_DINO_MODEL
        self.tune = {"thresh": thresh, "min_area": min_area, "light": light, "gone": gone, "eq": eq, "model": model}
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
            if meta.get("sum") != fingerprint(img):
                raise ValueError("사진과 메타데이터 짝이 안 맞음")
            if list(meta["area"]) != list(self.area):
                print("깨끗한 책상 사진의 DESK_AREA가 달라서 새로 찍어요")
                return
            self.base, self.known = img, [tuple(b) for b in meta["known"]]
            print("저장된 깨끗한 책상 사진 불러옴")
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"깨끗한 책상 사진을 못 읽어서 새로 찍어요 ({self.path}: {e})")

    def look(self, frame):
        """책상 부분만 흑백·작게 줄인 사진 (저장하지 않음). 나중에 set_prepared로 기준으로 삼을 수 있다."""
        return prepare(frame, self.area)[0]

    def set_base(self, frame, known_dets=()):
        """사람 0명인 깨끗한 방에서만 부른다."""
        self.set_prepared(self.look(frame), [d.box for d in known_dets])

    def set_prepared(self, img, known_boxes=()):
        """look으로 미리 줄여 둔 사진을 기준으로 삼고 파일에 적는다 (사람 0명일 때 찍은 것만)."""
        self.base = img
        self.known = [tuple(float(v) for v in b) for b in known_boxes]
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
                json.dump({"area": list(self.area), "known": self.known, "sum": fingerprint(self.base)}, f)
            os.replace(meta_tmp, self.path + ".json")
        except OSError as e:
            print(f"깨끗한 책상 사진을 못 썼어요 ({self.path}: {e}). 다시 켜면 새로 찍어요")

    def clear(self):
        """기준 사진을 메모리와 파일에서 지운다 (예약 기준은 정리 확인이 끝나면 지운다)."""
        self.base, self.known = None, []
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
        found = find_changes(self.base, now, **self.tune)
        if found is None:
            return None
        return [to_frame(b, offset) for b in found]


def settings(config):
    """게이트웨이 설정(DESK_THRESH, DESK_MIN_AREA, DESK_LIGHT, DESK_GONE, DESK_EQ) → DeskCompare에 넘길 값."""
    return {"thresh": config.desk_thresh, "min_area": config.desk_min_area, "light": config.desk_light,
            "gone": config.desk_gone, "eq": config.desk_eq, "model": config.desk_dino_model}
