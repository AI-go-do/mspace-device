"""M-SPACE 게이트웨이 (노트북/젯슨용)

우노가 USB 시리얼로 보내는 키패드 번호(JSON 한 줄)를 읽어서 서버에 물어보고,
결과를 우노에 OK/NO로 돌려준다 (FR-ACC-001). 재실 센서 전달은 다음 단계에서 추가.

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
  handle_line()           : 우노가 보낸 한 줄을 받아 우노에 돌려줄 답(OK/NO)을 만든다
  main()                  : 위 부품을 조립해서 시리얼을 계속 읽는다

설치:  pip3 install pyserial requests
실행:  DRY_RUN=1 python3 gateway.py
       SERVER=https://dev-mspace.java21.net DEVICE_TOKEN=<장치 토큰> python3 gateway.py
       (http://는 localhost 테스트에서만. 토큰이 실리므로 원격 서버는 https)
"""
from __future__ import annotations

import glob
import ipaddress
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
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
PORT_PATTERNS = ["/dev/ttyACM*", "/dev/ttyUSB*", "/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*"]


# ---------- 설정 ----------

@dataclass(frozen=True)
class Config:
    """게이트웨이 설정값 묶음. 코드를 고치지 않고 실행할 때 환경변수로 바꾼다."""
    server: str = "http://localhost:8080"  # 예약 서버(core) 주소
    dry_run: bool = False                  # True면 서버에 안 보내고 화면에만 찍는다 (서버 없이 테스트용)
    device_token: str | None = None        # 장치 토큰. 서버가 "등록된 키패드"인지 확인하는 값. 저장소에 넣지 않고 .env에만 둔다
    port: str | None = None                # 우노가 꽂힌 USB 포트. 못 찾으면 None


def find_port():
    """우노가 꽂힌 포트를 찾는다. PORT_PATTERNS 순서대로 처음 찾은 것, 없으면 None."""
    for pattern in PORT_PATTERNS:
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


def load_config(env=None):
    """환경변수에서 설정을 읽어 Config로 만든다. PORT를 안 정하면 find_port()로 찾는다."""
    env = os.environ if env is None else env
    return Config(
        server=env.get("SERVER", Config.server),
        dry_run=env.get("DRY_RUN") == "1",
        device_token=env.get("DEVICE_TOKEN"),
        port=env.get("PORT") or find_port(),
    )


def is_safe_server(server):
    """장치 토큰을 보내도 되는 주소인지. https는 괜찮고, http는 내 컴퓨터(localhost·127.0.0.1·::1)일 때만."""
    parts = urlsplit(server)
    if not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme != "http":
        return False
    if parts.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parts.hostname).is_loopback  # 글자 앞부분이 아니라 실제 주소로 판단한다
    except ValueError:  # 이름(예: core.example.com)은 loopback으로 보지 않는다
        return False


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

    def verify(self, body):
        for attempt in range(1, self.retries + 1):  # attempt = 지금 몇 번째 시도인지 (1, 2)
            try:
                r = requests.post(self.url, json=body, timeout=(CONNECT_TIMEOUT, TIMEOUT), headers=self.headers)  # 연결 1초, 답 2초 안에 안 오면 실패
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


# sensorType별 처리기 표. 재실 센서(pir, light 등)는 여기에 한 줄씩 추가한다 (FR-DEV-101)
HANDLERS = {
    "keypad": handle_keypad,
}


def handle_line(line, verifier):
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
    return handler(event, verifier)


# ---------- 실행 ----------

def main(config=None):
    config = config or load_config()
    if not config.port:
        raise SystemExit("우노를 못 찾았어요. USB 연결을 확인하거나 PORT=/dev/ttyACM0 처럼 지정하세요.")
    if not config.dry_run and not config.device_token:
        raise SystemExit("DEVICE_TOKEN이 없어요. .env에 넣고 불러오거나, 서버 없이 테스트하려면 DRY_RUN=1 로 실행하세요.")
    if not config.dry_run and not is_safe_server(config.server):
        raise SystemExit("원격 서버는 https://로만 연결해요 (장치 토큰이 실리므로). http://는 localhost 테스트에서만 써요.")
    print("연결:", config.port, "| 서버:", "DRY_RUN" if config.dry_run else config.server)
    verifier = make_verifier(config)
    ser = serial.Serial(config.port, 115200, timeout=1)  # ser = 우노와 연결된 시리얼 통로. 115200은 우노 코드와 같은 속도
    time.sleep(2)  # 우노는 시리얼 연결 시 재부팅되므로 켜질 때까지 기다린다

    while True:
        line = ser.readline().decode(errors="ignore")  # line = 우노가 보낸 한 줄 (글자로 변환, 깨진 글자는 무시)
        reply = handle_line(line, verifier)
        if reply:
            ser.write(reply)  # 우노에 결과를 돌려줘서 LED를 켜게 한다


if __name__ == "__main__":
    main()
