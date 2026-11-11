"""M-SPACE 게이트웨이 (노트북/젯슨용)

문 앞 웹캠 QR 입실(qr_door.py)을 서버에 물어보고, 결과를 우노에 OK/NO로 돌려줘 LED를 켠다 (FR-ACC-001).
키패드 입력은 11/10에 뺐다 (QR 입실만 씀).
우노가 USB 시리얼로 보내는 재실 센서(PIR·조도) 줄은 core 형식(docs/sensor-events-api.md)으로 바꿔 화면에 찍고,
SEND_SENSORS=1이면 core POST /api/sensor-events로 보낸다 (FR-DEV-101). 시리얼 읽기를 막지 않게 뒤에서(스레드) 보낸다.

출입 확인 API (core 담당 지영과 맞춘 형식):
  요청: {"eventId": "uuid", "roomId": 1, "deviceId": "qr-1", "code": "123456"}
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
  handle_line()           : 우노가 보낸 한 줄을 sensorType별 처리기(HANDLERS)로 보낸다
  main()                  : 위 부품을 조립해서 시리얼을 계속 읽는다
  qr_door.py (QR_CAMERA)  : 문 앞 웹캠으로 QR 입실. 같은 확인기로 묻고 결과를 우노 LED로 보낸다
  room_camera.py (ROOM_CAMERA) : 방 안 웹캠으로 인원 세기 + 퇴실 뒤 정리 확인. 사진은 저장·전송 안 함
  room_env.py (MQTT_ROOM)      : 교육장 MQTT 센서(CO2·온도·습도)를 그 방 값으로 core에 보냄. 구독만 함
  fake_env_sensors.py          : 젯슨 안 브로커에 방마다 가짜 CO2·온도·습도를 올린다 (진짜 센서가 멈췄을 때 시연용)
  sensor_adapters.py           : 위 센서들(우노·카메라·MQTT)이 core로 내보내는 공통 모양 (어댑터 패턴, FR-DEV-101)

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
    device_token: str | None = None        # 장치 토큰. 서버가 "등록된 장치"인지 확인하는 값. 저장소에 넣지 않고 .env에만 둔다
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
    floor_area: tuple | None = None        # 바닥 부분 (x1, y1, x2, y2) 화면 비율. 정리 확인 때 바닥도 확대해서 한 번 더 본다
    desk_photo: bool = False               # True면 깨끗한 책상 사진(사람 0명일 때)을 젯슨에만 두고 퇴실 뒤 책상과 비교
    desk_thresh: int = 35                  # 책상 비교: 밝기 차이가 이보다 크면 "달라짐" (DESK_THRESH, 1~255)
    desk_min_area: float = 0.0012          # 책상 비교: 책상 넓이의 이 비율보다 작은 차이는 무시 (DESK_MIN_AREA)
    desk_light: str = "mean"               # 책상 비교 방식: mean(전체 밝기) / local(자리마다 밝기) / ssim(모양 비교)
    desk_gone: bool = False                # 책상 비교: 물건을 치운 자리는 "달라짐"에서 뺀다 (DESK_GONE=1, 실험)
    desk_dino_model: str = "gateway/dinov2_small.onnx"  # DESK_LIGHT=dino에서 쓸 DINOv2 ONNX (DESK_DINO_MODEL, 실험)
    desk_eq: bool = False                  # 책상 비교: 지금 사진 밝기·대비를 기준에 맞춤 (DESK_EQ=1, 불 켬↔끔 실험)
    desk_move: bool = False                # 책상 비교: 원래 있던 걸 옮기거나 뒤집은 것은 새 물건으로 안 셈 (DESK_MOVE=1, 실험)
    desk_match: bool = False               # 정리 확인: 잘라낸 물건끼리 DINOv2로 "같은 물건인가" 비교 (DESK_MATCH=1, 실험)
    desk_match_sim: float = 0.8            # DESK_MATCH에서 "같은 물건"으로 볼 비슷한 정도 (DESK_MATCH_SIM, 0~1)
    tidy_photos: bool = False              # True면 정리 확인 때 빈 방 사진(입실 전·퇴실 뒤·3시간 뒤)을 core로 올린다 (Q-27)
    label_model: str | None = None         # 달라진 곳에 이름 붙일 모델 (YOLO-World ONNX). 없으면 YOLO_MODEL로
    baseline_file: str | None = "room_baseline.json"  # 깨끗한 방 기준을 적어 두는 파일. BASELINE_FILE=off면 메모리에만
    mqtt_room: int | None = None           # 교육장 MQTT 센서 값을 이 방 CO2·온도·습도로 보낸다 (Q-21 시연). 비우면 끔
    mqtt_rooms: tuple = ()                 # 여러 방: ((공간 이름, 방 번호), ...). MQTT_ROOMS="가상-1:1,가상-2:2"
    mqtt_location: str = "실습실"           # MQTT 센서 중 이 공간(deviceInfo.tags.location)만 쓴다
    mqtt_host: str | None = None           # 교육장 MQTT 브로커 주소. 아이디·비밀번호와 함께 .env에만 둔다
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = field(default=None, repr=False)  # 화면·로그에 찍히지 않게
    mqtt_client_id: str | None = None      # 다른 사람과 겹치면 서로 끊긴다. 비우면 mspace-<젯슨 이름>-reader
    mqtt_device_id: str | None = None      # core에 보낼 장치 이름. 비우면 sensor-<방 번호>


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
        floor_area=parse_desk_area(env.get("FLOOR_AREA", ""), "FLOOR_AREA"),
        desk_photo=env.get("DESK_PHOTO") == "1",
        desk_thresh=parse_number(env, "DESK_THRESH", Config.desk_thresh, 1, 255, int),
        desk_min_area=parse_number(env, "DESK_MIN_AREA", Config.desk_min_area, 0.0, 0.3, float),
        desk_light=env.get("DESK_LIGHT") if env.get("DESK_LIGHT") in ("local", "ssim", "dino") else Config.desk_light,
        desk_dino_model=env.get("DESK_DINO_MODEL") or Config.desk_dino_model,
        desk_gone=env.get("DESK_GONE") == "1",
        desk_eq=env.get("DESK_EQ") == "1",
        desk_move=env.get("DESK_MOVE") == "1",
        desk_match=env.get("DESK_MATCH") == "1",
        desk_match_sim=parse_number(env, "DESK_MATCH_SIM", Config.desk_match_sim, 0.0, 1.0, float),
        tidy_photos=env.get("TIDY_PHOTOS") == "1",
        label_model=env.get("LABEL_MODEL") or None,
        baseline_file=(None if env.get("BASELINE_FILE") == "off"
                       else env.get("BASELINE_FILE") or Config.baseline_file),
        mqtt_room=int(env["MQTT_ROOM"]) if _is_number(env.get("MQTT_ROOM", "")) else None,
        mqtt_rooms=parse_mqtt_rooms(env.get("MQTT_ROOMS", "")),
        mqtt_location=env.get("MQTT_LOCATION") or Config.mqtt_location,
        mqtt_host=parse_mqtt_host(env.get("MQTT_HOST", ""))[0],
        mqtt_port=(int(env["MQTT_PORT"]) if _is_number(env.get("MQTT_PORT", ""))
                   else parse_mqtt_host(env.get("MQTT_HOST", ""))[1] or Config.mqtt_port),
        mqtt_username=env.get("MQTT_USERNAME") or None,
        mqtt_password=env.get("MQTT_PASSWORD") or None,
        mqtt_client_id=env.get("MQTT_CLIENT_ID") or None,
        mqtt_device_id=env.get("MQTT_DEVICE_ID") or None,
    )


def parse_mqtt_rooms(text):
    """MQTT_ROOMS="가상-1:1, 가상-2:2" → (("가상-1", 1), ("가상-2", 2)). 형식이 틀린 칸은 건너뛴다."""
    rooms = []
    for part in text.split(","):
        name, _, room = part.strip().rpartition(":")
        if name.strip() and _is_number(room.strip()):
            if any(n == name.strip() for n, _ in rooms):
                print(f"MQTT_ROOMS에 같은 공간 이름이 두 번 있어 뒤의 것은 건너뛰어요: {name.strip()!r}")
                continue
            rooms.append((name.strip(), int(room.strip())))
    return tuple(rooms)


def env_rooms(config):
    """MQTT로 받을 (공간 이름, 방 번호) 목록. MQTT_ROOMS가 있으면 그것, 없으면 MQTT_LOCATION → MQTT_ROOM 하나."""
    if config.mqtt_rooms:
        return config.mqtt_rooms
    return ((config.mqtt_location, config.mqtt_room),) if config.mqtt_room else ()


def parse_mqtt_host(text):
    """MQTT_HOST="mqtt://broker:1883" → ("broker", 1883). 포트를 같이 적어도, 안 적어도 된다. 비면 (None, None)."""
    text = text.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.rstrip("/")
    if text.startswith("["):  # IPv6는 [::1]:1883 처럼 대괄호로 감싼다
        host, _, rest = text[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
        return (host or None), (int(port) if _is_number(port) else None)
    if text.count(":") > 1:  # 대괄호 없는 IPv6는 포트로 자르지 않는다
        return text, None
    host, sep, port = text.rpartition(":")
    if sep and _is_number(port) and host:
        return host, int(port)
    return (text or None), None


def parse_room_items(text):
    """ROOM_ITEMS="remote:1,book:3" → {"remote": 1, "book": 3}. 이상한 조각은 건너뛴다."""
    items = {}
    for part in text.split(","):
        label, _, n = part.strip().partition(":")
        if label.strip() and _is_number(n.strip()):
            items[label.strip()] = int(n.strip())
    return items


def parse_number(env, key, default, low, high, kind):
    """숫자 설정 하나. 비우면 기본값, 틀리거나 범위 밖이면 알려 주고 기본값."""
    text = env.get(key, "").strip()
    if not text:
        return default
    try:
        value = kind(text)
    except ValueError:
        value = None
    if value is not None and low <= value <= high:
        return value
    print(f"{key}가 이상해서 기본값 {default}을 써요: {text!r} ({low}~{high})")
    return default


def parse_desk_area(text, key="DESK_AREA"):
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
    print(f"{key}가 이상해서 그 부분 확대는 꺼요: {text!r} (예: 0.25,0.22,0.64,0.83 = 왼쪽 위 x,y, 오른쪽 아래 x,y 비율)")
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
    - 시리얼 읽기·QR 답이 늦어지지 않게 별도 스레드에서 보낸다
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
            status = self.post(body).status_code
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

    def post(self, body):
        return requests.post(self.url, json=body, timeout=(CONNECT_TIMEOUT, TIMEOUT), headers=self.headers,
                             allow_redirects=False)  # 리디렉션을 따라가면 토큰이 다른 곳으로 갈 수 있다

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
RECHECK_MARGIN_SEC = 60  # 다시 확인은 at보다 1분 늦게. 젯슨 시계가 core보다 조금 빨라도 TOO_EARLY(409)로 버려지지 않게
TIDY_DROP = SENSOR_DROP | {404}  # 404 = 다른 방 예약이거나 서버에 아직 API가 없음 → 다시 보내도 같다


PHOTO_QUEUE_MAX = 12  # 끊긴 동안 모아 둘 사진 수 (한 장 0.1~1MB라 메모리를 아낀다). 넘치면 오래된 것부터 버린다
# 400 = 사진이 이상하거나 core가 그 단계(ENTRY)를 아직 안 받음, 409 = 이미 올렸거나 마지막 퇴실 예약이 아님
PHOTO_DROP = TIDY_DROP


class PhotoSender(SensorSender):
    """정리 확인 사진을 POST /api/rooms/{roomId}/tidy-photos (core #115)로 multipart로 올린다.

    다시 보내기·버리기 규칙은 SensorSender와 같다. 사진은 메모리 줄에만 있고 다 보내면 사라진다.
    """

    def __init__(self, server, token, room, name="정리 확인 사진 전송"):
        super().__init__(server, token, path=f"/api/rooms/{room}/tidy-photos", drop=PHOTO_DROP, name=name)
        self.queue = deque(maxlen=PHOTO_QUEUE_MAX)

    def post(self, body):
        fields = {"reservationId": str(body["reservationId"]), "phase": body["phase"], "deviceId": body["deviceId"]}
        files = {"file": (f"{body['phase'].lower()}.jpg", body["jpg"], "image/jpeg")}
        return requests.post(self.url, data=fields, files=files, timeout=(CONNECT_TIMEOUT, TIMEOUT),
                             headers=self.headers, allow_redirects=False)


class CheckoutPoller:
    """서버에 방의 예약 상태를 물어서, 방금 끝난 예약(lastEnded)이 바뀌면 on_checkout(예약 번호)을 부른다.

    켠 직후 처음 본 lastEnded는 이미 지난 일이라 기억만 하고 부르지 않는다.
    서버에 못 닿으면 기다렸다 다시 묻는다 (그동안 정리 확인은 미뤄질 뿐 틀리지 않는다).

    10차(두고 간 물건, 지금 3시간): 서버가 recheck {reservationId, at}를 내려 주면, at이 지난 뒤 처음 물을 때
    on_recheck(예약 번호)를 한 번 부른다. 퇴실 뒤 다음 팀이 입실해 서버가 recheck를 빼면 부르지 않고,
    이미 불렀어도 on_recheck_gone(예약 번호)으로 아직 못 한 다시 확인을 취소한다.
    같은 예약의 at이 바뀌면(core 설정이 바뀜) on_recheck_moved(예약 번호)로 기다리던 요청만 빼고 새 시각에 다시 부른다.
    """

    def __init__(self, server, token, room, on_checkout, every=POLL_SEC, get=None, on_recheck=None, now=None,
                 on_recheck_gone=None, on_recheck_moved=None):
        self.url = f"{server}/api/rooms/{room}/current"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.on_checkout = on_checkout
        self.every = every
        self.get = get or requests.get
        self.known = False   # 처음 한 번 읽었는지
        self.last = None     # 마지막으로 본 끝난 예약 번호
        self.failing = False
        self.on_recheck = on_recheck
        self.now = now or time.time
        self.rechecked = set()  # 이미 다시 확인을 부른 (예약 번호, 시각) (서버가 계속 내려 줘도 한 번만)
        self.on_recheck_gone = on_recheck_gone
        self.on_recheck_moved = on_recheck_moved
        self.waiting = None     # 서버가 마지막으로 내려 준 다시 확인 (예약 번호, 시각)

    def poll_once(self):
        try:
            r = self.get(self.url, headers=self.headers, timeout=(CONNECT_TIMEOUT, TIMEOUT), allow_redirects=False)
            if r.status_code != 200:
                raise ValueError(f"응답 {r.status_code}")
            data = r.json()
            ended = (data.get("lastEnded") or {}).get("reservationId")
            recheck = parse_recheck(data.get("recheck"))
        except (requests.RequestException, ValueError, AttributeError) as e:
            if not self.failing:  # 끊길 때 한 번만 알린다
                print(f"[퇴실 확인] 서버에 예약 상태를 못 물어요, 다시 시도해요: {e}")
            self.failing = True
            return None
        if self.failing:
            print("[퇴실 확인] 서버 연결 다시 됨")
        self.failing = False
        self.check_recheck(recheck)
        if not self.known:
            self.known, self.last = True, ended
            return None
        if ended is not None and ended != self.last:
            self.last = ended
            print(f"[퇴실 확인] 예약 {ended} 퇴실 → 방이 비면 정리 확인")
            self.on_checkout(ended)
            return ended
        return None

    def check_recheck(self, recheck):
        """다시 확인할 때가 지났으면 on_recheck를 한 번 부른다."""
        if self.on_recheck is None:
            return
        rid = recheck[0] if recheck else None
        old = self.waiting
        if old is not None and old[0] != rid and self.on_recheck_gone:
            self.on_recheck_gone(old[0])  # 서버가 뺐다: 아직 방이 안 비어 못 한 다시 확인은 취소
        elif old is not None and old != recheck and old in self.rechecked and self.on_recheck_moved:
            self.on_recheck_moved(rid)  # 같은 예약인데 시각이 늦춰짐: 기다리던 요청만 빼고 새 시각에 다시
        self.waiting = recheck
        if recheck is None:
            return
        at = recheck[1]
        if recheck in self.rechecked or self.now() < at + RECHECK_MARGIN_SEC:
            return
        self.rechecked.add(recheck)
        print(f"[퇴실 확인] 예약 {rid} 3시간 뒤 다시 확인 → 방이 비면 확인")
        self.on_recheck(rid)

    def run_forever(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:  # 한 번 실패해도 퇴실 확인 스레드는 살려 둔다
                print(f"[퇴실 확인] 예약 상태 처리 중 오류, 다음에 다시 해요: {e}")
            time.sleep(self.every)

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True).start()
        return self


def parse_recheck(value):
    """서버 recheck {reservationId, at} → (예약 번호, epoch 초). 없거나 모양이 틀리면 None."""
    if not isinstance(value, dict):
        return None
    rid, at = value.get("reservationId"), value.get("at")
    if not is_int(rid) or not isinstance(at, str):
        return None
    try:
        when = datetime.fromisoformat(at.replace("Z", "+00:00"))  # 파이썬 3.10 아래는 Z를 못 읽는다
    except ValueError:
        return None
    if when.tzinfo is None:
        return None  # 시간대 없는 시각은 젯슨 시계와 어긋날 수 있어 쓰지 않는다
    return rid, when.timestamp()


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
    LAST_LIGHT[body["roomId"]] = (event["light"], time.monotonic())  # 조도도 같이 싣는다 (기록용)
    if camera_alive(body["roomId"]):  # 이 방은 카메라가 PIR까지 합쳐서 보낸다 (두 장치가 번갈아 다른 값을 보내지 않게)
        print(f"재실 {body['roomId']}번 방 PIR: {state}, 조도: {event['light']} (카메라 이벤트에 합쳐 보냄)")
        return None
    print(f"재실 {body['roomId']}번 방: {state} (pir={event['pir']}, 조도={event['light']})")
    if isinstance(verifier, DryRunVerifier):  # 연습 모드에서는 core에 보낼 모양을 그대로 보여 준다
        print("[DRY_RUN] POST /api/sensor-events", json.dumps(body, ensure_ascii=False))
    elif sensors is not None:
        sensors.push(body)
    return None


LAST_PIR = {}      # 방 번호 → (우노 PIR 마지막 값 0/1, 받은 시각)
LAST_LIGHT = {}    # 방 번호 → (우노 조도 마지막 값 0~1023, 받은 시각)
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


def fresh_light(room, now=None):
    """우노 조도 마지막 값. PIR과 같이 PIR_FRESH_SEC 넘게 새 값이 없으면 None."""
    got = LAST_LIGHT.get(room)
    if got is None or (time.monotonic() if now is None else now) - got[1] >= PIR_FRESH_SEC:
        return None
    return got[0]


def with_light(push, room):
    """카메라 재실 이벤트에 우노 조도를 sources.light로 붙여서 보낸다 (core는 기록만, 재실 판단엔 안 씀)."""
    def send(body):
        light = fresh_light(room)
        if body.get("kind") == "OCCUPANCY" and light is not None:
            body = {**body, "sources": {**body.get("sources", {}), "light": light}}
        push(body)
    return send


# sensorType별 처리기 표. 새 센서 줄은 여기에 한 줄씩 추가한다
HANDLERS = {
    "occupancy": handle_occupancy,  # 키패드(keypad)는 11/10에 뺐다. 옛 우노가 보내도 표에 없어 건너뛴다
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
    return handler(event, verifier, sensors)


# ---------- 실행 ----------

def main(config=None):
    config = config or load_config()
    cameras_only = not config.port and bool(config.qr_camera or config.room_camera or env_rooms(config))
    if not config.port and not cameras_only:
        raise SystemExit("우노를 못 찾았어요. USB 연결을 확인하거나 PORT=/dev/ttyACM0 처럼 지정하세요.")
    if not config.dry_run and not config.device_token:
        raise SystemExit("DEVICE_TOKEN이 없어요. .env에 넣고 불러오거나, 서버 없이 테스트하려면 DRY_RUN=1 로 실행하세요.")
    if not config.dry_run and not is_safe_server(config.server):
        raise SystemExit("원격 서버는 https://로만 연결해요 (장치 토큰이 실리므로). http://는 localhost 테스트에서만 써요.")
    print("연결:", config.port or "우노 없음 (카메라만)", "| 서버:", "DRY_RUN" if config.dry_run else config.server,
          "| 재실 전송:", "켬" if config.send_sensors and not config.dry_run else "끔",
          "| QR 입실:", f"켬 (카메라 {config.qr_camera}, {config.qr_room_id}번 방)" if config.qr_camera else "끔",
          "| 인원 세기:", f"켬 (카메라 {config.room_camera}, {config.room_camera_room}번 방)" if config.room_camera else "끔",
          "| 환경 MQTT:", ("켬 (" + ", ".join(f"{loc} → {room}번 방" for loc, room in env_rooms(config)) + ")"
                          if env_rooms(config) else "끔"))
    verifier = make_verifier(config)
    sensors = None
    if config.send_sensors and not config.dry_run:
        sensors = SensorSender(config.server, config.device_token).start()
    ser = None
    if not cameras_only:
        ser = serial.Serial(config.port, 115200, timeout=1)  # ser = 우노와 연결된 시리얼 통로. 115200은 우노 코드와 같은 속도
        time.sleep(2)  # 우노는 시리얼 연결 시 재부팅되므로 켜질 때까지 기다린다
    write_lock = threading.Lock()  # QR 답이 여러 스레드에서 우노로 가도 줄이 섞이지 않게

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
    if env_rooms(config):
        start_room_env(config, sensors)

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


def start_room_env(config, sensors, make_client=None, start_thread=True, make_sender=None):
    """교육장 MQTT 센서(CO2·온도·습도)를 방 값으로 core에 보낸다 (Q-21 시연). 구독만 한다.

    MQTT_ROOMS가 있으면 공간마다 방 하나씩, 없으면 MQTT_LOCATION → MQTT_ROOM 하나.
    방이 여럿이면 방마다 전송 줄을 따로 둔다. 한 방이 막혀도(403 등) 다른 방·카메라 이벤트는 계속 간다.
    """
    import socket

    import room_env
    if not config.mqtt_host:
        print("MQTT_ROOM을 켰지만 MQTT_HOST가 없어요. .env에 MQTT_HOST·MQTT_USERNAME·MQTT_PASSWORD를 넣어요")
        return None
    rooms = env_rooms(config)
    if len(rooms) > 1 and sensors is not None:
        make_sender = make_sender or (lambda room: SensorSender(config.server, config.device_token,
                                                               name=f"센서 전송 {room}번 방").start())
        pushes = [camera_push(config, make_sender(room)) for _, room in rooms]
    else:
        pushes = [camera_push(config, sensors)] * len(rooms)
    envs = [room_env.RoomEnv(loc, room, (config.mqtt_device_id if len(rooms) == 1 else None) or f"sensor-{room}",
                             push) for (loc, room), push in zip(rooms, pushes)]
    env = envs[0] if len(envs) == 1 else room_env.RoomRouter(envs)
    client_id = config.mqtt_client_id or f"mspace-{socket.gethostname()}-reader"
    client = (make_client or room_env.make_client)(env, client_id, config.mqtt_host, config.mqtt_username,
                                                  config.mqtt_password, port=config.mqtt_port)
    for e in envs:
        print(f"환경 MQTT: {config.mqtt_host}:{config.mqtt_port} ({client_id}), '{e.location}' 센서 평균을 "
              f"{e.room_id}번 방 CO2·온도·습도로 1분마다 보내요")
    if start_thread:
        client.loop_start()  # paho가 뒤에서 받고, 끊기면 1~5초 간격으로 다시 붙는다
        threading.Thread(target=room_env.run_ticks, args=(env,), daemon=True).start()
    return env, client


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


def desk_compare(config, detector):
    """DESK_PHOTO=1 이면 깨끗한 책상 사진 비교와 이름 붙이기를 만든다. 아니면 (None, None)."""
    if not config.desk_photo:
        return None, None
    if not config.desk_area or not detector.finds_objects:
        print("DESK_PHOTO=1은 DESK_AREA와 YOLO_MODEL이 있어야 해요. 책상 사진 비교는 꺼요")
        return None, None
    import desk_diff
    import room_camera
    path = os.path.splitext(config.baseline_file)[0] + "_desk.png" if config.baseline_file else None
    print("정리 확인: 깨끗한 책상 사진과 비교해요 (사람 0명일 때 찍은 책상 사진 한 장만 젯슨에 둠, 서버로 안 보냄)")
    return (desk_diff.DeskCompare(config.desk_area, path=path, **desk_diff.settings(config)),
            room_camera.make_labeler(detector, config.label_model))


def start_room_camera(config, sensors):
    """방 안 웹캠 인원 세기·정리 확인을 뒤에서(스레드) 켠다."""
    import room_camera  # 켤 때만 불러온다 (OpenCV는 젯슨에만 있으면 됨)
    room = config.room_camera_room
    CAMERA_ROOMS[room] = time.monotonic()  # 켤 때 한 번 잡고, 이후 사진을 읽을 때마다 새로 적는다
    detector = room_camera.make_detector(config.yolo_model, config.desk_area, config.floor_area)
    on_checkout = sensors is not None  # 서버로 보내는 중이면 "퇴실하면 확인", 아니면(연습) "30초 빈 방"
    desk, labeler = desk_compare(config, detector)
    entry_desk = None
    if desk is not None and on_checkout:  # 예약 기준 책상 사진 (입실 직전 빈 방, 정리 확인 뒤 지움, Q-22)
        import desk_diff
        path = os.path.splitext(config.baseline_file)[0] + "_entry_desk.png" if config.baseline_file else None
        entry_desk = desk_diff.DeskCompare(config.desk_area, path=path, **desk_diff.settings(config))
    photos = config.tidy_photos and on_checkout  # 연습 모드는 서버로 안 보내니 사진도 안 찍는다
    watcher = room_camera.RoomWatcher(room, check_objects=detector.finds_objects, room_items=config.room_items,
                                      on_checkout=on_checkout, baseline_path=config.baseline_file,
                                      desk=desk, labeler=labeler, entry_desk=entry_desk, photos=photos)
    cam = room_camera.open_camera(config.room_camera)
    send_check = send_photo = None
    if on_checkout:
        tidy = SensorSender(config.server, config.device_token, path=f"/api/rooms/{room}/tidy-checks",
                            drop=TIDY_DROP, name="정리 확인 전송").start()
        send_check = tidy.push
        if photos:
            send_photo = PhotoSender(config.server, config.device_token, room).start().push
            print("정리 확인 사진: 사람 0명인 빈 방 사진을 core에 올려요 (입실 전·퇴실 뒤·3시간 뒤, 30일 보관)")
        CheckoutPoller(config.server, config.device_token, room,
                       lambda rid: watcher.request_check(rid, time.monotonic()),
                       on_recheck=lambda rid: watcher.request_check(rid, time.monotonic(), "RECHECK"),
                       on_recheck_gone=watcher.cancel_recheck,
                       on_recheck_moved=watcher.postpone_recheck).start()
        print("정리 확인: 퇴실하면 확인 (서버 예약 상태를 10초마다 확인). "
              "기준은 사람이 처음 보이기 직전 3초 빈 방 (그 예약 동안 생긴 것만 봄)")
    else:
        print("정리 확인: 연습 모드 (모두 나가고 30초 뒤 확인, 서버로 안 보냄)")
    t = threading.Thread(target=room_camera.run_room_camera,
                         args=(watcher, cam, detector, with_light(camera_push(config, sensors), room),
                               lambda: fresh_pir(room)),
                         kwargs={"preview": start_preview(config.preview_port, config.preview_host),
                                 "on_frame": lambda: CAMERA_ROOMS.__setitem__(room, time.monotonic()),
                                 "send_check": send_check, "send_photo": send_photo},
                         daemon=True)
    t.start()
    return t


def start_qr(config, verifier, on_reply):
    """문 앞 웹캠 QR 입실을 뒤에서(스레드) 켠다. 확인기로 묻고 결과를 우노 LED로 보낸다."""
    import qr_door  # QR을 켤 때만 불러온다
    door = qr_door.QrDoor(verifier, config.qr_room_id)
    frames = qr_door.open_camera(config.qr_camera)  # 스레드 전에 연다: 카메라가 없으면 바로 멈추고 알려줌
    t = threading.Thread(target=qr_door.run_camera, args=(door, config.qr_camera, on_reply, frames), daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    main()
