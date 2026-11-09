"""교육장 MQTT 센서(CO2·온도·습도)를 받아 방 값으로 core에 보낸다 (Q-21 시연, FR-DEV-104).

시연은 실습실에 이미 있는 센서를 M-SPACE 방으로 치고 쓴다(docs Q-21). 공간 이름(MQTT_LOCATION)과
방 번호(MQTT_ROOM)는 설정값이라, 실제 방에 센서를 달면 설정만 바꾸면 된다.

- 구독만 한다. 같은 브로커를 다른 팀도 써서 발행은 하지 않는다
- ChirpStack LoRaWAN 업링크 `application/+/device/+/event/up` (JSON). 냉난방기 토픽은 받지 않는다
- 게이트웨이가 2대라 같은 메시지가 두 번 온다 → (devEui, fCnt)가 같으면 한 번만
- 센서 시각이 가끔 미래로 찍힌다 → 받은 시각보다 30초 넘게 뒤면 받은 시각을 쓴다
- 한 공간에 센서가 여러 대(실습실 9대 넘음) → 기기마다 최근 값 하나만 남기고 그다음 평균
- 기기 주기는 1분이나 2분 → 5분 넘게 안 온 기기는 평균에서 뺀다
- 형식이 안 맞는 메시지는 버리되 로그로 남긴다
- 1분마다, 그 사이 새 값이 왔을 때만 core에 CO2 이벤트(온도·습도 포함)를 보낸다. 같은 값을 다시 보내지 않아
  core의 "1000ppm 3분 연속" 판단이 옛 값으로 늘어나지 않는다
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

TOPIC = "application/+/device/+/event/up"
FUTURE_SEC = 30      # 센서 시각이 받은 시각보다 이만큼 넘게 뒤(미래)면 받은 시각을 쓴다
STALE_SEC = 300      # 기기 값이 이만큼 안 오면 평균에서 뺀다 (주기 1~2분 → 두세 번 놓쳐도 남게)
SEND_SEC = 60        # core로 보내는 간격 (core CO2 판단이 1분 단위)
SEEN_MAX = 5000      # 중복 확인용으로 기억할 (devEui, fCnt) 수. 넘치면 오래된 것부터 잊는다
DEDUP_SEC = 120      # 같은 (devEui, fCnt)가 이 안에 다시 오면 중복. 넘으면 센서가 재가입해 fCnt가 0부터 다시 센 것
KST = timezone(timedelta(hours=9))

# core가 받는 범위 (docs api 6-1). 벗어난 값은 그 값만 뺀다
CO2_RANGE = (0, 10_000)
TEMP_RANGE = (-40.0, 85.0)
HUMIDITY_RANGE = (0.0, 100.0)


class BadMessage(ValueError):
    """형식이 안 맞는 MQTT 메시지 (버리고 로그로 남긴다)."""


@dataclass(frozen=True)
class Reading:
    """업링크 한 개에서 꺼낸 값. 없는 값은 None."""
    dev: str
    fcnt: int | None
    location: str | None
    point: str | None
    at: float            # 측정 시각 (epoch 초, 미래면 받은 시각으로 고친 뒤)
    co2: int | None
    temperature: float | None
    humidity: float | None
    keys: tuple          # object 안에 온 키 (처음 한 번 보여 주기용)


def parse_time(text):
    """ChirpStack 시각 → epoch 초. 'Z'·나노초(9자리)도 받는다. 못 읽으면 None."""
    if not isinstance(text, str):
        return None
    s = text.strip().replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # 파이썬 3.10 이하 fromisoformat은 소수 6자리까지만
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        return None  # 기준 시간대를 모르면 쓰지 않는다
    return t.timestamp()


def _number(value, low, high, kind=float):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not low <= value <= high:
        return None
    return kind(round(value)) if kind is int else float(value)


def parse_uplink(payload, received):
    """MQTT 메시지 한 개 → Reading. 형식이 안 맞으면 BadMessage (부르는 쪽이 로그로 남긴다)."""
    try:
        d = json.loads(payload)
    except (ValueError, TypeError) as e:
        raise BadMessage(f"JSON 아님 ({e.__class__.__name__})") from None
    info = d.get("deviceInfo") if isinstance(d, dict) else None
    dev = info.get("devEui") if isinstance(info, dict) else None
    if not isinstance(dev, str) or not dev:
        raise BadMessage("deviceInfo.devEui 없음")
    obj = d.get("object")
    if not isinstance(obj, dict):
        raise BadMessage("object 없음")
    tags = info.get("tags") if isinstance(info.get("tags"), dict) else {}
    fcnt = d.get("fCnt")
    at = parse_time(d.get("time"))
    if at is None or at - received > FUTURE_SEC:
        at = received  # 시각이 없거나 미래로 찍혔으면 받은 시각
    return Reading(
        dev=dev,
        fcnt=fcnt if isinstance(fcnt, int) and not isinstance(fcnt, bool) else None,
        location=tags.get("location"),
        point=tags.get("point"),
        at=at,
        co2=_number(obj.get("co2"), *CO2_RANGE, kind=int),
        temperature=_number(obj.get("temperature"), *TEMP_RANGE),
        humidity=_number(obj.get("humidity"), *HUMIDITY_RANGE),
        keys=tuple(sorted(obj)),
    )


class RoomEnv:
    """한 공간(location)의 센서 여러 대 값을 모아 방 하나의 CO2·온도·습도로 보낸다."""

    def __init__(self, location, room_id, device_id, push, log=print):
        self.location = location
        self.room_id = room_id
        self.device_id = device_id
        self.push = push          # core로 보낼 줄 (SensorSender.push 또는 화면 출력)
        self.log = log
        self.lock = threading.Lock()
        self.seen = OrderedDict()  # (devEui, fCnt) → 받은 시각. 중복 확인
        self.latest = {}           # devEui → Reading (기기마다 최근 값 하나)
        self.shown = set()         # object 키를 이미 보여 준 기기
        self.fresh = False         # 마지막으로 보낸 뒤 새 값이 왔는지
        self.sent_at = None

    def on_message(self, topic, payload, received=None):
        """MQTT 메시지 하나. 반환: 쓴 Reading, 아니면 None (형식 오류·중복·다른 공간)."""
        received = time.time() if received is None else received
        try:
            r = parse_uplink(payload, received)
        except BadMessage as e:
            self.log(f"[환경 MQTT] 형식 안 맞음, 건너뜀 ({e}): {topic} {bytes(payload)[:200]!r}")
            return None
        with self.lock:
            if r.fcnt is not None:
                key = (r.dev, r.fcnt)
                prev = self.seen.get(key)
                if prev is not None and received - prev <= DEDUP_SEC:
                    return None  # 게이트웨이 2대가 같은 메시지를 보냄 (거의 같은 때 도착)
                self.seen[key] = received
                self.seen.move_to_end(key)
                if len(self.seen) > SEEN_MAX:
                    self.seen.popitem(last=False)
            if r.dev not in self.shown:
                self.shown.add(r.dev)
                self.log(f"[환경 MQTT] 새 센서 {r.dev} ({r.location} / {r.point}) object 키: {', '.join(r.keys)}")
            if r.location != self.location:
                return None
            old = self.latest.get(r.dev)
            if old is not None and old.at > r.at:
                return None  # 늦게 도착한 옛 값은 최근 값을 덮지 않는다
            self.latest[r.dev] = r
            if r.co2 is not None:  # CO2 없는 메시지만으로는 보내지 않는다 (같은 CO2를 새 eventId로 다시 보내지 않게)
                self.fresh = True
        return r

    def summary(self, now):
        """기기별 최근 값(5분 안)으로 낸 평균. CO2가 하나도 없으면 None."""
        with self.lock:
            live = [r for r in self.latest.values() if now - r.at <= STALE_SEC]
        co2 = [r.co2 for r in live if r.co2 is not None]
        if not co2:
            return None
        temps = [r.temperature for r in live if r.temperature is not None]
        hums = [r.humidity for r in live if r.humidity is not None]
        return {
            "ppm": round(sum(co2) / len(co2)),
            "temperatureC": round(sum(temps) / len(temps), 1) if temps else None,
            "humidityPct": round(sum(hums) / len(hums), 1) if hums else None,
            "at": max(r.at for r in live if r.co2 is not None),
            "devices": len(co2),
        }

    def tick(self, now=None):
        """SEND_SEC마다 부른다. 새 값이 있었으면 core CO2 이벤트를 만들어 push하고 돌려준다."""
        now = time.time() if now is None else now
        if self.sent_at is not None and now - self.sent_at < SEND_SEC:
            return None
        with self.lock:
            if not self.fresh:
                return None
            self.fresh = False  # 먼저 내린다. 평균을 내는 사이 온 새 값은 다시 True로 올려 다음 번에 보낸다
        s = self.summary(now)
        if s is None:
            with self.lock:
                self.fresh = True  # 보낼 CO2가 아직 없으니 표시를 되돌린다
            return None
        body = {
            "eventId": str(uuid.uuid4()),
            "roomId": self.room_id,
            "deviceId": self.device_id,
            "kind": "CO2",
            "ppm": s["ppm"],
            "source": "mqtt",
            "observedAt": datetime.fromtimestamp(s["at"], KST).isoformat(timespec="seconds"),
        }
        if s["temperatureC"] is not None:
            body["temperatureC"] = s["temperatureC"]
        if s["humidityPct"] is not None:
            body["humidityPct"] = s["humidityPct"]
        self.sent_at = now
        self.log(f"환경 {self.room_id}번 방 ({self.location} 센서 {s['devices']}대 평균): CO2 {s['ppm']}ppm"
                 + (f", {s['temperatureC']}℃" if s["temperatureC"] is not None else "")
                 + (f", 습도 {s['humidityPct']}%" if s["humidityPct"] is not None else ""))
        self.push(body)
        return body


def make_client(env, client_id, host, username, password, port=1883, mqtt=None):
    """paho-mqtt 2.x 클라이언트. 구독만 한다. QoS 1, clean_session=False, keepalive 30 (끊긴 동안 온 것도 받게)."""
    if mqtt is None:
        import paho.mqtt.client as mqtt  # 켤 때만 불러온다 (MQTT를 안 쓰면 설치 안 해도 됨)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            print(f"[환경 MQTT] 접속 거절: {reason_code} (MQTT_USERNAME·MQTT_PASSWORD 확인)")
            return
        client.subscribe(TOPIC, qos=1)
        print(f"[환경 MQTT] 접속됨, 구독: {TOPIC}")

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        print(f"[환경 MQTT] 끊김 ({reason_code}), 다시 붙어요")

    def on_connect_fail(client, userdata):
        print("[환경 MQTT] 접속 실패, 다시 해 볼게요 (MQTT_HOST 주소·인터넷 확인)")

    def on_message(client, userdata, msg):
        env.on_message(msg.topic, msg.payload)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=False)
    if username:
        client.username_pw_set(username, password)
    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.on_connect_fail = on_connect_fail
    client.reconnect_delay_set(min_delay=1, max_delay=5)
    client.connect_async(host, port, keepalive=30)
    return client


def run_ticks(env, every=5.0, sleep=time.sleep, stop=None):
    """뒤에서 돌며 env.tick()을 부른다 (실제 전송 간격은 SEND_SEC)."""
    while stop is None or not stop.is_set():
        try:
            env.tick()
        except Exception as e:  # noqa: BLE001 - 한 번 실패로 전송 스레드가 죽지 않게
            print(f"[환경 MQTT] 보내기 준비 실패: {e.__class__.__name__}: {e}")
        sleep(every)
