"""방 안 웹캠으로 인원 세기 + 퇴실 뒤 정리 확인 (젯슨)

8차 회의(11/6) 멘토 의견: "받은 카메라로 몇 명인지 세 보라", "방이 깨끗한지(관리자 관점)".

하는 일
  1. 인원 세기: 1초마다 사람 수를 세서, 바뀌면 바로 + 바뀌지 않아도 10초마다 core에 보낸다.
     지금 core 재실 API(POST /api/sensor-events, kind OCCUPANCY)를 그대로 쓴다.
       occupied = 사람 1명 이상 (또는 우노 PIR이 있음)  ← "하나라도 있음이면 있음" (FR-DEV-103)
       sources  = {"camera": true, "people": 2, "pir": ...}  ← core는 sources를 그대로 기록만 한다
     그래서 core를 바꾸지 않고도 "몇 명"이 서버에 남는다.
  2. 정리 확인: 사람이 있다가 모두 나가고 30초가 지나면, 빈 방에서 물건(컵·병·노트북·가방 …)을 찾는다.
     처음 빈 방(깨끗한 상태)에서 본 물건과 비교해서 "새로 생긴 물건"만 남은 물건으로 본다.
     결과는 화면과 room_checks.jsonl(글자만)에 남긴다. core에 받을 곳이 생기면 그쪽으로 보낸다.

개인정보
  사진·영상은 저장하지도, 보내지도 않는다. 프레임은 메모리에서 세고 바로 버린다. 남는 건 숫자와 물건 이름뿐.

모델
  YOLO_MODEL=yolov8n.onnx 처럼 ONNX 모델 파일을 주면 사람 + 물건 80종을 찾는다 (OpenCV DNN, 젯슨 기본 설치).
  모델이 없으면 OpenCV 기본 사람 찾기(HOG)로 사람만 센다. 정확도가 낮고, 정리 확인은 꺼진다.
"""
from __future__ import annotations

import json
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
KOREAN = {"bottle": "병", "cup": "컵", "wine glass": "잔", "bowl": "그릇", "fork": "포크", "knife": "칼",
          "spoon": "숟가락", "banana": "바나나", "apple": "사과", "sandwich": "샌드위치", "orange": "귤",
          "pizza": "피자", "donut": "도넛", "cake": "케이크", "hot dog": "핫도그", "backpack": "가방",
          "handbag": "손가방", "suitcase": "캐리어", "umbrella": "우산", "laptop": "노트북",
          "cell phone": "휴대폰", "book": "책", "mouse": "마우스", "keyboard": "키보드", "remote": "리모컨"}

BEAT_SEC = 10          # 바뀌지 않아도 10초마다 보낸다 (core는 1분 안 오면 끊김)
SMOOTH_FRAMES = 5      # 최근 5번 센 값의 가운데 값을 쓴다 (한 번 놓치거나 잘못 세서 숫자가 튀지 않게)
EMPTY_CHECK_SEC = 30   # 모두 나가고 30초 뒤에 정리 확인 (문 닫고 나가는 시간)
SAME_PLACE_IOU = 0.3   # 깨끗할 때 본 물건과 이만큼 겹치면 "원래 있던 것"으로 본다
FALL_SEC = 15          # 누운 자세로 이만큼 거의 안 움직이면 "쓰러짐 의심" (시연용 값, FALL_SEC로 바꿈)
LYING_RATIO = 1.2      # 사람 상자가 가로/세로 이보다 넓으면 누운 자세로 본다
STILL_IOU = 0.5        # 처음 누운 자리와 이만큼 겹치면 "안 움직임"
MISS_SEC = 3           # 누운 사람을 이 시간 넘게 연달아 못 찾아야 초기화·해제 (한 장 놓침으로 튀지 않게)


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


def nms(dets, threshold=0.45):
    """같은 물건을 여러 상자로 잡은 걸 하나로 줄인다 (점수 높은 것만 남김)."""
    kept = []
    for d in sorted(dets, key=lambda d: d.score, reverse=True):
        if all(k.label != d.label or iou(k.box, d.box) < threshold for k in kept):
            kept.append(d)
    return kept


def parse_yolo(output, scale_x, scale_y, conf=0.4):
    """YOLOv8 ONNX 출력 (1, 84, N)을 Detection 목록으로 바꾼다.

    84 = 상자 4개 값(가운데 x, 가운데 y, 너비, 높이) + 80종 점수. scale은 모델 입력(640) → 원래 화면 크기 비율.
    """
    import numpy as np
    rows = np.asarray(output)[0].T  # (N, 84)
    scores = rows[:, 4:]
    best = scores.argmax(axis=1)
    best_score = scores[np.arange(len(rows)), best]
    dets = []
    for i in np.nonzero(best_score >= conf)[0]:
        cx, cy, w, h = rows[i, :4]
        box = (float((cx - w / 2) * scale_x), float((cy - h / 2) * scale_y), float(w * scale_x), float(h * scale_y))
        dets.append(Detection(COCO_NAMES[int(best[i])], float(best_score[i]), box))
    return nms(dets)


# ---------- 찾는 기계 두 가지 (같은 detect(frame) 모양) ----------

class YoloDetector:
    """YOLOv8 ONNX 모델로 사람 + 물건 80종을 찾는다.

    onnxruntime이 있으면 그걸로 돌린다(젯슨 기본 OpenCV 4.6은 YOLOv8 모델을 못 읽음).
    없으면 OpenCV DNN(4.7 이상)으로 돌린다. 앞처리(크기 맞추기)는 둘 다 OpenCV로 한다.
    """
    finds_objects = True

    def __init__(self, model_path, size=640):
        import cv2
        self.cv2 = cv2
        self.size = size
        self.session = self.net = None
        try:
            import onnxruntime as ort
            providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
                         if p in ort.get_available_providers()]
            self.session = ort.InferenceSession(model_path, providers=providers)
            self.input_name = self.session.get_inputs()[0].name
            print("인원 세기: onnxruntime", providers[0])
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
        return parse_yolo(output, w / self.size, h / self.size)


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


class RoomWatcher:
    """찾은 목록을 받아서 (1) core에 보낼 재실 이벤트 (2) 정리 확인 결과를 만든다."""

    def __init__(self, room_id, check_objects=True, now=None):
        self.room_id = room_id
        self.check_objects = check_objects
        self.counts = deque(maxlen=SMOOTH_FRAMES)
        self.sent_people = None      # 마지막으로 보낸 사람 수
        self.sent_at = None          # 마지막으로 보낸 시각
        self.had_people = False      # 정리 확인 전에 사람이 있었는지
        self.empty_since = None      # 0명이 된 시각
        self.baseline = None         # 깨끗한 빈 방에서 본 물건 (처음 빈 방 또는 정리 확인 뒤)

    def people(self, dets):
        return sum(1 for d in dets if d.label == "person")

    def update(self, dets, now, pir=None, now_iso=None):
        """한 번 센 결과를 넣는다. 보낼 재실 이벤트(dict)와 정리 확인 결과(dict)를 (event, check)로 돌려준다."""
        self.counts.append(self.people(dets))
        people = sorted(self.counts)[len(self.counts) // 2]  # 가운데 값

        event = None
        if people != self.sent_people or self.sent_at is None or now - self.sent_at >= BEAT_SEC:
            event = self.event(people, pir, now_iso)
            self.sent_people, self.sent_at = people, now

        check = None
        if people > 0:
            self.had_people, self.empty_since = True, None
        else:
            if self.empty_since is None:
                self.empty_since = now
            if self.baseline is None and not self.had_people:
                self.baseline = self.objects(dets)  # 처음 켰을 때 빈 방 = 깨끗한 기준
            elif self.had_people and now - self.empty_since >= EMPTY_CHECK_SEC:
                check = self.check(dets, now_iso)
                self.had_people = False
        return event, check

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

    def alert_event(self, alert, pir=None):
        """이상 탐지를 바로 core에 남긴다. core에 알림 API가 없어서 재실 이벤트의 sources에 실어 보낸다
        (core는 sources를 그대로 기록). 쓰러짐 의심이면 사람이 있는 것이므로 occupied는 true."""
        people = self.sent_people or 0
        body = self.event(max(people, 1) if alert["status"] == "FALL_SUSPECTED" else people, pir, alert["checkedAt"])
        body["sources"]["alert"] = alert["status"]
        return body

    def objects(self, dets):
        return [d for d in dets if d.label in TRASH or d.label in BELONGINGS]

    def check(self, dets, now_iso):
        """빈 방에서 깨끗할 때 없던 물건만 골라 결과를 만든다."""
        if not self.check_objects:
            return None
        base = self.baseline or []
        new = [d for d in self.objects(dets)
               if not any(b.label == d.label and iou(b.box, d.box) >= SAME_PLACE_IOU for b in base)]
        trash, left = {}, {}
        for d in new:
            bucket = trash if d.label in TRASH else left
            bucket[d.label] = bucket.get(d.label, 0) + 1
        status = "LEFT_ITEMS" if left else "NEEDS_CLEANING" if trash else "CLEAN"
        if status == "CLEAN":
            self.baseline = self.objects(dets)  # 깨끗하면 지금 모습을 새 기준으로
        return {
            "roomId": self.room_id,
            "status": status,
            "trash": trash,
            "leftItems": left,
            "checkedAt": now_iso or datetime.now(KST).isoformat(timespec="seconds"),
        }


class FallWatcher:
    """이상 탐지: 사람이 누운 자세로 FALL_SEC 넘게 거의 안 움직이면 한 번 알린다 (8차 회의 멘토 "쓰러짐 감지").

    사람 상자가 세로보다 가로가 넓으면 누운 자세로 본다. 처음 누운 자리를 기준으로 잡고,
    그 자리에서 크게 벗어나지 않은 채 FALL_SEC가 지나면 FALL_SUSPECTED, 다시 일어나거나 사라지면
    FALL_CLEARED를 돌려준다. 카메라를 위에서 비스듬히 달면 서 있는 사람도 넓게 보일 수 있어서
    LYING_RATIO는 설치 각도에 맞게 바꾼다. 사진은 쓰지 않고 상자 크기만 본다.
    """

    def __init__(self, room_id, hold_sec=FALL_SEC):
        self.room_id = room_id
        self.hold_sec = hold_sec
        self.anchor = None   # 처음 누운 자리 (x, y, w, h)
        self.since = None    # 그때 시각
        self.alerted = False
        self.missing_since = None  # 누운 사람을 못 찾기 시작한 시각

    @staticmethod
    def lying(dets):
        return [d for d in dets if d.label == "person" and d.box[2] > LYING_RATIO * d.box[3]]

    def update(self, dets, now, now_iso=None):
        """한 번 찾은 결과를 넣는다. 알릴 일이 생기면 결과(dict), 아니면 None."""
        lying = self.lying(dets)
        still = None
        if self.anchor is not None and lying:
            best = max(lying, key=lambda d: iou(d.box, self.anchor))
            still = best if iou(best.box, self.anchor) >= STILL_IOU else None
        if still is not None:
            self.missing_since = None
            if not self.alerted and now - self.since >= self.hold_sec:
                self.alerted = True
                return self.result("FALL_SUSPECTED", now, now_iso)
            return None
        if self.anchor is not None and not lying:  # 못 찾음: 잠깐이면 기다린다 (YOLO는 누운 사람을 가끔 놓친다)
            if self.missing_since is None:
                self.missing_since = now
            if now - self.missing_since < MISS_SEC:
                return None
        self.missing_since = None
        cleared = self.result("FALL_CLEARED", now, now_iso) if self.alerted else None
        self.anchor = max(lying, key=lambda d: d.score).box if lying else None
        self.since = now if lying else None
        self.alerted = False
        return cleared

    def result(self, status, now, now_iso):
        return {
            "roomId": self.room_id,
            "status": status,
            "lyingSec": int(now - self.since),
            "checkedAt": now_iso or datetime.now(KST).isoformat(timespec="seconds"),
        }


def fall_message(alert):
    if alert["status"] == "FALL_SUSPECTED":
        return f"⚠ 이상 탐지 {alert['roomId']}번 방: 사람이 {alert['lyingSec']}초 넘게 누운 채 안 움직여요 (쓰러짐 의심)"
    return f"이상 탐지 {alert['roomId']}번 방: 다시 움직임 (해제)"


def check_message(check):
    if check["status"] == "CLEAN":
        return f"정리 확인 {check['roomId']}번 방: 깨끗함"
    parts = []
    if check["leftItems"]:
        parts.append("두고 간 물건 " + describe(check["leftItems"]))
    if check["trash"]:
        parts.append("치울 것 " + describe(check["trash"]))
    return f"정리 확인 {check['roomId']}번 방: " + " / ".join(parts)


# ---------- 카메라 루프 ----------

def open_camera(device):
    import cv2
    cam = cv2.VideoCapture(int(device) if str(device).isdigit() else device)
    if not cam.isOpened():
        raise SystemExit(f"방 안 웹캠({device})을 열 수 없어요. ls /dev/video* 로 번호를 확인하세요.")
    return cam


def make_detector(model_path):
    if model_path:
        print("인원 세기: YOLO 모델", model_path)
        return YoloDetector(model_path)
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


def run_room_camera(watcher, cam, detector, push, pir_now=lambda: None, log_path="room_checks.jsonl",
                    every=1.0, clock=time.monotonic, sleep=time.sleep, fall=None, on_frame=lambda: None,
                    warn_every=10.0):
    """카메라에서 1초마다 한 장씩 세서 watcher에 넣는다. 사진은 저장하지 않는다.

    on_frame: 사진을 잘 읽을 때마다 부른다 (게이트웨이가 카메라가 살아 있는지 본다).
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
            on_frame()
            try:
                dets = detector.detect(frame)
                del frame  # 사진은 바로 버린다
                event, check = watcher.update(dets, clock(), pir_now())
                if event:
                    print(f"인원 {watcher.room_id}번 방: {event['sources']['people']}명")
                    push(event)
                if check:
                    print(check_message(check))
                    safe_log(log_path, check)
                alert = fall.update(dets, clock()) if fall else None
                if alert:
                    print(fall_message(alert))
                    push(watcher.alert_event(alert, pir_now()))  # core에 먼저 알리고 기록은 나중에
                    safe_log(log_path, alert)
            except Exception as e:  # 한 장에서 오류가 나도 카메라 스레드는 계속 돈다
                print("방 안 카메라 처리 오류(계속 돌아요):", repr(e))
        sleep(max(0.0, every - (clock() - started)))
