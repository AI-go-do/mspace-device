"""방 안 웹캠으로 인원 세기 + 퇴실 뒤 정리 확인 (젯슨)

8차 회의(11/6) 멘토 의견: "받은 카메라로 몇 명인지 세 보라", "방이 깨끗한지(관리자 관점)".

하는 일
  1. 인원 세기: 1초마다 사람 수를 세서, 바뀌면 바로 + 바뀌지 않아도 10초마다 core에 보낸다.
     지금 core 재실 API(POST /api/sensor-events, kind OCCUPANCY)를 그대로 쓴다.
       occupied = 사람 1명 이상 (또는 우노 PIR이 있음)  ← "하나라도 있음이면 있음" (FR-DEV-103)
       sources  = {"camera": true, "people": 2, "pir": ...}  ← core는 sources를 그대로 기록만 한다
     그래서 core를 바꾸지 않고도 "몇 명"이 서버에 남는다.
  2. 정리 확인: 서버가 "예약이 끝났다"고 하면(퇴실 모드) 또는 연습 모드에서 모두 나가고 30초가 지나면,
     빈 방에서 물건(컵·병·노트북·가방 …)을 찾는다. 퇴실 모드 결과는 core POST /api/rooms/{id}/tidy-checks로 보낸다.
     퇴실 모드는 "사람이 처음 보이기 직전 빈 방"(한 장씩 0명이 연속 3초)을 그 예약의 기준으로 잡고(Q-22 B안),
     퇴실 뒤 빈 방과 비교한다. 그래서 앞 팀이 두고 간 것을 다음 팀 탓으로 하지 않는다. 화장실 등으로 잠깐 비었다
     다시 들어오면 처음 기준을 그대로 둔다. 방이 한 번도 안 비어 기준이 없으면 결과를 보내지 않는다(어질러짐 신고가 받음).
     연습 모드는 처음 빈 방(깨끗한 상태)에서 본 물건과 비교한다.
     기준과 종류별 개수로 비교해서 "늘어난 물건"만 남은 물건으로 본다.
     자리는 보지 않는다 (리모컨을 조금 옮겨도 잘못 잡지 않게). 방 비품(ROOM_ITEMS, 예: remote:1)은
     그 개수까지 있어도 없어도 괜찮다고 본다 (바구니에 넣어 안 보이거나 책상에 나와 있어도 됨).
     기준을 저장하면 "깨끗한 방 기준 저장"을 알리고, 기준이 없으면 결과 대신 지금 모습을 기준으로 저장한다.
     기준·정리 확인 때만 화면을 4조각으로 나눠 자세히 본다(멀리 있는 작은 물건). 평소 인원 세기는 한 장 그대로.
     결과는 화면과 room_checks.jsonl(글자만)에도 남긴다.

  3. (선택) 책상 확대 DESK_AREA: 정리 확인 때 책상만 잘라 한 번 더 본다 (위에서 찍으면 물건이 작아서).
  4. (선택) 책상 사진 비교 DESK_PHOTO=1: 깨끗한 책상 사진과 달라진 곳을 찾아 모델이 모르는 물건도 "모르는 물건"으로 잡는다
     (desk_diff.py). 달라진 곳에는 모델(또는 YOLO-World LABEL_MODEL)로 이름을 붙인다.

개인정보
  사진·영상은 저장하지도, 보내지도 않는다. 프레임은 메모리에서 세고 바로 버린다. 남는 건 숫자와 물건 이름뿐.
  DESK_PHOTO=1을 켰을 때만 사람 0명일 때 찍은 책상 사진(책상 부분, 흑백·작게)을 젯슨 안에 둔다. 서버로는 안 보낸다.
  퇴실 모드의 예약 기준 사진(입실 직전 빈 방 1장)은 그 예약 정리 확인이 끝나면 지운다 (docs v0.4.26, Q-22).

모델
  YOLO_MODEL=yolov8n.onnx 처럼 ONNX 모델 파일을 주면 사람 + 물건 80종을 찾는다 (OpenCV DNN, 젯슨 기본 설치).
  모델이 없으면 OpenCV 기본 사람 찾기(HOG)로 사람만 센다. 정확도가 낮고, 정리 확인은 꺼진다.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from desk_diff import desk_box, fingerprint

KST = timezone(timedelta(hours=9))

# YOLO(COCO) 80종 이름. 모델 출력 번호 순서와 같아야 한다
COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
    "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]

# 정리 확인에서 볼 물건. 쓰레기(먹고 남긴 것)와 두고 간 물건으로 나눠서 알려 준다
TRASH = {"bottle", "cup", "wine glass", "bowl", "fork", "knife", "spoon", "banana", "apple", "sandwich",
         "orange", "pizza", "donut", "cake", "hot dog"}
BELONGINGS = {"backpack", "handbag", "suitcase", "umbrella", "laptop", "cell phone", "book", "mouse",
              "keyboard", "remote"}
# 이름을 직접 정하는 모델(YOLO-World, LABEL_MODEL)이 낼 수 있는 이름. 80종 밖이라 위 두 묶음에 더한다
TRASH |= {"paper", "can", "snack bag", "plastic bag", "tissue", "food container"}
BELONGINGS |= {"wallet", "credit card", "power bank", "earphone case", "tumbler", "pencil case", "glasses",
               "remote control"}
# 방에 원래 있는 가구·시설. 책상 사진 비교로 달라진 곳에 이 이름이 붙으면 의자를 빼 둔 것 같은 자리 바뀜이라 세지 않는다
FURNITURE = {"chair", "couch", "bench", "bed", "dining table", "tv", "potted plant", "clock", "refrigerator",
             "sink", "toilet", "oven", "microwave"}
UNKNOWN = "unknown"  # 책상 사진 비교로 달라졌는데 이름을 못 붙인 것 → 치울 것(확인 필요)으로 본다
KOREAN = {"bottle": "병", "cup": "컵", "wine glass": "잔", "bowl": "그릇", "fork": "포크", "knife": "칼",
          "spoon": "숟가락", "banana": "바나나", "apple": "사과", "sandwich": "샌드위치", "orange": "귤",
          "pizza": "피자", "donut": "도넛", "cake": "케이크", "hot dog": "핫도그", "backpack": "가방",
          "handbag": "손가방", "suitcase": "캐리어", "umbrella": "우산", "laptop": "노트북",
          "cell phone": "휴대폰", "book": "책", "mouse": "마우스", "keyboard": "키보드", "remote": "리모컨",
          "paper": "종이", "can": "캔", "snack bag": "과자 봉지", "plastic bag": "비닐봉지", "tissue": "휴지",
          "food container": "음식 용기", "wallet": "지갑", "credit card": "카드", "power bank": "보조배터리",
          "earphone case": "이어폰 케이스", "tumbler": "텀블러", "pencil case": "필통", "glasses": "안경",
          "remote control": "리모컨", "unknown": "모르는 물건"}

BEAT_SEC = 10          # 바뀌지 않아도 10초마다 보낸다 (core는 1분 안 오면 끊김)
SMOOTH_FRAMES = 5      # 최근 5번 센 값의 가운데 값을 쓴다 (한 번 놓치거나 잘못 세서 숫자가 튀지 않게)
EMPTY_CHECK_SEC = 30   # (연습 모드) 모두 나가고 30초 뒤에 정리 확인 (문 닫고 나가는 시간)
SETTLE_SEC = 10        # (퇴실 모드) 퇴실 뒤 방이 10초 비어 있으면 확인 (나가는 사람이 찍히지 않게)
PENDING_MAX_SEC = 600  # (퇴실 모드) 퇴실 뒤 10분 안에 방이 비지 않으면 그 예약 확인은 포기 (다음 사람 물건을 탓하지 않게)
ENTRY_EMPTY_SEC = 3    # (퇴실 모드) 한 장씩 셌을 때 0명이 연속 3초면 "빈 방"으로 보고 다음 예약 기준 후보로 든다 (Q-22)
HELD_SNAPSHOT_SEC = 15  # (퇴실 모드) 사람이 잠깐 보여 못 쓴 빈 방 후보를 이만큼 들고 있다가, 바로 뒤 QR 입실이면 그것으로 기준을 잡는다
SNAPSHOT_SEC = 60      # (퇴실 모드) 빈 방이 계속되면 후보를 1분마다 새로 찍는다 (햇빛·조명 변화 따라감)
DESK_DIFF_CONF = 0.5   # 책상 사진 비교로 찾은 물건의 확신도. 모델 점수가 없어서 core 경고 기준(기본 0.8)보다 낮게 둔다
                       # → 알림은 가도 이것만으로 경고는 안 간다 (잘못 본 그림자·빛으로 경고하지 않게)
PHOTO_MAX_SIDE = 1280  # 정리 확인 사진(TIDY_PHOTOS=1)은 긴 변을 이만큼으로 줄여 올린다
PHOTO_QUALITY = 80     # JPEG 품질. 1280x720이면 보통 100~300KB
PHOTO_MAX_BYTES = 1_000_000  # core 한도는 2MB지만 중간 프록시(nginx 기본 1MB)에 막히지 않게 1MB 아래로
RETRIEVE_RECHECK_SEC = 600  # 찾으러 온 QR(core retrieve-pass, 10분)로 들어온 뒤 이만큼 지나 빈 방이면 다시 찍는다 (RETRIEVE_RECHECK_MIN)
ENTRY_PIR_GRACE_SEC = 10  # PIR이 먼저 켜진 뒤 이 안에 카메라가 사람을 보면 보통 입실. 더 길면 카메라에 안 보인 사람이
                          # 그동안 뭘 두었을 수 있어 후보를 쓰지 않는다


@dataclass(frozen=True)
class Detection:
    label: str
    score: float
    box: tuple  # (x, y, w, h) 픽셀


# ---------- 모델 출력 풀기 (OpenCV 없이 테스트할 수 있게 떼어 둠) ----------

def iou(a, b):
    """두 상자가 얼마나 겹치는지 (0~1)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    h = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = w * h
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


PERSON_NMS = 0.6  # 사람끼리는 붙어 앉아 많이 겹치므로 더 많이 겹쳐야 같은 사람으로 본다
SAME_SPOT = 0.8   # 이름이 다른 물건 두 상자가 이만큼 겹치면 같은 물건에 이름이 두 개 붙은 것


def same_thing(a, b, threshold, person_threshold, same_spot):
    """b(점수 높은 쪽)가 있으면 a는 버려도 되는지."""
    if a.label == b.label:
        return iou(a.box, b.box) >= (person_threshold if a.label == "person" else threshold)
    # 물건끼리 거의 같은 자리·크기면 한 물건이다 (11/7 방 사진: 리모컨을 "리모컨"과 "휴대폰"으로 동시에 봄).
    # 사람은 손에 든 물건과 상자가 겹칠 수 있어서 이름이 다르면 따로 둔다.
    return "person" not in (a.label, b.label) and iou(a.box, b.box) >= same_spot


def nms(dets, threshold=0.45, person_threshold=PERSON_NMS, same_spot=SAME_SPOT):
    """같은 물건을 여러 상자로 잡은 걸 하나로 줄인다 (점수 높은 것만 남김).

    사람은 옆 사람과 상자가 많이 겹쳐도 다른 사람일 수 있어서 기준을 더 느슨하게 둔다.
    물건 하나에 이름이 두 개 붙으면(거의 같은 상자) 점수 높은 이름만 남긴다.
    """
    kept = []
    for d in sorted(dets, key=lambda d: d.score, reverse=True):
        if not any(same_thing(d, k, threshold, person_threshold, same_spot) for k in kept):
            kept.append(d)
    return kept


PERSON_CONF = 0.4   # 사람은 이 점수 넘어야 센다 (잘못 세면 재실이 틀림)
OBJECT_CONF = 0.3   # 물건은 조금 낮춰 작은 물건도 잡는다. 늘 있는 엉뚱한 것은 빈 방 기준과 비교할 때 빠짐


class LabelCountMismatch(ValueError):
    """모델이 내는 종류 수와 이름표(names.json) 수가 다름."""


def parse_yolo(output, scale_x, scale_y, conf=OBJECT_CONF, person_conf=PERSON_CONF, names=None):
    """YOLOv8 ONNX 출력 (1, 84, N)을 Detection 목록으로 바꾼다.

    84 = 상자 4개 값(가운데 x, 가운데 y, 너비, 높이) + 80종 점수. YOLO-World처럼 종류를 직접 정한 모델은 names로. scale은 모델 입력(640) → 원래 화면 크기 비율.
    점수 기준은 사람(person_conf)과 물건(conf)을 따로 둔다.
    """
    import numpy as np
    rows = np.asarray(output)[0].T  # (N, 84)
    scores = rows[:, 4:]
    labels = names or COCO_NAMES
    if scores.shape[1] != len(labels):  # 이름표가 없거나 다른 모델 것: 엉뚱한 이름(power bank → train)을 내지 않게
        raise LabelCountMismatch(f"모델 종류 수({scores.shape[1]})와 이름 수({len(labels)})가 달라요")
    best = scores.argmax(axis=1)
    best_score = scores[np.arange(len(rows)), best]
    dets = []
    for i in np.nonzero(best_score >= min(conf, person_conf))[0]:
        label = labels[int(best[i])]
        if best_score[i] < (person_conf if label == "person" else conf):
            continue
        cx, cy, w, h = rows[i, :4]
        box = (float((cx - w / 2) * scale_x), float((cy - h / 2) * scale_y), float(w * scale_x), float(h * scale_y))
        dets.append(Detection(label, float(best_score[i]), box))
    return nms(dets)


TILE_FRAC = 0.55  # 4조각 하나의 크기 (화면 가로·세로의 55%, 가운데가 조금씩 겹침)
TILE_GRID = 2     # 정리 확인 때 화면을 가로·세로 몇 줄로 자르나 (TILE_GRID). 2 = 4조각(지금까지), 3 = 9조각
TILE_OVERLAP = 1.1  # 3줄 이상일 때 조각 하나 = 화면의 1/줄 수 × 1.1 (옆 조각과 조금씩 겹침)


def tile_boxes(width, height, frac=TILE_FRAC, grid=2):
    """화면을 grid × grid 조각으로 나눈 (x, y, w, h). 옆 조각과 겹쳐서 경계에 걸친 물건도 어느 한 조각에는 통째로 들어간다.

    grid 2는 지금까지와 같은 네 귀퉁이 조각(각 55%). 3 이상은 조각 하나가 1/grid × TILE_OVERLAP.
    """
    if grid > 2:
        frac = min(1.0, TILE_OVERLAP / grid)
    tw, th = int(width * frac), int(height * frac)
    xs = [round(i * (width - tw) / (grid - 1)) for i in range(grid)]
    ys = [round(j * (height - th) / (grid - 1)) for j in range(grid)]
    return [(x, y, tw, th) for y in ys for x in xs]


def detect_tiled(detect, frame, grid=2):
    """한 장 전체 + grid × grid 조각을 각각 찾아서 합친다. 조각은 덜 줄어들어서 멀리 있는 작은 물건이 보인다.

    퇴실 뒤 정리 확인처럼 가끔 한 번만 쓴다 (4조각이면 한 장보다 5배, 9조각이면 10배 느림).
    """
    h, w = frame.shape[:2]
    whole = list(detect(frame))
    extra = []
    for x, y, tw, th in tile_boxes(w, h, grid=grid):
        if tw < 2 or th < 2:
            continue  # 아주 작은 부분(DESK_AREA·FLOOR_AREA가 몇 픽셀)이면 조각이 비어서 모델이 오류를 낸다
        for d in detect(frame[y:y + th, x:x + tw]):
            bx, by, bw, bh = d.box
            if cut_by_tile(d.box, x, y, tw, th, w, h):
                continue  # 조각 경계에 잘린 큰 물건(반쪽 노트북 등)은 한 장 전체 결과를 믿는다
            box = (bx + x, by + y, bw, bh)
            if any(o.label == d.label and inside(box, o.box) for o in whole + extra):
                continue  # 이미 찾은 같은 물건
            extra.append(Detection(d.label, d.score, box))
    return nms(whole + extra)


def detect_desk(detect, frame, area):
    """책상 부분만 잘라 (책상 전체 + 책상 4조각) 찾는다. 책상 위 작은 물건이 몇 배 크게 보인다."""
    h, w = frame.shape[:2]
    x, y, dw, dh = desk_box(w, h, area)
    found = detect_tiled(detect, frame[y:y + dh, x:x + dw])
    return [Detection(d.label, d.score, (d.box[0] + x, d.box[1] + y, d.box[2], d.box[3])) for d in found]


def merge(base, extra):
    """두 결과를 합친다. 이미 찾은 같은 물건(같은 이름, 절반 넘게 겹침)은 한 번만 센다."""
    kept = list(base)
    for d in extra:
        if not any(k.label == d.label and inside(d.box, k.box) for k in kept):
            kept.append(d)
    return nms(kept)


def cut_by_tile(box, x, y, tw, th, width, height, margin=3):
    """조각 안 상자가 화면 가장자리가 아닌 조각 경계에 닿았는지 (물건이 잘렸다는 뜻)."""
    bx, by, bw, bh = box
    return ((bx <= margin and x > 0) or (by <= margin and y > 0)
            or (bx + bw >= tw - margin and x + tw < width) or (by + bh >= th - margin and y + th < height))


def similar_size(a, b, ratio=2.5):
    """두 상자 넓이가 ratio배 안으로 비슷한지."""
    small, big = sorted((a[2] * a[3], b[2] * b[3]))
    return small > 0 and big <= ratio * small


def overlaps(a, b, ratio=0.3):
    """두 상자가 작은 쪽 넓이의 ratio 넘게 겹치는지."""
    return inside(a, b, ratio)


def inside(a, b, ratio=0.5):
    """a 상자가 b 상자 안에 절반 넘게 들어가 있는지."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    small = min(aw * ah, bw * bh)
    return small > 0 and ix * iy / small >= ratio


def load_names(model_path):
    """모델 옆에 이름표(yolov8s-worldv2.names.json = ["power bank", ...])가 있으면 읽는다. 없으면 80종(COCO)."""
    path = os.path.splitext(model_path)[0] + ".names.json"
    if not os.path.exists(path):
        return COCO_NAMES
    with open(path, encoding="utf-8") as f:
        return [str(n) for n in json.load(f)]


# ---------- 찾는 기계 두 가지 (같은 detect(frame) 모양) ----------

class YoloDetector:
    """YOLOv8 ONNX 모델로 사람 + 물건 80종을 찾는다.

    onnxruntime이 있으면 그걸로 돌린다(젯슨 기본 OpenCV 4.6은 YOLOv8 모델을 못 읽음).
    없으면 OpenCV DNN(4.7 이상)으로 돌린다. 앞처리(크기 맞추기)는 둘 다 OpenCV로 한다.
    """
    finds_objects = True

    def __init__(self, model_path, size=640, desk_area=None, floor_area=None, tile_grid=TILE_GRID):
        import cv2
        self.cv2 = cv2
        self.size = size
        self.tile_grid = tile_grid  # 정리 확인 때 화면 전체를 몇 줄로 자르나 (책상 확대는 늘 4조각)
        self.desk_area = desk_area  # (x1, y1, x2, y2) 비율. 정리 확인 때 책상만 잘라 한 번 더 본다
        self.floor_area = floor_area  # 같은 모양. 바닥(가방·에어팟 케이스가 떨어지는 곳)도 잘라 한 번 더 본다
        self.names = load_names(model_path)
        self.session = self.net = None
        try:
            import onnxruntime as ort
            providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
                         if p in ort.get_available_providers()]
            opts = ort.SessionOptions()
            # 젯슨 CPU를 YOLO가 다 쓰면 VLM이 3배 느려져 90초 안에 답을 못 한다 (11/12 #43: CPU 477%, VLM TimeoutError)
            opts.intra_op_num_threads = yolo_threads()
            opts.inter_op_num_threads = 1
            self.session = ort.InferenceSession(model_path, sess_options=opts, providers=providers)
            self.input_name = self.session.get_inputs()[0].name
            print(f"인원 세기: onnxruntime {providers[0]} (CPU 스레드 {opts.intra_op_num_threads}개, YOLO_THREADS)")
        except ImportError:
            self.net = cv2.dnn.readNetFromONNX(model_path)
            try:  # 젯슨 GPU(CUDA)를 쓸 수 있으면 쓴다. 안 되면 CPU
                self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
                self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
            except Exception:  # noqa: BLE001 - OpenCV 빌드마다 다름
                pass
            print("인원 세기: OpenCV DNN", cv2.__version__)

    def detect(self, frame):
        h, w = frame.shape[:2]
        blob = self.cv2.dnn.blobFromImage(frame, 1 / 255.0, (self.size, self.size), swapRB=True)
        if self.session is not None:
            output = self.session.run(None, {self.input_name: blob})[0]
        else:
            self.net.setInput(blob)
            output = self.net.forward()
        return parse_yolo(output, w / self.size, h / self.size, names=self.names)

    def detect_detailed(self, frame):
        """정리 확인·기준 저장 때만: 한 장 전체 + 조각(TILE_GRID), 그리고 DESK_AREA·FLOOR_AREA가 있으면 그 부분 확대까지 합친다."""
        found = detect_tiled(self.detect, frame, getattr(self, "tile_grid", TILE_GRID))
        for area in (self.desk_area, self.floor_area):
            if area:
                found = merge(found, detect_desk(self.detect, frame, area))
        return found


class HogPersonDetector:
    """모델 파일이 없을 때 쓰는 OpenCV 기본 사람 찾기. 서 있는 사람에 맞춰져 있어 앉은 사람은 놓칠 수 있다."""
    finds_objects = False

    def __init__(self):
        import cv2
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, frame):
        boxes, weights = self.hog.detectMultiScale(frame, winStride=(8, 8))
        return [Detection("person", float(s), tuple(float(v) for v in b)) for b, s in zip(boxes, weights)]


# ---------- 판단 (카메라·모델 없이 테스트할 수 있게 떼어 둠) ----------

def describe(items):
    """{"cup": 2, "laptop": 1} → "컵 2, 노트북 1"."""
    return ", ".join(f"{KOREAN.get(k, k)} {n}" for k, n in sorted(items.items()))


def photo_jpeg(frame):
    """정리 확인 사진(사람 0명일 때만 부름)을 JPEG 바이트로 줄인다. 못 만들면 None (결과 전송은 그대로)."""
    try:
        import cv2
        h, w = frame.shape[:2]
        scale = min(1.0, PHOTO_MAX_SIDE / max(h, w))
        if scale < 1.0:
            frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        for quality in (PHOTO_QUALITY, 60, 40):
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok and len(buf) <= PHOTO_MAX_BYTES:
                return buf.tobytes()
        print(f"정리 확인 사진이 {PHOTO_MAX_BYTES // 1000}KB보다 커서 올리지 않아요")
    except Exception as e:  # 사진 하나 못 만들어도 정리 확인은 계속
        print("정리 확인 사진을 못 만들었어요(결과는 그대로 보내요):", repr(e))
    return None


def photo_sum(jpg):
    return hashlib.sha256(jpg).hexdigest()[:16] if jpg else None


class RoomWatcher:
    """찾은 목록을 받아서 (1) core에 보낼 재실 이벤트 (2) 정리 확인 결과를 만든다."""

    def __init__(self, room_id, check_objects=True, now=None, room_items=None, on_checkout=False,
                 baseline_path=None, desk=None, labeler=None, entry_desk=None, photos=False, vlm=None,
                 retrieve_sec=RETRIEVE_RECHECK_SEC):
        self.room_id = room_id
        # 찾으러 온 뒤 다시 찍기 (11/12 재웅·지영): 두고 간 물건을 찾으러 10분 QR(purpose RETRIEVE)로 들어오면
        # 앞 예약 다시 확인을 취소하지 않고, QR 시각 + retrieve_sec 뒤 방이 비면 RECHECK를 찍어 보낸다
        self.retrieve_sec = retrieve_sec
        self.retrieve = None         # {"reservationId", "at"}: 찾으러 온 QR을 받았고 아직 다시 찍을 때가 안 됨
        self.retrieve_rid = None     # 찾으러 온 뒤 다시 확인을 요청한 예약 (recheck_pending에 들어감)
        self.door_events = deque()   # 문 QR 스레드가 넣는 (purpose, 시각). 카메라 스레드가 update에서 꺼내 처리
        # 입실 전 사진은 그 예약이 실제로 입실(QR·키패드)한 직전의 빈 방으로 (11/12 #39: 치우러 잠깐 들어간 것이 기준이 됨).
        # 퇴실 뒤 처음 문이 열리면(찾으러 온 QR 빼고) 다음에 사람이 보일 때 기준을 새로 굳힌다. 그 뒤 문 열림(화장실)은 그대로
        self.checkin_seen = False    # 마지막 퇴실 뒤 입실 문 열림을 이미 봤나
        self.rebase_entry = False    # 다음에 사람이 보이면 기준을 새로 굳힌다
        self.held_snapshot = None    # (후보, 시각): 사람이 보였지만 기준으로 안 쓴 빈 방 후보 (QR보다 사람이 먼저 잡힌 때 씀)
        # vlm (VLM_URL, 10판): 퇴실 뒤 첫 확인 때 입실 전·퇴실 뒤 사진을 VLM에게 한 번 더 묻는다 (vlm_check.VlmCheck).
        # 입실 전 사진은 VLM에 보낼 크기로만 메모리에 둔다 (파일로 안 씀, 젯슨이 꺼지면 YOLO 결과만 보냄)
        self.vlm = vlm
        # photos (TIDY_PHOTOS=1, docs Q-27): 퇴실 모드에서 입실 전(ENTRY)·퇴실 뒤(FIRST)·3시간 뒤(RECHECK) 빈 방 컬러 사진을
        # 결과에 붙여 core로 올린다. 사람이 0명인 장만 찍는다. 입실 전 사진은 정리 확인을 보내면 젯슨에서 지운다
        self.photos = photos
        # desk: 깨끗한 책상 사진 비교(desk_diff.DeskCompare, DESK_PHOTO=1). 모델이 모르는 물건도 "남음"으로 잡는다
        # labeler(frame, box): 달라진 곳에 이름 붙이기. 못 붙이면 None → "모르는 물건"
        self.desk = desk
        self.labeler = labeler
        self.baseline_path = baseline_path  # 기준을 파일에 적어 두면 껐다 켜도 남는다. None이면 메모리에만
        # on_checkout: 서버가 "예약이 끝났다"고 알려 줄 때만 확인한다 (request_check). 아니면 30초 빈 방 (연습 모드)
        self.on_checkout = on_checkout
        self.pending = None          # (예약 번호, 알게 된 시각, "FIRST"): 퇴실했는데 아직 확인 못 한 예약
        # 3시간 뒤 다시 확인 대기 (예약 번호, 알게 된 시각). pending과 따로 둔다: 지금 방에 있는 사람의
        # 예약 기준(entry)은 이것 때문에 지우거나 바꾸지 않는다
        self.recheck_pending = None
        # 첫 확인(FIRST)에서 물건이 남았던 예약의 기준 {"reservationId", "objects"}. 3시간 뒤 다시 확인(RECHECK) 때
        # 이것과 비교한다 (10차·core #113: 3시간 안에 안 가져가면 경고). 메모리에만: 젯슨이 꺼지면 다시 확인을 건너뛰고 경고도 없음
        self.room_items = dict(room_items or {})  # 방 비품: {"remote": 1} = 리모컨 0~1개는 괜찮음
        self.check_objects = check_objects
        self.counts = deque(maxlen=SMOOTH_FRAMES)
        self.sent_people = None      # 마지막으로 보낸 사람 수
        self.sent_occupied = None    # 마지막으로 보낸 재실 (사람 수 + PIR)
        self.sent_at = None          # 마지막으로 보낸 시각
        self.had_people = False      # 정리 확인 전에 사람이 있었는지
        self.empty_since = None      # 0명이 된 시각
        self.baseline = None         # 깨끗한 빈 방에서 본 물건 종류별 개수 (처음 빈 방 또는 깨끗한 정리 확인 뒤)
        # 예약 기준 (퇴실 모드, Q-22 B안): 사람이 처음 보이기 직전 빈 방. 퇴실 뒤 이것과 비교해 "그 예약 동안 생긴 것"만 본다
        self.entry_desk = entry_desk  # 예약 기준 책상 사진 (DESK_PHOTO일 때). 정리 확인이 끝나면 지운다
        self.entry_path = (os.path.splitext(baseline_path)[0] + "_entry.json") if baseline_path else None
        self.entry_photo_path = (os.path.splitext(baseline_path)[0] + "_entry.jpg") if baseline_path else None
        self.raw_empty_since = None  # 한 장씩 셌을 때 0명이 된 시각 (가운데 값이 아니라 그 장 그대로)
        self.snapshot = None         # 빈 방 후보 {"objects", "img", "known", "jpg", "at"}. 메모리에만
        self.entry = None            # 확정된 예약 기준 {"objects", "at", "jpg"(입실 전 사진, TIDY_PHOTOS일 때)}
        self.pir_since = None        # 후보를 찍은 뒤 카메라엔 0명인데 PIR이 처음 켜진 시각
        self.recheck_base = None
        self.load_baseline()
        self.load_entry()

    def load_baseline(self):
        """저장해 둔 기준 파일이 있으면 읽는다. 없거나 깨졌거나 다른 방 것이면 새로 찍는다."""
        if not self.baseline_path or not os.path.exists(self.baseline_path):
            return
        try:
            with open(self.baseline_path, encoding="utf-8") as f:
                saved = json.load(f)
            objects = saved["objects"]
            if saved.get("roomId") != self.room_id or not isinstance(objects, dict):
                print(f"기준 파일이 {self.room_id}번 방 것이 아니라 새로 찍어요: {self.baseline_path}")
                return
            self.baseline = {str(k): int(v) for k, v in objects.items()}
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"기준 파일을 못 읽어서 새로 찍어요 ({self.baseline_path}: {e})")
            return
        print(f"저장된 깨끗한 방 기준 불러옴 {self.room_id}번 방: 물건 {sum(self.baseline.values())}개"
              f"{' (' + describe(self.baseline) + ')' if self.baseline else ''} ({saved.get('savedAt', '시각 모름')})")

    def write_baseline(self, now_iso):
        """기준을 파일에 적는다. 임시 파일에 쓰고 바꿔치기해서, 쓰다가 꺼져도 파일이 깨지지 않게 한다."""
        if not self.baseline_path:
            return
        record = {"roomId": self.room_id, "objects": self.baseline,
                  "savedAt": now_iso or datetime.now(KST).isoformat(timespec="seconds")}
        tmp = self.baseline_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False)
            os.replace(tmp, self.baseline_path)
        except OSError as e:  # 못 써도 카메라는 계속 (메모리엔 있음)
            print(f"기준 파일을 못 썼어요 ({self.baseline_path}: {e}). 다시 켜면 기준을 새로 찍어요")

    def load_entry(self):
        """젯슨이 꺼졌다 켜져도 예약 기준이 이어지게 파일에서 읽는다. 없거나 깨졌으면 기준 없음."""
        if not self.entry_path or not os.path.exists(self.entry_path):
            if self.entry_desk is not None and self.entry_desk.ready:
                self.entry_desk.clear()  # 짝 없는 사진은 쓰지 않는다
            return
        try:
            with open(self.entry_path, encoding="utf-8") as f:
                saved = json.load(f)
            if (not isinstance(saved, dict) or saved.get("roomId") != self.room_id
                    or not isinstance(saved.get("objects"), dict)):
                raise ValueError("다른 방 기준이거나 모양이 다름")
            objects = {str(k): int(v) for k, v in saved["objects"].items()}
            desk_sum = (fingerprint(self.entry_desk.base)
                        if self.entry_desk is not None and self.entry_desk.ready else None)
            if self.entry_desk is not None and saved.get("deskSum") != desk_sum:
                # 사진을 바꾼 뒤 개수 파일을 쓰기 전에 꺼졌다. 옛 개수 + 새 사진을 섞어 쓰지 않는다
                raise ValueError("책상 사진과 개수가 같은 때 저장된 것이 아님")
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as e:
            print(f"예약 기준 파일을 못 읽어서 버려요 ({self.entry_path}: {e})")
            self.clear_entry()
            return
        self.entry = {"objects": objects, "at": float("-inf"),  # 켜기 전에 잡은 것 = 어떤 퇴실 알림보다 먼저
                      "jpg": self.load_entry_photo(saved.get("photoSum")),
                      "loaded": True}  # 다음에 사람이 들어오면 새로 잡는다 (11/12 #47: 옛 기준으로 노트북을 놓침)
        print(f"저장된 예약 기준 불러옴 {self.room_id}번 방 ({saved.get('savedAt', '시각 모름')}) · "
              "다음 입실 때 새로 잡아요")

    def load_entry_photo(self, want):
        """입실 전 사진을 읽는다. 개수 파일에 적힌 지문과 다르면(사진만 바뀐 채 꺼짐) 쓰지 않는다."""
        if not want or not self.entry_photo_path or not os.path.exists(self.entry_photo_path):
            return None
        try:
            with open(self.entry_photo_path, "rb") as f:
                jpg = f.read()
        except OSError as e:
            print(f"입실 전 사진을 못 읽어서 빼고 보내요 ({self.entry_photo_path}: {e})")
            return None
        return jpg if photo_sum(jpg) == want else None

    def write_entry_photo(self, jpg):
        """입실 전 사진을 젯슨에 둔다 (껐다 켜도 퇴실 뒤 같이 올리게). 없으면 옛 사진을 지운다."""
        if not self.entry_photo_path:
            return
        try:
            if jpg:
                with open(self.entry_photo_path + ".tmp", "wb") as f:
                    f.write(jpg)
                os.replace(self.entry_photo_path + ".tmp", self.entry_photo_path)
            elif os.path.exists(self.entry_photo_path):
                os.remove(self.entry_photo_path)
        except OSError as e:  # 못 써도 메모리엔 있음
            print(f"입실 전 사진 파일을 못 썼어요 ({self.entry_photo_path}: {e})")

    def set_entry(self, snap, now, now_iso):
        jpg = snap.get("jpg")
        self.entry = {"objects": snap["objects"], "at": now, "jpg": jpg, "vlm": snap.get("vlm")}
        self.write_entry_photo(jpg)
        if self.entry_desk is not None:
            if snap["img"] is not None:
                self.entry_desk.set_prepared(snap["img"], snap["known"], snap.get("labels") or ())
            else:
                self.entry_desk.clear()
        if self.entry_path:
            record = {"roomId": self.room_id, "objects": snap["objects"],
                      "savedAt": now_iso or datetime.now(KST).isoformat(timespec="seconds")}
            if self.entry_desk is not None:  # 사진 지문을 같이 적어 같은 때 저장된 짝인지 켤 때 확인한다
                record["deskSum"] = fingerprint(self.entry_desk.base) if self.entry_desk.ready else None
            if jpg:
                record["photoSum"] = photo_sum(jpg)
            try:
                with open(self.entry_path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(record, f, ensure_ascii=False)
                os.replace(self.entry_path + ".tmp", self.entry_path)
            except OSError as e:  # 못 써도 메모리엔 있음
                print(f"예약 기준 파일을 못 썼어요 ({self.entry_path}: {e})")
        n = sum(snap["objects"].values())
        shot = ""
        if self.photos:  # 입실 전 사진을 찍었는지 같이 알린다 (11/12 수연)
            shot = f" · 입실 전 사진 찍음 ({kb(jpg)})" if jpg else " · 입실 전 사진 없음"
        print(f"예약 기준 저장 {self.room_id}번 방: 입실 직전 빈 방 물건 {n}개"
              f"{' (' + describe(snap['objects']) + ')' if n else ''}{shot}")

    def clear_entry(self):
        """예약 기준을 지운다 (정리 확인을 보냈거나, 그 예약 확인을 포기했을 때)."""
        self.entry = None
        if self.entry_desk is not None:
            self.entry_desk.clear()
        for path in (self.entry_path, self.entry_photo_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as e:
                    print(f"예약 기준 파일을 못 지웠어요 ({path}: {e})")

    def track_entry(self, raw, pir, now, now_iso, detail, dets, photo):
        """빈 방 후보를 들고 있다가, 사람이 처음 보이면 그 후보를 다음 예약 기준으로 확정한다.

        QR 순간이 아니라 "사람이 보이기 직전"이라 QR 없이 들어와도, 바로 들어와도 기준이 잡힌다.
        """
        if raw > 0:
            self.raw_empty_since = None
            pir_since, self.pir_since = self.pir_since, None
            rebase, self.rebase_entry = self.rebase_entry, False
            held, self.held_snapshot = self.held_snapshot, None
            if self.snapshot is None and rebase and held is not None and now - held[1] <= HELD_SNAPSHOT_SEC:
                self.snapshot = held[0]  # QR보다 사람이 먼저 잡혀 후보를 써 버렸다: 그 빈 방으로 기준을 잡는다
            elif self.snapshot is None and rebase:
                print(f"예약 기준 {self.room_id}번 방: 입실 문이 열렸지만 직전 빈 방 사진이 없어 기준을 새로 못 잡음")
            if self.snapshot is not None:
                snap, self.snapshot = self.snapshot, None
                if self.retrieve is not None:
                    # 찾으러 온 사람 (10분 QR). 앞 예약 다시 확인은 그대로 두고, 이 빈 방은 다음 예약 기준으로 쓰지 않는다
                    # (두고 간 물건이 찍힌 빈 방이라). 다음 팀 기준은 그다음 빈 방에서 새로 찍는다
                    print(f"정리 확인 {self.room_id}번 방: 예약 {self.retrieve['reservationId']} 물건 찾으러 들어옴 "
                          f"(QR 뒤 {self.retrieve_sec // 60}분 지나 방이 비면 다시 찍어요)")
                    return
                self.drop_recheck_on_entry()  # 빈 방 뒤 사람이 들어옴: 후보를 쓰든 버리든 앞 예약 다시 확인은 안 함
                if pir_since is not None and now - pir_since > ENTRY_PIR_GRACE_SEC:
                    # 카메라엔 안 보였지만 PIR이 오래 켜져 있었다. 그 사이 뭘 두었을 수 있어 이 후보는 버린다
                    print(f"예약 기준 {self.room_id}번 방: 사람이 보이기 전 PIR이 오래 켜져 있어 후보를 쓰지 않음")
                    return
                if not self.confirm_entry(snap, now, now_iso, rebase=rebase):
                    self.held_snapshot = (snap, now)  # 안 썼으면 잠깐 들고 있는다 (바로 뒤 QR 입실이면 씀)
            return
        if pir:  # 카메라엔 안 보여도 누가 있을 수 있다. 후보를 새로 찍지 않는다
            self.raw_empty_since = None
            if self.pir_since is None and self.snapshot is not None:
                self.pir_since = now
            return
        if self.raw_empty_since is None:
            self.raw_empty_since = now
        if now - self.raw_empty_since < ENTRY_EMPTY_SEC:
            return
        if self.snapshot is not None and self.pir_since is None and now - self.snapshot["at"] < SNAPSHOT_SEC:
            return  # (PIR이 켜졌던 후보는 다시 비면 바로 새로 찍는다)
        seen = detail() if detail else dets
        img = known = None
        labels = []
        if self.entry_desk is not None and photo is not None:
            img = self.entry_desk.look(photo())  # 책상 부분만 흑백·작게. 메모리에만
            known = [d.box for d in self.objects(seen)]
            labels = [d.label for d in self.objects(seen)]
        jpg = self.room_photo(photo, dets, seen)  # 입실 전 사진 (TIDY_PHOTOS)
        self.snapshot = {"objects": self.count(seen), "img": img, "known": known, "labels": labels, "jpg": jpg,
                         "vlm": self.vlm_photo(photo, dets, seen), "at": now}
        self.pir_since = None

    def door_opened(self, purpose, now):
        """문 QR이 열렸다 (QR 스레드에서 부름). 카메라 스레드가 다음 update에서 처리한다."""
        self.door_events.append((purpose, now))

    def handle_door(self):
        """찾으러 온 QR(RETRIEVE)이면 앞 예약 다시 확인을 남겨 두고, 다른 QR(다음 팀 등)이면 지금처럼 취소한다."""
        while self.door_events:
            purpose, at = self.door_events.popleft()
            if purpose == "RETRIEVE":
                rid = (self.recheck_base or {}).get("reservationId")
                if rid is None:
                    print(f"정리 확인 {self.room_id}번 방: 찾으러 온 QR이지만 다시 확인할 기준이 없어 다시 찍지 않음 "
                          "(첫 확인이 깨끗했거나 젯슨이 꺼졌다 켜짐)")
                    continue
                self.retrieve = {"reservationId": rid, "at": at}
                if self.recheck_pending and self.recheck_pending[0] == rid:
                    self.recheck_pending = None  # 3시간 확인이 기다리던 중이었으면 찾으러 온 뒤 확인으로 대신한다
                print(f"정리 확인 {self.room_id}번 방: 예약 {rid} 물건 찾으러 온 QR. {self.retrieve_sec // 60}분 뒤 빈 방이면 다시 찍어요")
            else:
                if not self.checkin_seen:  # 퇴실 뒤 처음 입실 문 열림: 이 예약 기준을 그 직전 빈 방으로
                    self.checkin_seen = True
                    self.rebase_entry = True
                if self.retrieve is not None or self.retrieve_rid is not None:
                    print(f"정리 확인 {self.room_id}번 방: 찾으러 온 뒤 다시 확인 전에 다른 QR로 들어와 취소")
                self.retrieve = self.retrieve_rid = None
                self.drop_recheck_on_entry()

    def due_retrieve(self, now):
        """찾으러 온 QR 뒤 retrieve_sec가 지났으면 다시 확인을 요청한다 (방이 비면 checkout_check가 찍는다)."""
        if self.retrieve is None or now - self.retrieve["at"] < self.retrieve_sec:
            return
        if self.pending is not None or self.recheck_pending is not None:
            return  # 퇴실 확인이 먼저
        rid, self.retrieve = self.retrieve["reservationId"], None
        self.retrieve_rid = rid
        self.recheck_pending = (rid, now)

    def drop_recheck_on_entry(self):
        """앞 예약 퇴실 뒤 누가 들어왔다 (다음 팀·워크인·앞 팀이 가지러 옴). 그 뒤 물건은 누구 것인지 모르니
        앞 예약 다시 확인은 하지 않는다 (잘못된 경고를 막음, 다음 팀 입실 때 서버가 빼는 것과 같은 규칙)."""
        if self.recheck_base or self.recheck_pending:
            rid = (self.recheck_base or {}).get("reservationId") or self.recheck_pending[0]
            print(f"정리 확인 {self.room_id}번 방: 예약 {rid} 퇴실 뒤 사람이 들어와 다시 확인은 하지 않음")
            self.recheck_base = self.recheck_pending = None

    def confirm_entry(self, snap, now, now_iso, rebase=False):
        """빈 방 후보를 예약 기준으로 굳힌다. 썼으면 True, 화장실처럼 처음 기준을 둘 때는 False."""
        pending_at = self.pending[1] if self.pending else None
        if rebase and self.entry is not None:
            print(f"예약 기준 {self.room_id}번 방: 입실 문이 열려 그 직전 빈 방으로 기준을 다시 잡아요 "
                  "(그 전에 들어왔던 것은 기준으로 안 씀)")
        elif self.entry is not None and self.entry.get("loaded"):
            print(f"예약 기준 {self.room_id}번 방: 다시 켜기 전 기준 대신 지금 입실 직전 빈 방으로 새로 잡아요")
        elif self.entry is not None and (pending_at is None or self.entry["at"] >= pending_at):
            return False  # 같은 예약 사람이 잠깐 나갔다 온 것 (화장실). 처음 기준을 그대로 둔다
        if self.pending:
            # 앞 예약이 끝났는데 방이 10초 비기 전에 다음 사람이 들어옴. 앞 예약은 결과를 내지 않는다 (신고가 받음)
            print(f"정리 확인 {self.room_id}번 방: 예약 {self.pending[0]} 확인 전에 다음 사람이 들어와 건너뜀")
            self.pending = None
        self.set_entry(snap, now, now_iso)
        return True

    def people(self, dets):
        return sum(1 for d in dets if d.label == "person")

    def entry_vlm(self):
        """입실 전 사진의 VLM용 JPEG. 메모리에 없으면(젯슨을 다시 켬) 저장된 입실 전 사진(TIDY_PHOTOS)으로 만든다."""
        if self.vlm is None or self.entry is None:
            return None
        if self.entry.get("vlm"):
            return self.entry["vlm"]
        jpg = self.entry.get("jpg")
        if not jpg:
            return None
        try:
            import cv2
            import numpy as np
            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("입실 전 사진을 못 읽음")
            self.entry["vlm"] = self.vlm.prepare(img)
            return self.entry["vlm"]
        except Exception as e:  # 못 만들어도 YOLO 확인은 그대로
            print(f"입실 전 사진으로 VLM 사진을 못 만들었어요 ({e.__class__.__name__}). YOLO 결과만 써요")
            return None

    def vlm_photo(self, photo, *seen):
        """VLM_URL일 때 지금 화면을 VLM에 보낼 크기 JPEG로 (메모리에만). 사람이 보이면 None."""
        if self.vlm is None or photo is None or any(self.people(d) for d in seen):
            return None
        try:
            return self.vlm.prepare(photo())
        except Exception as e:  # 사진을 못 만들어도 YOLO 확인은 그대로
            print(f"VLM 사진을 못 만들었어요 ({e.__class__.__name__}). YOLO 결과만 써요")
            return None

    def room_photo(self, photo, *seen):
        """TIDY_PHOTOS일 때 지금 화면을 JPEG로. 빠르게 센 것·자세히 찾은 것 어디에도 사람이 없을 때만 (사람 사진은 안 올림)."""
        if not self.photos or photo is None or any(self.people(d) for d in seen):
            return None
        return photo_jpeg(photo())

    def update(self, dets, now, pir=None, now_iso=None, detail=None, photo=None):
        """한 번 센 결과를 넣는다. 보낼 재실 이벤트(dict)와 정리 확인 결과(dict)를 (event, check)로 돌려준다.

        detail: 기준 저장·정리 확인 때만 부르는 "자세히 찾기"(4조각). 없으면 dets를 그대로 쓴다.
        photo: 기준 저장·정리 확인 때만 부르는 "지금 화면" (책상 사진 비교용, DESK_PHOTO=1일 때만 씀).
        check는 정리 확인 결과이거나, 깨끗한 방 기준을 저장했다는 알림(status BASELINE_SAVED)이다.
        """
        self.counts.append(self.people(dets))
        people = sorted(self.counts)[len(self.counts) // 2]  # 가운데 값

        event = None
        occupied = people > 0 or bool(pir)
        if (people != self.sent_people or occupied != self.sent_occupied  # PIR만 바뀌어도 바로 보낸다
                or self.sent_at is None or now - self.sent_at >= BEAT_SEC):
            event = self.event(people, pir, now_iso)
            self.sent_people, self.sent_occupied, self.sent_at = people, occupied, now

        check = None
        if self.on_checkout:
            self.handle_door()
            self.due_retrieve(now)
        if self.on_checkout and self.check_objects:
            self.track_entry(self.people(dets), pir, now, now_iso, detail, dets, photo)
        if people > 0:
            self.had_people, self.empty_since = True, None
        elif pir:
            # 카메라엔 안 보여도 PIR이 움직임을 느끼면(마지막 움직임 뒤 2분) 아직 누가 있을 수 있다
            self.empty_since = None
        else:
            if self.empty_since is None:
                self.empty_since = now
            no_photo = self.desk is not None and not self.desk.ready and photo is not None
            if (self.baseline is None or no_photo) and not self.had_people and self.check_objects:
                # 처음 켰을 때 빈 방 = 깨끗한 기준 (책상 사진 비교를 새로 켰는데 사진이 없을 때도)
                check = self.save_baseline(detail() if detail else dets, now_iso, photo=photo)
            elif self.on_checkout:
                check = self.checkout_check(dets, now, now_iso, detail, photo)
            elif self.had_people and now - self.empty_since >= EMPTY_CHECK_SEC:
                seen = detail() if detail and self.check_objects else dets
                if self.baseline is None and self.check_objects:
                    # 기준 없이 비교하면 전부 "새 물건"이 된다. 결과를 내지 않고 지금 모습을 기준으로 삼는다
                    check = self.save_baseline(seen, now_iso, reason="기준이 없어 지금 모습을 기준으로 저장",
                                               photo=photo)
                else:
                    check = self.check(seen, now_iso, photo=photo)
                self.had_people = False
        if self.on_checkout and self.pending and now - self.pending[1] >= PENDING_MAX_SEC:
            print(f"정리 확인 {self.room_id}번 방: 예약 {self.pending[0]} 퇴실 뒤 10분 동안 방이 비지 않아 건너뜀")
            if self.entry is not None and self.entry["at"] < self.pending[1]:
                self.clear_entry()  # 그 예약의 기준이었다. 다음 예약 탓으로 쓰지 않게 버린다
            self.pending = None
        if (self.on_checkout and self.recheck_pending and self.retrieve_rid != self.recheck_pending[0]
                and now - self.recheck_pending[1] >= PENDING_MAX_SEC):
            # 찾으러 온 뒤 확인은 10분 제한 없이 방이 빌 때까지 기다린다 (core #130은 recheck.at을 문 연 시각 + 10분으로
            # 당기고 그 뒤 따로 부르지 않아서, 건너뛰면 다시 확인이 사라짐). 다음 팀 QR이면 handle_door가 취소한다
            rid = self.recheck_pending[0]
            print(f"정리 확인 {self.room_id}번 방: 예약 {rid} 다시 확인 시각 뒤 10분 동안 방이 비지 않아 건너뜀")
            self.cancel_recheck(rid)
        return event, check

    def request_check(self, reservation_id, now, phase="FIRST"):
        """서버에서 예약이 끝났다(FIRST)거나 다시 확인할 때가 됐다(RECHECK)고 알았을 때 부른다.

        방이 비면(사람 0, PIR 없음) 확인한다.
        """
        if phase == "RECHECK":
            waiting = (self.retrieve or {}).get("reservationId")
            if reservation_id in (waiting, self.retrieve_rid):
                # 찾으러 온 뒤 다시 확인을 기다리는 중: 그 확인이 대신한다 (N분 전에 찍지 않게, 기준도 지키게)
                print(f"정리 확인 {self.room_id}번 방: 예약 {reservation_id} 3시간 확인은 찾으러 온 뒤 확인으로 대신해요")
                return
            self.recheck_pending = (reservation_id, now)
        else:
            self.pending = (reservation_id, now, "FIRST")
            self.checkin_seen = False  # 다음 입실 문 열림이 다음 예약의 입실

    def cancel_recheck(self, reservation_id):
        """서버가 recheck를 뺐다(그 사이 다음 팀이 입실) 등: 그 예약은 다시 확인하지 않는다. 첫 확인(FIRST)은 그대로."""
        if self.recheck_pending and self.recheck_pending[0] == reservation_id:
            self.recheck_pending = None
        if self.recheck_base and self.recheck_base["reservationId"] == reservation_id:
            self.recheck_base = None

    def postpone_recheck(self, reservation_id):
        """서버가 같은 예약의 다시 확인 시각을 바꿨다: 기다리던 요청만 빼고 기준은 둔다 (새 시각에 다시 부름)."""
        if self.recheck_pending and self.recheck_pending[0] == reservation_id:
            self.recheck_pending = None

    def checkout_check(self, dets, now, now_iso, detail, photo=None):
        if (self.pending is None and self.recheck_pending is None) or now - self.empty_since < SETTLE_SEC:
            return None
        now_iso = now_iso or datetime.now(KST).isoformat(timespec="seconds")
        if self.pending is None:  # 퇴실 확인이 먼저. 그게 없을 때만 3시간 뒤 다시 확인
            reservation_id, _ = self.recheck_pending
            self.recheck_pending = None
            return self.recheck(reservation_id, dets, now_iso, detail, photo) if self.check_objects else None
        reservation_id = self.pending[0]
        self.pending = None
        self.had_people = False
        if not self.check_objects:
            return None
        if self.entry is None:
            # 입실 전에 방이 3초도 비지 않아 기준이 없다. 누구 것인지 모르니 결과를 보내지 않는다 (어질러짐 신고가 받음)
            return {"roomId": self.room_id, "status": "SKIPPED", "checkedAt": now_iso,
                    "reason": f"예약 {reservation_id}: 입실 직전 빈 방 기준이 없어 결과를 보내지 않음"}
        seen = detail() if detail else dets
        desk = self.entry_desk if self.entry_desk is not None and self.entry_desk.ready else None
        check = self.check(seen, now_iso, photo=photo if desk else None, base=self.entry["objects"], desk=desk)
        check["reservationId"] = reservation_id
        check["phase"] = "FIRST"
        before = self.entry_vlm()
        after = self.vlm_photo(photo, dets, seen) if before else None
        if after:  # VLM은 카메라 스레드를 막지 않게 run_room_camera가 따로 묻는다 (사진은 결과를 보내면 버림)
            check["vlmPhotos"] = (before, after)
        photos = {"FIRST": self.room_photo(photo, dets, seen), "ENTRY": self.entry.get("jpg") if self.photos else None}
        photos = {k: v for k, v in photos.items() if v}  # 퇴실 뒤 사진부터 (입실 전은 core가 아직 안 받을 수 있음)
        if photos:
            check["photos"] = photos
        # 물건이 남았으면 3시간 뒤 다시 확인할 때 쓰게 기준 개수와 VLM용 입실 전 사진을 남긴다 (책상 사진은 지움).
        # VLM 사진은 젯슨 메모리에만 두고, 다시 확인이 끝나거나 취소되면(다음 팀 입실 등) 버린다 (11/12 수연: 보관).
        # YOLO가 깨끗하다 해도 VLM 답("or")으로 남음이 될 수 있어, VLM에게 물을 때는 일단 남긴다
        keep = check["status"] != "CLEAN" or "vlmPhotos" in check
        self.recheck_base = ({"reservationId": reservation_id, "objects": dict(self.entry["objects"])} if keep else None)
        if keep and before:
            self.recheck_base["vlm"] = before
        self.clear_entry()  # 보냈으면 지운다 (다음 예약은 다음 빈 방에서 새로)
        return check

    def recheck(self, reservation_id, dets, now_iso, detail, photo=None):
        """3시간 뒤 다시 확인 (RECHECK). 첫 확인 때와 같은 기준(입실 직전 빈 방)과 개수로 비교하고,
        VLM을 켰으면 남겨 둔 입실 전 사진과 지금 사진을 VLM에게 다시 묻는다 (확신도 = 지금 "새 물건 있음" 확률).

        책상 사진 비교는 하지 않는다 (책상 사진은 첫 확인 뒤 지움).
        """
        base, self.recheck_base = self.recheck_base, None
        retrieved = self.retrieve_rid == reservation_id
        self.retrieve_rid = None
        if base is None or base["reservationId"] != reservation_id:
            return {"roomId": self.room_id, "status": "SKIPPED", "checkedAt": now_iso,
                    "reason": f"예약 {reservation_id}: 다시 확인할 기준이 없어 결과를 보내지 않음 (첫 확인이 깨끗했거나 젯슨이 꺼졌다 켜짐)"}
        seen = detail() if detail else dets
        check = self.check(seen, now_iso, base=base["objects"])
        check["reservationId"] = reservation_id
        check["phase"] = "RECHECK"
        after = self.vlm_photo(photo, dets, seen) if base.get("vlm") else None
        if after:  # FIRST처럼 run_room_camera가 따로 묻는다
            check["vlmPhotos"] = (base["vlm"], after)
        if retrieved:
            check["afterRetrieve"] = True  # 화면 출력용 (core로는 안 보냄)
            if check["status"] != "CLEAN":
                # 아직 남음: 기준은 남겨 둔다. core는 예약마다 RECHECK를 한 번만 받아서(두 번째는 409) 보통 다시 부르지 않고,
                # /current에서 recheck가 빠지면 on_recheck_gone이 지운다. 남은 물건은 core가 처리 (결정 30: 경고·알림 없음)
                self.recheck_base = base
        jpg = self.room_photo(photo, dets, seen)
        if jpg:
            check["photos"] = {"RECHECK": jpg}
        return check

    def event(self, people, pir, now_iso):
        sources = {"camera": True, "people": people}
        if pir is not None:
            sources["pir"] = bool(pir)
        return {
            "eventId": str(uuid.uuid4()),
            "roomId": self.room_id,
            "deviceId": f"camera-{self.room_id}",
            "kind": "OCCUPANCY",
            "occupied": people > 0 or bool(pir),  # 하나라도 있음이면 있음
            "sources": sources,
            "observedAt": now_iso or datetime.now(KST).isoformat(timespec="seconds"),
        }

    def save_baseline(self, dets, now_iso, reason=None, photo=None):
        items = self.count(dets)
        self.baseline = dict(items)
        self.write_baseline(now_iso)
        if self.desk is not None and photo is not None:
            self.desk.set_base(photo(), self.objects(dets))  # 사람 0명일 때만 여기 온다
        note = {
            "roomId": self.room_id,
            "status": "BASELINE_SAVED",
            "objects": items,
            "checkedAt": now_iso or datetime.now(KST).isoformat(timespec="seconds"),
        }
        if reason:
            note["reason"] = reason
        return note

    def objects(self, dets):
        return [d for d in dets if d.label in TRASH or d.label in BELONGINGS]

    def scores(self, dets):
        """정리 확인에 쓰는 물건의 확신도를 종류별로, 낮은 것부터. {"cup": [0.41, 0.77]}"""
        out = {}
        for d in self.objects(dets):
            out.setdefault(d.label, []).append(round(float(d.score), 2))
        return {k: sorted(v) for k, v in out.items()}

    def count(self, dets):
        """정리 확인에 쓰는 물건만 종류별로 센다. {"cup": 2, "remote": 1}"""
        items = {}
        for d in self.objects(dets):
            items[d.label] = items.get(d.label, 0) + 1
        return items

    def desk_items(self, frame, dets, desk=None):
        """책상 사진 비교로 달라진 곳 중, 모델이 이미 찾은 물건·기준 때 있던 물건 자리가 아닌 것에 이름을 붙인다."""
        desk = desk or self.desk
        changes = desk.changes(frame)
        if changes is None:
            return None  # 비교를 못 함 (기준 없음, 조명·카메라가 크게 바뀜). "달라진 곳 없음"과 구분
        seen = [d.box for d in self.objects(dets)] + list(desk.known)
        items = {}
        for box in changes:
            if any(overlaps(box, b) for b in seen):
                continue  # 이미 개수로 센 물건이거나, 원래 있던 물건(리모컨)을 옮겨서 생긴 빈자리
            label = (self.labeler(frame, box) if self.labeler else None) or UNKNOWN
            if label in FURNITURE:
                continue  # 의자를 빼 두거나 넣은 자리 (가구는 정리 대상이 아님)
            items[label] = items.get(label, 0) + 1
        return items

    def not_moved(self, dets, now_items, base, desk):
        """DESK_MOVE=1: 늘어난 물건 중 "원래 있던 걸 옮기거나 다른 이름으로 잘못 본 것"은 빼고 다시 센다 (9판).

        - 책상 비교가 "옮긴 자리"로 본 곳에 있는 것 (리모컨을 옮김·뒤집음)
        - 기준 때 다른 이름의 물건이 있던 자리에, 비슷한 크기로 잡힌 것 (뒤집힌 리모컨을 laptop으로 봄)
        개수가 기준보다 늘어난 이름만 본다. 기준에 있던 같은 이름의 물건은 원래대로 센다.
        """
        known = list(zip(desk.known, desk.known_labels))
        items = dict(now_items)
        for d in self.objects(dets):
            if items.get(d.label, 0) <= max(base.get(d.label, 0), self.room_items.get(d.label, 0)):
                continue
            renamed = any(label != d.label and overlaps(d.box, box) and similar_size(d.box, box) for box, label in known)
            if renamed or any(overlaps(d.box, box) for box in desk.moved):
                items[d.label] -= 1
        return {k: v for k, v in items.items() if v > 0}

    def check(self, dets, now_iso, photo=None, base=None, desk=None):
        """깨끗할 때보다 늘어난 물건만 골라 결과를 만든다 (종류별 개수, 자리는 보지 않음).

        책상 사진 비교(DESK_PHOTO)를 켜면 모델이 모르는 물건도 "모르는 물건"(치울 것)으로 더한다.
        base·desk를 주면(퇴실 모드의 예약 기준) 그것과 비교하고, 깨끗한 방 기준은 건드리지 않는다.
        """
        if not self.check_objects:
            return None
        own = base is None  # 깨끗한 방 기준과 비교 (연습 모드)
        base = (self.baseline or {}) if own else base
        desk = self.desk if own else desk
        now_items = self.count(dets)
        extra = {}
        compared = False
        if desk is not None and desk.ready and photo is not None:
            frame = photo()
            found = self.desk_items(frame, dets, desk)
            del frame
            compared = found is not None
            extra = found or {}
            if not compared:
                print(f"정리 확인 {self.room_id}번 방: 책상 사진 비교를 건너뜀 (조명·카메라가 크게 바뀜)")
        if compared and desk.tune.get("move"):
            now_items = self.not_moved(dets, now_items, base, desk)
        trash, left, confidence = {}, {}, {}
        scores = self.scores(dets)
        by_yolo = False
        for label, n in now_items.items():
            more = n - max(base.get(label, 0), self.room_items.get(label, 0))  # 방 비품은 그 개수까지 괜찮음
            if more > 0:
                by_yolo = True
                (trash if label in TRASH else left)[label] = more
                # 어느 것이 새로 생긴 건지 모르니 점수가 낮은 쪽부터 쓴다 (확신도를 부풀려 잘못 경고하지 않게)
                confidence[label] = scores[label][:more]
        for label, n in extra.items():  # 책상 사진 비교로 찾은 것: 자리가 새로 생긴 것이라 기준 개수와 빼지 않는다
            group = left if label in BELONGINGS else trash  # 이름 모름(unknown)·쓰레기는 치울 것
            group[label] = group.get(label, 0) + n
            confidence[label] = confidence.get(label, []) + [None] * n  # 자기 점수 없음: 보낼 때 VLM 확률로 채운다
        status = "LEFT_ITEMS" if left else "NEEDS_CLEANING" if trash else "CLEAN"
        if own and status == "CLEAN" and compared:  # 비교를 건너뛰었으면 물건이 있을 수 있어 사진을 바꾸지 않는다
            self.desk.set_base(photo(), self.objects(dets))  # 깨끗하면 책상 사진도 새로 (햇빛·조명이 천천히 바뀌는 것 따라감)
        if own and status == "CLEAN":  # 깨끗하면 기준을 넓혀 둔다 (줄어든 건 기준에서 빼지 않음: 다시 나와도 괜찮게)
            widened = {k: max(base.get(k, 0), now_items.get(k, 0)) for k in set(base) | set(now_items)}
            if widened != base:
                self.baseline = widened
                self.write_baseline(now_iso)
        return {
            "roomId": self.room_id,
            "status": status,
            "trash": trash,
            "leftItems": left,
            "confidence": confidence,  # {이름: [물건마다 YOLO 점수 0~1 또는 None(책상 비교로만 찾음)]}
            "evidence": {"yolo": by_yolo, "desk": bool(extra)},  # 무엇이 찾았나 (물건이 있을 확률 표의 열쇠)
            "checkedAt": now_iso or datetime.now(KST).isoformat(timespec="seconds"),
        }



MAX_SERVER_ITEMS = 20


def evidence_key(evidence, vlm="unused", phase="FIRST"):
    """무엇이 물건을 찾았나 → 물건이 있을 확률 표(model_score.presence_table)의 열쇠.

    FIRST: "yolo+desk+vlm"처럼 찾은 것만 잇는다. VLM을 안 물었으면 vlm 칸이 없고,
    물었는데 "없음"이면 vlm-no, 못 알아들었거나 답이 없으면 vlm-?.
    RECHECK(3시간 뒤): YOLO 개수만 보므로 늘 "recheck".
    """
    if phase == "RECHECK":
        return "recheck"
    parts = [name for name in ("yolo", "desk") if (evidence or {}).get(name)]
    if vlm != "unused":
        parts.append({True: "vlm", False: "vlm-no"}.get(vlm, "vlm-?"))
    return "+".join(parts) or "none"


def check_key(check):
    return evidence_key(check.get("evidence"), check.get("vlm", "unused"), check.get("phase", "FIRST"))


def tidy_body(check, device_id, event_id=None, model=None):
    """정리 확인 결과를 서버 POST /api/rooms/{roomId}/tidy-checks 형식으로 바꾼다 (10차, 11/10 지영과 맞춤).

    10차: 쓰레기든 물건이든 종류 구분 없이 "두고 감". 장치 결과(치울 것·두고 간 물건) → 서버 2가지:
    남은 것이 하나라도 있으면 LEFT_ITEMS, 없으면 CLEAN.
    items = 물건마다 {"name": 모델 이름(영어), "confidence": 0~1}. 한글 바꾸기·경고 기준(확신도)은 core가 한다.
    phase = FIRST(퇴실 직후, 알림만) | RECHECK(3시간 뒤, 그래도 남았으면 core가 경고).
    model = 지금 설정의 시험 점수 (model_score.find). 같은 설정으로 채점한 기록이 있을 때만 붙인다.

    confidence = 이 물건이 정말 남아 있을 확률 (11/12 수연: 이름이 맞을 확률이 아니라 "물건이다"의 확률).
      VLM이 답했으면 그 물건의 YOLO 점수와 VLM "새 물건 있음" 확률 중 높은 값 (11/12 수연: VLM은 없다 24%,
      YOLO는 마우스 71%로 "남음"인데 24%로 보이던 것 고침). YOLO 점수가 없는 물건(책상 비교로만 찾음)은 VLM 확률.
      VLM 답이 없으면(VLM 꺼짐·시간 초과·젯슨 재시작으로 입실 전 사진 없음) YOLO 점수, 그것도 없으면 DESK_DIFF_CONF.
      RECHECK도 VLM에게 다시 물어서, 경고(core, RECHECK + 0.8 이상)는 3시간 뒤의 YOLO·VLM 중 높은 값으로 정해진다.
    """
    found = {**check["leftItems"], **check["trash"]}
    conf = check.get("confidence", {})
    yes = check.get("vlmYes")
    items = []
    for label, n in sorted(found.items()):
        scores = list(conf.get(label, []))[:n]
        scores += [None] * (n - len(scores))
        for c in scores:
            rank = c if c is not None else yes if yes is not None else DESK_DIFF_CONF
            c = rank if yes is None else yes if c is None else max(c, yes)
            items.append(({"name": label, "confidence": round(float(c), 3)}, float(rank)))
    # 20개가 넘으면 확신이 큰 것부터 남긴다. 같으면 YOLO 점수가 큰 것부터
    items = [item for item, _ in sorted(items, key=lambda pair: (-pair[0]["confidence"], -pair[1]))]
    body = {
        "eventId": event_id or str(uuid.uuid4()),
        "reservationId": check["reservationId"],
        "deviceId": device_id,
        "phase": check.get("phase", "FIRST"),
        "result": "LEFT_ITEMS" if found else "CLEAN",
        "items": items[:MAX_SERVER_ITEMS],
        "observedAt": check["checkedAt"],
    }
    if model:
        body["model"] = model
    return body


def photo_body(reservation_id, phase, device_id, jpg):
    """POST /api/rooms/{roomId}/tidy-photos (core #115) multipart 칸. phase = ENTRY | FIRST | RECHECK."""
    return {"reservationId": reservation_id, "phase": phase, "deviceId": device_id, "jpg": jpg}


PHOTO_NAMES = {"ENTRY": "입실 전", "FIRST": "퇴실 뒤", "RECHECK": "3시간 뒤"}


def kb(jpg):
    return f"{max(1, round(len(jpg) / 1024))}KB"


def photo_message(check, photos):
    """정리 확인 때 어떤 사진을 찍었고 무엇이 없는지 한 줄로 (11/12 수연 "사진 찍은 것도 로그로")."""
    want = ("ENTRY", "FIRST") if check.get("phase") == "FIRST" else (check.get("phase"),)
    parts = [f"{PHOTO_NAMES.get(k, k)} 찍음 ({kb(photos[k])})" if photos.get(k)
             else f"{PHOTO_NAMES.get(k, k)} 없음" for k in want]
    return f"정리 확인 사진 {check['roomId']}번 방 예약 {check['reservationId']}: " + ", ".join(parts)


def check_message(check):
    if check["status"] == "BASELINE_SAVED":
        n = sum(check["objects"].values())
        listed = f" ({describe(check['objects'])})" if n else ""
        why = f" - {check['reason']}" if check.get("reason") else ""
        return f"깨끗한 방 기준 저장 {check['roomId']}번 방: 물건 {n}개{listed}{why}"
    if check["status"] == "SKIPPED":
        return f"정리 확인 {check['roomId']}번 방: 건너뜀 - {check['reason']}"
    again = ""
    if check.get("phase") == "RECHECK":
        again = " (찾으러 온 뒤 다시 확인)" if check.get("afterRetrieve") else " (3시간 뒤 다시 확인)"
    if check["status"] == "CLEAN":
        return f"정리 확인 {check['roomId']}번 방{again}: 깨끗함"
    parts = []
    if check["leftItems"]:
        parts.append("두고 간 물건 " + describe(check["leftItems"]))
    if check["trash"]:
        parts.append("치울 것 " + describe(check["trash"]))
    return f"정리 확인 {check['roomId']}번 방{again}: " + " / ".join(parts) + item_scores(check)


def item_scores(check):
    """화면에만: 물건마다 YOLO 점수 (11/12 수연. 서버로는 VLM 확률을 보내지만, 물건마다 얼마였는지 보고 싶음)."""
    found = {**check.get("leftItems", {}), **check.get("trash", {})}
    conf = check.get("confidence", {})
    shown = []
    for label, n in sorted(found.items()):
        scores = list(conf.get(label, []))[:n]
        scores += [None] * (n - len(scores))
        for c in scores:
            shown.append(f"{KOREAN.get(label, label)} {c:.0%}" if c is not None else f"{KOREAN.get(label, label)} 점수 없음")
    if not shown:
        return ""
    return "\n  물건마다 YOLO 점수 (화면에만): " + ", ".join(shown) + " (점수 없음 = 책상 비교·VLM으로만 찾음)"


# ---------- 카메라 루프 ----------

def open_camera(device):
    import cv2
    cam = cv2.VideoCapture(int(device) if str(device).isdigit() else device)
    if not cam.isOpened():
        raise SystemExit(f"방 안 웹캠({device})을 열 수 없어요. ls /dev/video* 로 번호를 확인하세요.")
    return cam


LABEL_MIN_SIDE = 320  # 달라진 곳을 이 크기 이상으로 잘라 키워서 모델에 넣는다 (작은 물건도 크게 보이게)


def label_region(detect, frame, box, pad=0.6, conf=0.2):
    """달라진 곳 상자 주변을 넉넉히 잘라 모델을 돌려, 가운데에 있는 물건 이름을 돌려준다. 없으면 None.

    detect는 사람·물건 80종 모델이거나, 우리 물건 이름을 넣은 YOLO-World 모델(LABEL_MODEL)이다.
    """
    h, w = frame.shape[:2]
    bx, by, bw, bh = box
    side = max(bw, bh) * (1 + 2 * pad)
    side = max(side, LABEL_MIN_SIDE * 0.5)
    cx, cy = bx + bw / 2, by + bh / 2
    x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    x1, y1 = int(min(w, cx + side / 2)), int(min(h, cy + side / 2))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    crop = frame[y0:y1, x0:x1]
    best = None
    for d in detect(crop):
        if d.label == "person" or d.score < conf:
            continue
        dx, dy = d.box[0] + d.box[2] / 2 + x0, d.box[1] + d.box[3] / 2 + y0
        if bx <= dx <= bx + bw and by <= dy <= by + bh and (best is None or d.score > best.score):
            best = d
    return best.label if best else None


def make_labeler(detector, label_model=None):
    """달라진 곳에 이름 붙이는 함수. LABEL_MODEL(YOLO-World)이 있으면 그걸, 없으면 지금 모델을 쓴다."""
    labeler = detector
    if label_model:
        try:
            labeler = YoloDetector(label_model)
            print("이름 붙이기: YOLO-World 모델", label_model, f"({len(labeler.names)}가지)")
        except Exception as e:  # noqa: BLE001 - 모델 파일 문제여도 카메라는 계속
            print(f"LABEL_MODEL을 못 읽어서 지금 모델로 이름을 붙여요 ({label_model}: {e})")
            labeler = detector

    def label(frame, box):
        """달라진 곳 하나에 이름. LABEL_MODEL 이름표가 틀리면 지금 모델로 바꿔 다시 붙인다."""
        nonlocal labeler
        try:
            return label_region(labeler.detect, frame, box)
        except LabelCountMismatch as e:
            if labeler is detector:
                raise
            print(f"LABEL_MODEL의 이름표가 모델과 안 맞아서 지금 모델로 이름을 붙여요 ({e})")
            labeler = detector
            return label_region(labeler.detect, frame, box)

    return label


YOLO_THREADS = 2  # 나머지 코어는 VLM(llama-server -t 6)과 QR 카메라에 남긴다


def yolo_threads(env=None):
    """YOLO(onnxruntime)가 쓸 CPU 스레드 수. YOLO_THREADS로 바꾸고, 0이면 onnxruntime 기본(코어 전부)."""
    value = (os.environ if env is None else env).get("YOLO_THREADS", "").strip()
    try:
        n = int(value) if value else YOLO_THREADS
    except ValueError:
        print(f"YOLO_THREADS는 0 이상 정수예요. {YOLO_THREADS}개로 해요: {value!r}")
        return YOLO_THREADS
    return max(0, n)


def make_detector(model_path, desk_area=None, floor_area=None, tile_grid=TILE_GRID):
    if model_path:
        print("인원 세기: YOLO 모델", model_path)
        if tile_grid != TILE_GRID:
            print(f"정리 확인: 화면을 {tile_grid}×{tile_grid}={tile_grid * tile_grid}조각으로 나눠 봐요 (TILE_GRID)")
        if desk_area:
            print("정리 확인: 책상 부분(DESK_AREA) %.2f,%.2f,%.2f,%.2f도 확대해서 봐요" % tuple(desk_area))
        if floor_area:
            print("정리 확인: 바닥 부분(FLOOR_AREA) %.2f,%.2f,%.2f,%.2f도 확대해서 봐요" % tuple(floor_area))
        return YoloDetector(model_path, desk_area=desk_area, floor_area=floor_area, tile_grid=tile_grid)
    print("인원 세기: 모델 파일이 없어 OpenCV 기본 사람 찾기(HOG)를 써요. 정리 확인은 꺼져요 (YOLO_MODEL 참고)")
    return HogPersonDetector()


def write_log(log_path, record):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def safe_log(log_path, record):
    """기록 파일에 못 써도(권한·디스크) 카메라는 계속 돈다."""
    try:
        write_log(log_path, record)
    except OSError as e:
        print("기록 파일에 못 썼어요(계속 돌아요):", log_path, repr(e))


def start_thread(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()


def apply_vlm(check, new_item, rule, yes=None):
    """VLM 답(새 물건 있음 True / 없음 False / 못 알아들음 None)을 정리 확인 결과에 합친다 (10판, VLM_RULE).

    or : YOLO가 깨끗하다 해도 VLM이 "있다"면 이름 모르는 물건 1개로 더한다 (확신도는 VLM이 YES라 한 확률)
    and: YOLO가 찾았어도 VLM이 "없다"면 깨끗함으로 바꾼다
    vlm: VLM 답만 쓴다
    """
    check["vlm"] = new_item
    if new_item is None:  # 못 알아들은 답의 확률은 쓰지 않는다 (YOLO 점수 그대로)
        return check
    if yes is not None:
        check["vlmYes"] = yes  # 이 사진 두 장에서 VLM이 "새 물건 있음"이라 할 확률
    clean = check["status"] == "CLEAN"
    if new_item and clean and rule in ("or", "vlm"):
        check["trash"] = {**check["trash"], UNKNOWN: check["trash"].get(UNKNOWN, 0) + 1}
        check.setdefault("confidence", {})[UNKNOWN] = check.get("confidence", {}).get(UNKNOWN, []) + [None]
        check["status"] = "NEEDS_CLEANING"
    elif not new_item and not clean and rule in ("and", "vlm"):
        check["trash"], check["leftItems"], check["confidence"] = {}, {}, {}
        check["status"] = "CLEAN"
    return check


def ask_vlm_then_finish(watcher, check, pair, log_path, send_check, send_photo):
    """VLM에게 묻고(따로 도는 스레드) 결과를 합친 뒤 보낸다. VLM이 안 되면 YOLO 결과 그대로 보낸다."""
    try:
        new_item, seconds, yes = watcher.vlm.ask_jpeg(*pair)
        answer = {True: "새 물건 있음", False: "새 물건 없음", None: "못 알아들음"}[new_item]
        sure = f", 있음 확률 {yes:.0%}" if yes is not None else ""
        print(f"VLM {watcher.room_id}번 방 예약 {check['reservationId']}: {answer} ({seconds:.0f}초{sure})")
        apply_vlm(check, new_item, watcher.vlm.rule, yes)
        base = getattr(watcher, "recheck_base", None)
        if (check.get("phase", "FIRST") == "FIRST" and check["status"] == "CLEAN" and base
                and base.get("reservationId") == check.get("reservationId")):
            watcher.recheck_base = None  # VLM도 깨끗하다 함: 3시간 뒤 확인이 없으니 남겨 둔 입실 전 사진을 버린다
    except Exception as e:  # 서버가 꺼졌거나 오래 걸림: 결과를 버리지 않고 YOLO 결과로 보낸다
        print(f"VLM에게 못 물었어요 ({e.__class__.__name__}). YOLO 결과만 보내요")
    del pair
    finish_check(watcher, check, log_path, send_check, send_photo)


def finish_check(watcher, check, log_path, send_check, send_photo):
    photos = check.pop("photos", None) or {}  # 사진 바이트는 기록 파일·화면에 남기지 않는다
    print(check_message(check))
    safe_log(log_path, check)
    if send_check is not None and "reservationId" in check:
        send_check(tidy_body(check, f"camera-{watcher.room_id}", model=getattr(watcher, "model_score", None)))
        if getattr(watcher, "photos", False):
            print(photo_message(check, photos))
        if send_photo is not None and photos:
            for phase, jpg in photos.items():
                send_photo(photo_body(check["reservationId"], phase, f"camera-{watcher.room_id}", jpg))
            print(f"정리 확인 사진 {len(photos)}장 올리기 대기 ({', '.join(photos)})")


def run_room_camera(watcher, cam, detector, push, pir_now=lambda: None, log_path="room_checks.jsonl",
                    every=1.0, clock=time.monotonic, sleep=time.sleep, on_frame=lambda: None, preview=None,
                    warn_every=10.0, send_check=None, send_photo=None):
    """카메라에서 1초마다 한 장씩 세서 watcher에 넣는다. 사진은 저장하지 않는다
    (TIDY_PHOTOS=1이면 사람 0명인 빈 방 사진만 core로 올리고, 입실 전 사진은 퇴실 확인 때까지만 젯슨에 둔다).

    on_frame: 사진을 잘 읽을 때마다 부른다 (게이트웨이가 카메라가 살아 있는지 본다).
    send_check: 퇴실 모드에서 예약에 붙은 정리 확인 결과를 서버로 보낸다 (tidy_body 형식).
    send_photo: 결과에 붙은 빈 방 사진(TIDY_PHOTOS)을 결과 다음에 올린다 (photo_body 형식). 기록 파일엔 안 남긴다.
    """
    print(f"방 안 카메라 켜짐: {watcher.room_id}번 방")
    last_warn = None
    while True:
        started = clock()
        ok, frame = cam.read()
        if not ok and (last_warn is None or started - last_warn >= warn_every):
            print(f"방 안 카메라 {watcher.room_id}번 방: 사진을 못 읽어요 (USB 연결 확인)")
            last_warn = started
        if ok:
            try:
                dets = detector.detect(frame)
                if preview is not None:  # 화면 보기를 켰을 때만 상자를 그려 메모리에 한 장 둔다
                    try:
                        area = getattr(detector, "desk_area", None)
                        if area:  # 책상 부분을 같이 그려서 DESK_AREA가 책상에 맞는지 본다
                            area = desk_box(frame.shape[1], frame.shape[0], area)
                        floor = getattr(detector, "floor_area", None)
                        if floor:  # 바닥 부분도 (FLOOR_AREA)
                            floor = desk_box(frame.shape[1], frame.shape[0], floor)
                        preview.update(frame, dets, watcher.people(dets), area=area, floor=floor)
                    except Exception as e:  # 화면 보기 오류는 인원 세기를 막지 않는다
                        print("화면 보기 오류(카메라는 계속 돌아요):", repr(e))
                detailed = getattr(detector, "detect_detailed", None)
                detail = (lambda: detailed(frame)) if detailed else None  # 기준·정리 확인 때만 불림
                # 책상 사진 비교(DESK_PHOTO)·정리 확인 사진(TIDY_PHOTOS) 때만 지금 화면을 쓴다
                photo = ((lambda: frame) if watcher.desk is not None or getattr(watcher, "photos", False)
                         or getattr(watcher, "vlm", None) is not None else None)
                event, check = watcher.update(dets, clock(), pir_now(), detail=detail, photo=photo)
                del frame, detail, photo  # 사진은 바로 버린다
                on_frame()  # 찾기까지 성공한 장만 "카메라 살아 있음"으로 친다
                if event:
                    print(f"인원 {watcher.room_id}번 방: {event['sources']['people']}명")
                    push(event)
                if check:
                    pair = check.pop("vlmPhotos", None)
                    if pair is not None:  # VLM은 30초쯤 걸려서 따로 묻고, 답을 합친 뒤 보낸다 (인원 세기는 계속)
                        print(f"정리 확인 {watcher.room_id}번 방 예약 {check['reservationId']}: VLM에게 한 번 더 물어요 (30초쯤)")
                        start_thread(ask_vlm_then_finish, watcher, check, pair, log_path, send_check, send_photo)
                    else:
                        finish_check(watcher, check, log_path, send_check, send_photo)
            except Exception as e:  # 한 장에서 오류가 나도 카메라 스레드는 계속 돈다
                print("방 안 카메라 처리 오류(계속 돌아요):", repr(e))
        sleep(max(0.0, every - (clock() - started)))
