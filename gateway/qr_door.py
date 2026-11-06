"""문 앞 웹캠으로 QR 입실 (FR-ACC-001의 다른 입력 방식)

학생 폰 화면의 QR을 문 앞 웹캠이 읽어서, 키패드와 같은 core API(POST /api/access/verify)로 확인한다.
결과는 우노에 OK/NO로 보내 같은 LED(초록/빨강)를 켠다. 판단은 core만 한다 (키패드와 같음).

QR 안에 넣는 글자 (둘 중 하나):
  123456                 입실 번호 6자리만. 방은 QR_ROOM_ID(기본 1)
  MSPACE:1:123456        방 번호까지. 다른 방 QR이면 서버에 묻지 않고 거부

지금은 예약 때 받은 6자리 입실 번호를 QR로 띄운다 (core를 안 바꾸고 바로 시연 가능).
나중에 core가 "30초마다 바뀌는 QR 코드"를 주면 parse_qr()만 바꾸면 된다.

사진·영상은 저장하지도, 보내지도 않는다. 읽은 글자만 쓴다.
"""
from __future__ import annotations

import re
import time
import uuid

QR_RE = re.compile(r"(?:MSPACE:([0-9]{1,4}):)?([0-9]{6})")  # 앞부분(방 번호)은 있어도 되고 없어도 된다
SAME_QR_IGNORE_SEC = 5  # 같은 QR을 카메라 앞에 계속 들고 있으면 5초 동안은 다시 묻지 않는다


def parse_qr(text, room_id):
    """QR 글자를 (방 번호, 입실 번호)로 바꾼다. 우리 형식이 아니거나 다른 방 QR이면 None."""
    m = QR_RE.fullmatch(text.strip()) if isinstance(text, str) else None
    if not m:
        return None
    room = int(m.group(1)) if m.group(1) else room_id
    if room != room_id:  # 다른 방 QR은 이 문에서 쓸 수 없다
        return None
    return room, m.group(2)


class QrDoor:
    """읽은 QR 글자 → core 확인 → 우노에 줄 답(OK/NO). 카메라와 떼어 놓아서 카메라 없이 테스트할 수 있다."""

    def __init__(self, verifier, room_id, clock=time.monotonic):
        self.verifier = verifier
        self.room_id = room_id
        self.clock = clock
        self.last_text = None
        self.last_at = -SAME_QR_IGNORE_SEC

    def handle(self, text):
        """QR 글자 하나를 처리한다. 무시할 때(같은 QR 반복, 빈 글자)는 None."""
        if not text:
            return None
        now = self.clock()
        if text == self.last_text and now - self.last_at < SAME_QR_IGNORE_SEC:
            return None  # 같은 QR을 계속 비추는 중
        self.last_text, self.last_at = text, now
        parsed = parse_qr(text, self.room_id)
        if parsed is None:
            print("QR: 우리 입실 QR이 아님 (또는 다른 방), 거부")
            return b"NO\n"
        room, code = parsed
        body = {
            "eventId": str(uuid.uuid4()),  # 찍을 때마다 새 이벤트. 재시도는 HttpVerifier가 같은 body로 한다
            "roomId": room,
            "deviceId": f"qr-{room}",      # 키패드(keypad-1)와 구분
            "code": code,
        }
        res = self.verifier.verify(body)
        res = res if isinstance(res, dict) else {}
        ok = res.get("result") == "OPENED"
        reason = "" if ok else res.get("reason", "서버 응답 없음")
        print("QR 출입", body["eventId"], "->", "열림" if ok else f"거부 ({reason})")
        return b"OK\n" if ok else b"NO\n"


def open_camera(device):
    """웹캠을 연다. device는 0, 1 같은 번호나 /dev/video0 같은 경로. OpenCV는 젯슨에 기본으로 깔려 있다."""
    import cv2  # 카메라를 쓸 때만 필요해서 여기서 불러온다 (테스트·CI에는 OpenCV가 없어도 됨)
    cam = cv2.VideoCapture(int(device) if str(device).isdigit() else device)
    if not cam.isOpened():
        raise SystemExit(f"웹캠({device})을 열 수 없어요. ls /dev/video* 로 번호를 확인하세요.")
    return cam, cv2.QRCodeDetector()


def run_camera(door, device, on_reply, frames=None):
    """카메라에서 계속 QR을 찾아 door.handle()에 넘기고, 답(OK/NO)을 on_reply로 넘긴다.

    frames: 테스트용. (ok, frame)을 차례로 돌려주는 가짜 카메라와 detector를 넘길 수 있다.
    """
    cam, detector = frames if frames is not None else open_camera(device)
    print(f"QR 카메라 켜짐 ({device}), {door.room_id}번 방")
    while True:
        ok, frame = cam.read()
        if not ok:
            time.sleep(0.5)  # 카메라가 잠깐 끊겨도 멈추지 않는다
            continue
        try:
            text, _, _ = detector.detectAndDecode(frame)
            reply = door.handle(text)
            if reply:
                on_reply(reply)
        except Exception as e:  # 한 장에서 오류가 나도 QR 스레드는 계속 돈다
            print("QR 처리 오류(계속 돌아요):", repr(e))
