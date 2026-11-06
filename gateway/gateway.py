"""M-SPACE 게이트웨이 (노트북/젯슨용)

우노가 USB 시리얼로 보내는 키패드 번호(JSON 한 줄)를 읽어서 서버에 물어보고,
결과를 우노에 OK/NO로 돌려준다 (FR-ACC-001).
재실 센서(PIR·조도) 줄은 core 형식(docs/sensor-events-api.md)으로 바꿔 화면에 찍고,
SEND_SENSORS=1이면 core POST /api/sensor-events로 보낸다 (FR-DEV-101). 키패드를 막지 않게 뒤에서(스레드) 보낸다.

출입 확인 API (core 담당 지영과 맞춘 형식):
  요청: {"eventId": "uuid", "roomId": 1, "deviceId": "keypad-1", "code": "123456"}
        헤더 Authorization: Bearer <장치 토큰>
  응답: {"result": "OPENED"}  또는  {"result": "DENIED", "reason": "WRONG_CODE"}

서버가 아직 없으면 DRY_RUN=1 로 실행하면 보낼 내용만 화면에 찍는다.

구조 (역할별로 나눠서 하나만 바꿔 끼울 수 있게):
  Config / load_config()  : 설정값을 한곳에 모은다 (환경변수에서 읽음)
  find_port()             : 우노가 꽂힌 USB 포트를 찾는다
  DryRunVerifier          : 서버 없이 연습용으로 답한다 (123456만 통과)
  HttpVerifier            : 진짜 서버에 묻는다 (토큰, 재시도)
  SensorSender            : 재실 이벤트를 줄 세워 뒤에서 core로 보낸다 (재시도, 끊겨도 모아 둠)
  handle_occupancy()      : 재실 센서 줄을 core 형식으로 바꿔 화면에 찍고 SensorSender에 넘긴다
  handle_line()           : 우노가 보낸 한 줄을 받아 우노에 돌려줄 답(OK/NO)을 만든다
  main()                  : 위 부품을 조립해서 시리얼을 계속 읽는다
  qr_door.py (QR_CAMERA)  : 문 앞 웹캠으로 QR 입실. 같은 확인기로 묻고 결과를 우노 LED로 보낸다
  room_camera.py (ROOM_CAMERA) : 방 안 웹캠으로 인원 세기 + 퇴실 뒤 정리 확인. 사진은 저장·전송 안 함

설치:  pip3 install pyserial requests
실행:  DRY_RUN=1 python3 gateway.py
       SERVER=https://dev-mspace.java21.net DEVICE_TOKEN=<장치 토큰> python3 gateway.py
       (http://는 localhost 테스트에서만. 토큰이 실리므로 원격 서버는 https)
       재실 센서도 보내려면 앞에 SEND_SENSORS=1 을 붙인다 (core에 /api/sensor-events가 올라간 뒤)
       QR 입실도 켜려면 QR_CAMERA=0 (웹캠 번호)을 붙인다. 방 번호는 QR_ROOM_ID (기본 1)
       인원 세기를 켜려면 ROOM_CAMERA=1 (방 안 웹캠 번호), YOLO_MODEL=yolov8n.onnx. 방 번호는 ROOM_CAMERA_ROOM (기본 1)
"""
from __future__ import annotations

import glob
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests
import serial

RETRIES = 2  # 서버 전송 실패 시 최대 시도 횟수
CONNECT_TIMEOUT = 1  # 서버에 연결될 때까지 기다리는 시간(초)
TIMEOUT = 2          # 연결된 뒤 서버 답을 기다리는 시간(초)
# requests는 연결·답 시간을 따로 잰다. 최악 (1+2)초×2번 + 1초 쉼 = 7초. 우노는 9초까지 기다린다(WAIT_MAX_MS)
# eventId를 UUID로 바꿀 때 쓰는 고정 이름공간. 바꾸면 같은 이벤트가 다른 UUID가 되므로 고정해 둔다
EVENT_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "mspace-device")
# 우노가 잡히는 포트 이름들. 리눅스: ttyACM(정품), ttyUSB(호환 보드) / 맥: cu.usbmodem(정품), cu.usbserial·cu.wchusbserial(호환 보드)
KST = timezone(timedelta(hours=9))  # 한국 시각(+09:00). core와 맞춘 시각 형식
PORT_PATTERNS = ["/dev/ttyACM*", "/dev/ttyUSB*", "/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*"]


# ---------- 설정 ----------

@dataclass(frozen=True)
class Config:
    """게이트웨이 설정값 묶음. 코드를 고치지 않고 실행할 때 환경변수로 바꾼다."""
    server: str = "http://localhost:8080"  # 예약 서버(core) 주소
    dry_run: bool = False                  # True면 서버에 안 보내고 화면에만 찍는다 (서버 없이 테스트용)
    device_token: str | None = None        # 장치 토큰. 서버가 "등록된 키패드"인지 확인하는 값. 저장소에 넣지 않고 .env에만 둔다
    port: str | None = None                # 우노가 꽂힌 USB 포트. 못 찾으면 None
    send_sensors: bool = False             # True면 재실 이벤트도 core로 보낸다 (core에 센서 API가 올라간 뒤 켠다)
    qr_camera: str | None = None           # QR 입실용 웹캠 (0, 1 또는 /dev/video0). 비우면 QR 끔
    qr_room_id: int = 1                    # QR 입실을 받는 방 번호
    room_camera: str | None = None         # 방 안 웹캠 (인원 세기·정리 확인). 비우면 끔
    room_camera_room: int = 1              # 방 안 웹캠이 있는 방 번호
    preview_port: int | None = None        # 방 안 카메라 화면을 브라우저로 보기 (예: 8090). 없으면 끔
    preview_host: str = "127.0.0.1"        # 화면 보기를 열 주소. 맥에서 보려면 Tailscale IP (전체 공개 0.0.0.0은 피함)
    yolo_model: str | None = None          # YOLO ONNX 모델 파일. 없으면 사람만 세는 기본 방식(HOG)
    room_items: dict = field(default_factory=dict)  # 방 비품 {"remote": 1}: 그 개수까지는 있어도 없어도 괜찮음
    desk_area: tuple | None = None         # 책상 부분 (x1, y1, x2, y2) 화면 비율. 정리 확인 때 책상만 확대해서 한 번 더 본다
    baseline_file: str | None = "room_baseline.json"  # 깨끗한 방 기준을 적어 두는 파일. BASELINE_FILE=off면 메모리에만


def find_port():
    """우노가 꽂힌 포트를 찾는다. PORT_PATTERNS 순서대로 처음 찾은 것, 없으면 None."""
    for pattern in PORT_PATTERNS:
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


def _is_number(text):
    """0-9로만 된 글자인지 (², ١ 같은 숫자 모양 글자는 int()가 못 바꿔서 뺀다)."""
    return text.isascii() and text.isdigit()


def load_config(env=None):
    """환경변수에서 설정을 읽어 Config로 만든다. PORT를 안 정하면 find_port()로 찾는다."""
    env = os.environ if env is None else env
    return Config(
        server=env.get("SERVER", Config.server),
        dry_run=env.get("DRY_RUN") == "1",
        device_token=env.get("DEVICE_TOKEN"),
        port=env.get("PORT") or find_port(),
        send_sensors=env.get("SEND_SENSORS") == "1",
        qr_camera=env.get("QR_CAMERA") or None,
        qr_room_id=int(env["QR_ROOM_ID"]) if _is_number(env.get("QR_ROOM_ID", "")) else Config.qr_room_id,
        room_camera=env.get("ROOM_CAMERA") or None,
        room_camera_room=(int(env["ROOM_CAMERA_ROOM"]) if _is_number(env.get("ROOM_CAMERA_ROOM", ""))
                          else Config.room_camera_room),
        yolo_model=env.get("YOLO_MODEL") or None,
        preview_port=int(env["PREVIEW_PORT"]) if _is_number(env.get("PREVIEW_PORT", "")) else None,
        preview_host=env.get("PREVIEW_HOST") or Config.preview_host,
        room_items=parse_room_items(env.get("ROOM_ITEMS", "")),
        desk_area=parse_desk_area(env.get("DESK_AREA", "")),
        baseline_file=(None if env.get("BASELINE_FILE") == "off"
                       else env.get("BASELINE_FILE") or Config.baseline_file),
    )


def parse_room_items(text):
    """ROOM_ITEMS="remote:1,book:3" → {"remote": 1, "book": 3}. 이상한 조각은 건너뛴다."""
    items = {}
    for part in text.split(","):
        label, _, n = part.strip().partition(":")
        if label.strip() and _is_number(n.strip()):
            items[label.strip()] = int(n.strip())
    return items


def parse_desk_area(text):
    """DESK_AREA="0.25,0.22,0.64,0.83" → (0.25, 0.22, 0.64, 0.83). 화면 왼쪽 위 x·y, 오른쪽 아래 x·y 비율(0~1).

    비우면 None(책상 확대 안 함). 틀리면 알려 주고 None.
    """
    if not text.strip():
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in text.split(","))
    except ValueError:
        x1 = y1 = x2 = y2 = -1.0
    if 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1:
        return (x1, y1, x2, y2)
    print(f"DESK_AREA가 이상해서 책상 확대는 꺼요: {text!r} (예: 0.25,0.22,0.64,0.83 = 왼쪽 위 x,y, 오른쪽 아래 x,y 비율)")
    return None


def is_loopback(url):
    """주소가 내 컴퓨터(localhost·127.0.0.1·::1)인지. 글자 앞부분이 아니라 실제 주소로 판단한다."""
    host = urlsplit(url).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:  # 이름(예: core.example.com)은 loopback으로 보지 않는다
        return False


def is_safe_server(server):
    """장치 토큰을 보내도 되는 주소인지. https는 괜찮고, http는 내 컴퓨터일 때만."""
    parts = urlsplit(server)
    if not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and is_loopback(server)


# ---------- 번호 확인 (verifier): 같은 모양 verify(body) 두 개 ----------

class DryRunVerifier:
    """서버 없이 연습할 때 쓰는 가짜 확인기. 123456만 통과시킨다."""

    def verify(self, body):
        print("[DRY_RUN] POST /api/access/verify", body["eventId"])  # 번호(code)는 비밀이라 로그에 남기지 않는다
        if body.get("code") == "123456":
            return {"result": "OPENED"}
        return {"result": "DENIED", "reason": "WRONG_CODE"}


class HttpVerifier:
    """진짜 서버(core)에 번호를 묻는 확인기. 응답(JSON)을 돌려주고, 끝내 실패하면 None.

    같은 eventId로 다시 보내기 때문에, 서버가 eventId로 중복을 걸러야 한 번만 저장된다.
    """

    def __init__(self, server, token, retries=RETRIES):
        self.url = server + "/api/access/verify"
        self.headers = {"Authorization": f"Bearer {token}"}  # 장치 토큰을 같이 보낸다
        self.retries = retries
        # 내 컴퓨터로 보낼 때는 프록시(HTTP_PROXY 설정)를 거치지 않는다. 토큰·번호가 밖으로 새지 않게
        self.proxies = {"http": None, "https": None} if is_loopback(self.url) else None

    def verify(self, body):
        for attempt in range(1, self.retries + 1):  # attempt = 지금 몇 번째 시도인지 (1, 2)
            try:
                r = requests.post(self.url, json=body, timeout=(CONNECT_TIMEOUT, TIMEOUT), headers=self.headers,
                                  proxies=self.proxies, allow_redirects=False)  # 연결 1초, 답 2초 안에 안 오면 실패
                if 300 <= r.status_code < 400:  # 다른 주소로 넘기라는 답(리디렉션)은 따라가지 않는다. 번호가 http로 샐 수 있어서
                    print(f"서버가 다른 주소로 넘김 ({r.status_code}), 따라가지 않음")
                    return None
                if 400 <= r.status_code < 500:  # 4xx(토큰·형식 오류)는 다시 보내도 결과가 같아서 바로 거부한다
                    print(f"서버가 요청을 거부함 ({r.status_code}), 다시 보내지 않음")
                    return None
                r.raise_for_status()  # 5xx(서버 오류)는 실패로 처리해서 다시 보낸다
                return r.json() if r.content else {}
            except requests.RequestException as e:  # e = 실패 이유 (연결 끊김, 시간 초과, 5xx)
                print(f"전송 실패 {attempt}/{self.retries}: {e}")
                if attempt < self.retries:  # 마지막 실패 뒤에는 기다리지 않고 바로 거부한다
                    time.sleep(attempt)  # 1초 쉬고 재시도 (시도 횟수를 늘리면 2초, 3초…로 점점 길어진다)
        return None


# ---------- 재실 이벤트 전송 (ESP32 sender.cpp와 같은 규칙) ----------

SENSOR_QUEUE_MAX = 360   # 끊긴 동안 모아 둘 이벤트 수 (10초마다 1개 → 약 1시간). 넘치면 가장 오래된 것부터 버린다
SENSOR_DROP = {400, 409, 413, 422}  # 이 이벤트 자체의 형식 문제 → 다시 보내도 같아서 버린다
RETRY_MIN, RETRY_MAX = 1, 30        # 실패하면 1초, 2초, 4초 … 최대 30초 간격으로 다시


class SensorSender:
    """재실 이벤트를 줄(큐)에 세워 두고 앞에서부터 core로 보낸다.

    - 2xx(STORED/DUPLICATE)면 성공, 400·409·413·422는 그 이벤트만 버린다
    - 401·403·404·408·429·5xx·연결 실패는 버리지 않고 간격을 늘려 같은 eventId로 다시 보낸다
      (토큰·서버 설정을 고치는 동안 기록이 사라지지 않게)
    - 키패드 응답이 늦어지지 않게 별도 스레드에서 보낸다
    """

    def __init__(self, server, token, path="/api/sensor-events", drop=SENSOR_DROP, name="센서 전송"):
        self.url = server + path
        self.drop = drop
        self.name = name
        self.headers = {"Authorization": f"Bearer {token}"}
        self.queue = deque(maxlen=SENSOR_QUEUE_MAX)  # 꽉 차면 append가 가장 오래된 것을 밀어낸다
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.backoff = RETRY_MIN

    def push(self, body):
        with self.lock:
            self.queue.append(body)
        self.wake.set()

    def step(self):
        """맨 앞 하나를 보내 본다. 반환: 다음 시도까지 기다릴 초 (0이면 바로 다음 것, None이면 큐가 비었음)."""
        with self.lock:
            if not self.queue:
                return None
            body = self.queue[0]
        try:
            r = requests.post(self.url, json=body, timeout=(CONNECT_TIMEOUT, TIMEOUT), headers=self.headers,
                              allow_redirects=False)  # 리디렉션을 따라가면 토큰이 다른 곳으로 갈 수 있다
            status = r.status_code
        except requests.RequestException as e:
            status = None
            print(f"[{self.name}] 연결 실패, 다시 보내요: {e.__class__.__name__}")
        if status is not None and (200 <= status < 300 or status in self.drop):
            if status in self.drop:
                print(f"[{self.name}] 서버가 이것을 거부함({status}). 다시 보내도 같아서 이것만 버려요")
            with self.lock:
                if self.queue and self.queue[0] is body:
                    self.queue.popleft()
            self.backoff = RETRY_MIN
            return 0
        if status is not None:
            hint = {401: "토큰 확인", 403: "토큰·방 번호 확인", 404: "서버에 센서 API가 아직 없음"}.get(status, "서버 문제")
            print(f"[{self.name}] 막힘({status}, {hint}). {self.backoff}초 뒤 다시, 대기 {len(self.queue)}개")
        wait = self.backoff
        self.backoff = min(self.backoff * 2, RETRY_MAX)
        return wait

    def run_forever(self):
        while True:
            wait = self.step()
            if wait is None:
                self.wake.wait()
                self.wake.clear()
            elif wait:
                time.sleep(wait)

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True).start()
        return self



# ---------- 퇴실 알기 (정리 확인을 예약이 끝났을 때만 하려고) ----------

POLL_SEC = 10  # 10초마다 서버에 "지금 이 방 예약 상태"를 묻는다 (core #81 GET /api/rooms/{id}/current)
TIDY_DROP = SENSOR_DROP | {404}  # 404 = 다른 방 예약이거나 서버에 아직 API가 없음 → 다시 보내도 같다


class CheckoutPoller:
    """서버에 방의 예약 상태를 물어서, 방금 끝난 예약(lastEnded)이 바뀌면 on_checkout(예약 번호)을 부른다.

    켠 직후 처음 본 lastEnded는 이미 지난 일이라 기억만 하고 부르지 않는다.
    서버에 못 닿으면 기다렸다 다시 묻는다 (그동안 정리 확인은 미뤄질 뿐 틀리지 않는다).
    """

    def __init__(self, server, token, room, on_checkout, every=POLL_SEC, get=None):
        self.url = f"{server}/api/rooms/{room}/current"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.on_checkout = on_checkout
        self.every = every
        self.get = get or requests.get
        self.known = False   # 처음 한 번 읽었는지
        self.last = None     # 마지막으로 본 끝난 예약 번호
        self.failing = False

    def poll_once(self):
        try:
            r = self.get(self.url, headers=self.headers, timeout=(CONNECT_TIMEOUT, TIMEOUT), allow_redirects=False)
            if r.status_code != 200:
                raise ValueError(f"응답 {r.status_code}")
            ended = (r.json().get("lastEnded") or {}).get("reservationId")
        except (requests.RequestException, ValueError) as e:
            if not self.failing:  # 끊길 때 한 번만 알린다
                print(f"[퇴실 확인] 서버에 예약 상태를 못 물어요, 다시 시도해요: {e}")
            self.failing = True
            return None
        if self.failing:
            print("[퇴실 확인] 서버 연결 다시 됨")
        self.failing = False
        if not self.known:
            self.known, self.last = True, ended
            return None
        if ended is not None and ended != self.last:
            self.last = ended
            print(f"[퇴실 확인] 예약 {ended} 퇴실 → 방이 비면 정리 확인")
            self.on_checkout(ended)
            return ended
        return None

    def run_forever(self):
        while True:
            self.poll_once()
            time.sleep(self.every)

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True).start()
        return self


def make_verifier(config):
    """설정에 맞는 확인기를 고른다. 나머지 코드는 어느 쪽인지 몰라도 된다."""
    if config.dry_run:
        return DryRunVerifier()
    return HttpVerifier(config.server, config.device_token)


# ---------- 우노가 보낸 한 줄 처리 ----------

def to_uuid(raw_id):
    """우노가 만든 eventId("방-부팅번호-순번")를 서버 형식인 UUID로 바꾼다.

    uuid5는 같은 입력이면 항상 같은 UUID가 나온다. 그래서 재전송해도 eventId가 같아
    서버가 중복을 걸러낼 수 있다 (랜덤 uuid4를 쓰면 재전송마다 달라져서 안 된다).
    """
    return str(uuid.uuid5(EVENT_NS, raw_id))


EVENT_ID_RE = re.compile(r"[0-9]+-[0-9]+-[0-9]+")  # 우노 형식: 방번호-부팅번호-순번
CODE_RE = re.compile(r"[0-9]{6}")                   # 숫자 6자리 (0~9만. ² 같은 다른 숫자 글자는 안 됨)


def valid_keypad(event):
    """우노가 보내는 키패드 줄 형식인지 확인한다. 형식이 다르면 False (게이트웨이가 멈추지 않게)."""
    eid, room, code = event.get("eventId"), event.get("roomId"), event.get("code")
    return (
        isinstance(eid, str) and EVENT_ID_RE.fullmatch(eid) is not None
        and isinstance(room, int) and not isinstance(room, bool)  # True/False도 int로 취급되므로 따로 막는다
        and isinstance(code, str) and CODE_RE.fullmatch(code) is not None
    )


def handle_keypad(event, verifier):
    """키패드 줄: 서버에 번호를 묻고 우노에 돌려줄 답(OK/NO)을 만든다."""
    if not valid_keypad(event):  # 형식이 어긋난 줄은 건너뛴다 (우노 코드와 같은 형식만 받는다)
        print("형식이 맞지 않는 줄, 건너뜀")
        return None
    # 출입 확인은 지영과 맞춘 4개 필드만 보낸다
    body = {
        "eventId": to_uuid(event["eventId"]),  # 서버 형식(UUID)으로 바꾼다
        "roomId": event["roomId"],
        "deviceId": f"keypad-{event['roomId']}",  # 방마다 키패드 1개라서 방 번호로 이름을 붙인다
        "code": event["code"],
    }
    res = verifier.verify(body)  # res = 서버의 판단 결과. 예: {"result": "OPENED"}
    res = res if isinstance(res, dict) else {}  # 응답이 없거나 이상한 형식이면 빈 응답으로 본다
    ok = res.get("result") == "OPENED"  # 열림일 때만 True. 서버 응답이 없으면 안전하게 거부(False)
    reason = "" if ok else res.get("reason", "서버 응답 없음")  # 거부 이유. 나중에 5회 잠금 기능에 쓴다
    print("출입", body["eventId"], "->", "열림" if ok else f"거부 ({reason})")
    return b"OK\n" if ok else b"NO\n"


def is_int(value):
    """진짜 정수인지. True/False도 int로 취급되므로 따로 막는다."""
    return isinstance(value, int) and not isinstance(value, bool)


def valid_occupancy(event):
    """우노가 보내는 재실 센서 줄 형식인지 확인한다. pir은 0/1, light는 0~1023."""
    eid, room, pir, light = (event.get(k) for k in ("eventId", "roomId", "pir", "light"))
    return (
        isinstance(eid, str) and EVENT_ID_RE.fullmatch(eid) is not None
        and is_int(room)
        and is_int(pir) and pir in (0, 1)
        and is_int(light) and 0 <= light <= 1023
    )


def occupancy_body(event, now=None):
    """우노 재실 줄을 core 형식(docs/sensor-events-api.md 2-1장)으로 바꾼다.

    우노에는 시계가 없어서 observedAt은 게이트웨이가 받은 시각(한국 시각)으로 붙인다.
    occupied는 지금은 PIR 하나로 정한다. 레이더가 붙으면 "하나라도 있음이면 있음"(FR-DEV-103)으로 합친다.
    """
    now = now or datetime.now(KST)
    return {
        "eventId": to_uuid(event["eventId"]),
        "roomId": event["roomId"],
        "deviceId": f"sensor-{event['roomId']}",  # 방마다 센서 장치 1개라서 방 번호로 이름을 붙인다
        "kind": "OCCUPANCY",
        "occupied": event["pir"] == 1,
        "sources": {"pir": event["pir"] == 1, "light": event["light"]},  # core는 기록만 한다 (정확도 비교용)
        "observedAt": now.isoformat(timespec="seconds"),
    }


def handle_occupancy(event, verifier, sensors=None):
    """재실 센서 줄: core 형식으로 바꿔 화면에 찍고, sensors가 있으면 core로 보낼 줄에 넣는다. 우노에 돌려줄 답은 없다."""
    if not valid_occupancy(event):
        print("형식이 맞지 않는 센서 줄, 건너뜀")
        return None
    body = occupancy_body(event)
    state = "있음" if body["occupied"] else "없음"
    LAST_PIR[body["roomId"]] = (event["pir"], time.monotonic())  # 방 안 카메라가 있으면 카메라 이벤트에 같이 싣는다
    if camera_alive(body["roomId"]):  # 이 방은 카메라가 PIR까지 합쳐서 보낸다 (두 장치가 번갈아 다른 값을 보내지 않게)
        print(f"재실 {body['roomId']}번 방 PIR: {state} (카메라 이벤트에 합쳐 보냄)")
        return None
    print(f"재실 {body['roomId']}번 방: {state} (pir={event['pir']}, 조도={event['light']})")
    if isinstance(verifier, DryRunVerifier):  # 연습 모드에서는 core에 보낼 모양을 그대로 보여 준다
        print("[DRY_RUN] POST /api/sensor-events", json.dumps(body, ensure_ascii=False))
    elif sensors is not None:
        sensors.push(body)
    return None


LAST_PIR = {}      # 방 번호 → (우노 PIR 마지막 값 0/1, 받은 시각)
CAMERA_ROOMS = {}  # 방 번호 → 방 안 카메라가 마지막으로 사진을 잘 읽은 시각
PIR_FRESH_SEC = 30     # 우노 PIR이 이보다 오래되면 안 쓴다 (우노가 멈췄는데 "있음"으로 고정되지 않게)
CAMERA_FRESH_SEC = 30  # 카메라가 이보다 오래 사진을 못 읽으면 우노가 다시 재실을 보낸다 (core 끊김 판정 1분보다 짧게)


def camera_alive(room, now=None):
    """이 방의 카메라가 요즘 사진을 잘 읽고 있으면 True (그동안은 카메라가 PIR까지 합쳐 보낸다)."""
    seen = CAMERA_ROOMS.get(room)
    return seen is not None and (time.monotonic() if now is None else now) - seen < CAMERA_FRESH_SEC


def fresh_pir(room, now=None):
    """우노 PIR 마지막 값. PIR_FRESH_SEC 넘게 새 값이 없으면 None."""
    got = LAST_PIR.get(room)
    if got is None or (time.monotonic() if now is None else now) - got[1] >= PIR_FRESH_SEC:
        return None
    return got[0]


# sensorType별 처리기 표. 새 센서 줄은 여기에 한 줄씩 추가한다
HANDLERS = {
    "keypad": handle_keypad,
    "occupancy": handle_occupancy,
}


def handle_line(line, verifier, sensors=None):
    """우노가 보낸 한 줄을 처리하고, 우노에 돌려줄 값(b"OK\\n" 등)을 반환한다. 돌려줄 게 없으면 None."""
    line = line.strip()
    if not line.startswith("{"):  # JSON이 아닌 줄(빈 줄 등)은 건너뛴다
        return None
    try:
        event = json.loads(line)  # event = 한 줄을 딕셔너리로 바꾼 것 (eventId, roomId, sensorType ...)
    except json.JSONDecodeError:
        print("JSON 아님 (내용은 번호가 섞일 수 있어 남기지 않음)")
        return None
    sensor_type = event.get("sensorType")
    # 모르는 sensorType이면 None. 글자가 아닌 값(리스트 등)은 표에서 찾을 수 없어 오류가 나므로 먼저 거른다
    handler = HANDLERS.get(sensor_type) if isinstance(sensor_type, str) else None
    if handler is None:
        return None
    if handler is handle_occupancy:
        return handler(event, verifier, sensors)
    return handler(event, verifier)


# ---------- 실행 ----------

def main(config=None):
    config = config or load_config()
    cameras_only = not config.port and bool(config.qr_camera or config.room_camera)
    if not config.port and not cameras_only:
        raise SystemExit("우노를 못 찾았어요. USB 연결을 확인하거나 PORT=/dev/ttyACM0 처럼 지정하세요.")
    if not config.dry_run and not config.device_token:
        raise SystemExit("DEVICE_TOKEN이 없어요. .env에 넣고 불러오거나, 서버 없이 테스트하려면 DRY_RUN=1 로 실행하세요.")
    if not config.dry_run and not is_safe_server(config.server):
        raise SystemExit("원격 서버는 https://로만 연결해요 (장치 토큰이 실리므로). http://는 localhost 테스트에서만 써요.")
    print("연결:", config.port or "우노 없음 (카메라만)", "| 서버:", "DRY_RUN" if config.dry_run else config.server,
          "| 재실 전송:", "켬" if config.send_sensors and not config.dry_run else "끔",
          "| QR 입실:", f"켬 (카메라 {config.qr_camera}, {config.qr_room_id}번 방)" if config.qr_camera else "끔",
          "| 인원 세기:", f"켬 (카메라 {config.room_camera}, {config.room_camera_room}번 방)" if config.room_camera else "끔")
    verifier = make_verifier(config)
    sensors = None
    if config.send_sensors and not config.dry_run:
        sensors = SensorSender(config.server, config.device_token).start()
    ser = None
    if not cameras_only:
        ser = serial.Serial(config.port, 115200, timeout=1)  # ser = 우노와 연결된 시리얼 통로. 115200은 우노 코드와 같은 속도
        time.sleep(2)  # 우노는 시리얼 연결 시 재부팅되므로 켜질 때까지 기다린다
    write_lock = threading.Lock()  # 키패드 답과 QR 답이 동시에 우노로 가도 줄이 섞이지 않게

    def reply_to_uno(reply):
        if ser is None:  # 우노 없이 카메라만 시험할 때는 LED 대신 화면에
            print("우노 LED 대신:", "초록(열림)" if reply == b"OK\n" else "빨강(거부)")
            return
        with write_lock:
            ser.write(reply)  # 우노에 결과를 돌려줘서 LED를 켜게 한다

    if config.qr_camera:
        start_qr(config, verifier, reply_to_uno)
    if config.room_camera:
        start_room_camera(config, sensors)

    while ser is None:  # 우노가 없으면 카메라 스레드만 돌게 두고 기다린다 (Ctrl+C로 끝)
        time.sleep(1)
    while True:
        line = ser.readline().decode(errors="ignore")  # line = 우노가 보낸 한 줄 (글자로 변환, 깨진 글자는 무시)
        reply = handle_line(line, verifier, sensors)
        if reply:
            reply_to_uno(reply)


def camera_push(config, sensors):
    """방 안 카메라 이벤트를 어디로 보낼지 정한다: DRY_RUN이면 화면, SEND_SENSORS면 core, 아니면 화면만."""
    def push(body):
        if sensors is not None:
            sensors.push(body)
        else:
            prefix = "[DRY_RUN]" if config.dry_run else "[보내지 않음, SEND_SENSORS=1로 켜기]"
            print(prefix, "POST /api/sensor-events", json.dumps(body, ensure_ascii=False))
    return push


def tailscale_ip(run=subprocess.run, tries=12, wait=5.0, sleep=time.sleep):
    """`tailscale ip -4`로 내 Tailscale 주소를 찾는다. 막 켜졌을 땐 Tailscale이 늦게 붙어서 몇 번 기다린다."""
    for i in range(tries):
        try:
            out = run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
            ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
            if ip:
                return ip
        except (OSError, subprocess.SubprocessError):
            pass
        if i < tries - 1:
            sleep(wait)
    return None


def start_preview(port, host="127.0.0.1", find_tailscale=tailscale_ip):
    """PREVIEW_PORT가 있으면 브라우저로 카메라 화면을 보는 작은 서버를 켠다.

    PREVIEW_HOST=tailscale 이면 Tailscale 주소를 알아서 찾는다 (자동 실행 서비스에서 $(tailscale ip -4)를 못 써서).
    """
    if not port:
        return None
    if host == "tailscale":
        found = find_tailscale()
        if not found:
            print("Tailscale 주소를 못 찾아서 화면 보기는 젯슨 안에서만 열어요 (127.0.0.1)")
        host = found or "127.0.0.1"
    import preview
    try:
        return preview.Preview(port, host=host).start()
    except (OSError, OverflowError) as e:  # 번호가 겹치거나 범위 밖: 화면 보기만 끄고 카메라는 계속
        print(f"화면 보기를 못 켰어요 (포트 {port}: {e}). PREVIEW_PORT를 다른 번호로 바꿔 보세요 (예: 8090)")
        return None


def start_room_camera(config, sensors):
    """방 안 웹캠 인원 세기·정리 확인을 뒤에서(스레드) 켠다."""
    import room_camera  # 켤 때만 불러온다 (OpenCV는 젯슨에만 있으면 됨)
    room = config.room_camera_room
    CAMERA_ROOMS[room] = time.monotonic()  # 켤 때 한 번 잡고, 이후 사진을 읽을 때마다 새로 적는다
    detector = room_camera.make_detector(config.yolo_model, config.desk_area)
    on_checkout = sensors is not None  # 서버로 보내는 중이면 "퇴실하면 확인", 아니면(연습) "30초 빈 방"
    watcher = room_camera.RoomWatcher(room, check_objects=detector.finds_objects, room_items=config.room_items,
                                      on_checkout=on_checkout, baseline_path=config.baseline_file)
    cam = room_camera.open_camera(config.room_camera)
    send_check = None
    if on_checkout:
        tidy = SensorSender(config.server, config.device_token, path=f"/api/rooms/{room}/tidy-checks",
                            drop=TIDY_DROP, name="정리 확인 전송").start()
        send_check = tidy.push
        CheckoutPoller(config.server, config.device_token, room,
                       lambda rid: watcher.request_check(rid, time.monotonic())).start()
        print("정리 확인: 퇴실하면 확인 (서버 예약 상태를 10초마다 확인)")
    else:
        print("정리 확인: 연습 모드 (모두 나가고 30초 뒤 확인, 서버로 안 보냄)")
    t = threading.Thread(target=room_camera.run_room_camera,
                         args=(watcher, cam, detector, camera_push(config, sensors), lambda: fresh_pir(room)),
                         kwargs={"preview": start_preview(config.preview_port, config.preview_host),
                                 "on_frame": lambda: CAMERA_ROOMS.__setitem__(room, time.monotonic()),
                                 "send_check": send_check},
                         daemon=True)
    t.start()
    return t


def start_qr(config, verifier, on_reply):
    """문 앞 웹캠 QR 입실을 뒤에서(스레드) 켠다. 키패드와 같은 확인기, 같은 우노 LED를 쓴다."""
    import qr_door  # QR을 켤 때만 불러온다
    door = qr_door.QrDoor(verifier, config.qr_room_id)
    frames = qr_door.open_camera(config.qr_camera)  # 스레드 전에 연다: 카메라가 없으면 바로 멈추고 알려줌
    t = threading.Thread(target=qr_door.run_camera, args=(door, config.qr_camera, on_reply, frames), daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    main()
