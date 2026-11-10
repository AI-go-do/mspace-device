"""가짜 교육장 센서: 젯슨 안 브로커에 방마다 CO2·온도·습도를 1분마다 올린다 (진짜 센서가 멈췄을 때 시연용).

진짜 센서(ChirpStack)와 같은 모양의 업링크를 `application/fake/device/<devEui>/event/up`으로 올린다.
게이트웨이는 평소처럼 구독만 하면 되고(MQTT_HOST=localhost, MQTT_ROOMS="가상-1:1,..."),
진짜 센서가 살아나면 MQTT_HOST만 원래 주소로 되돌리면 된다.

- 젯슨 안 브로커(localhost)에만 올린다. 다른 팀도 쓰는 교육장 브로커에는 절대 올리지 않는다
- 방마다 센서 1대. 사람 수가 몇 분마다 바뀌고, 사람이 있으면 CO2가 천천히 오르고 없으면 바깥 공기(약 420ppm)로 내려간다
- CO2는 FAKE_CO2_MAX(기본 950)를 넘지 않는다. 1000ppm 3분 연속이면 core가 실제 예약자에게 환기 알림을 보내기 때문

실행 (젯슨, 게이트웨이와 다른 터미널):
    FAKE_ROOMS=1,2,3 ~/mspace-venv/bin/python gateway/fake_env_sensors.py
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
OUTDOOR_CO2 = 420.0
PERIOD_SEC = 60
DEVICES_PER_ROOM = 1  # M-SPACE 방 하나에 센서 하나
POINTS = ("방",)


def location_name(room):
    """가짜 센서의 공간 이름. 게이트웨이 MQTT_ROOMS에 같은 이름을 쓴다."""
    return f"가상-{room}"


def dev_eui(room, i):
    """가짜 센서 장치 번호 (16자리). 진짜 센서와 겹치지 않게 fa로 시작한다."""
    return f"fa00000000{room:04d}{i:02d}"


@dataclass
class RoomState:
    """방 하나의 가짜 공기 상태."""
    room: int
    people: int = 0
    co2: float = OUTDOOR_CO2
    temperature: float = 22.5
    humidity: float = 45.0
    fcnt: dict = field(default_factory=dict)


def step(state, rng, co2_max=950.0):
    """1분 지난 뒤의 상태. 사람 수가 가끔 바뀌고, 그에 따라 CO2·온도·습도가 천천히 움직인다."""
    if rng.random() < 0.1:  # 10분에 한 번꼴로 사람이 들어오거나 나간다
        state.people = max(0, min(4, state.people + rng.choice((-2, -1, 1, 2))))
    target = OUTDOOR_CO2 + state.people * 150  # 사람 한 명당 오를 수 있는 만큼
    state.co2 += (target - state.co2) * 0.1 + rng.uniform(-8, 8)
    state.co2 = max(OUTDOOR_CO2 - 20, min(co2_max, state.co2))
    state.temperature += (22.5 + state.people * 0.4 - state.temperature) * 0.05 + rng.uniform(-0.1, 0.1)
    state.humidity += (45 + state.people * 2 - state.humidity) * 0.05 + rng.uniform(-0.5, 0.5)
    state.humidity = max(20.0, min(80.0, state.humidity))
    return state


def uplinks(state, now, rng, co2_max=950.0):
    """방 하나의 센서 업링크 [(토픽, JSON 바이트)]. 측정 흔들림을 조금 더한다 (CO2는 상한을 넘지 않게)."""
    out = []
    for i in range(DEVICES_PER_ROOM):
        dev = dev_eui(state.room, i)
        state.fcnt[dev] = state.fcnt.get(dev, -1) + 1
        body = {
            "time": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds"),
            "fCnt": state.fcnt[dev],
            "deviceInfo": {"devEui": dev, "deviceName": f"FAKE-{state.room}-{i}",
                           "tags": {"location": location_name(state.room), "point": POINTS[i % len(POINTS)]}},
            "object": {"co2": min(round(co2_max), round(state.co2 + rng.uniform(-15, 15))),
                       "temperature": round(state.temperature + rng.uniform(-0.2, 0.2), 1),
                       "humidity": round(state.humidity + rng.uniform(-1, 1), 1)},
        }
        out.append((f"application/fake/device/{dev}/event/up", json.dumps(body, ensure_ascii=False).encode()))
    return out


def parse_rooms(text):
    """FAKE_ROOMS="1,2,3" → [1, 2, 3]."""
    return [int(x) for x in (p.strip() for p in text.split(",")) if x.isascii() and x.isdigit()]


def check_host(host):
    """젯슨 안 브로커가 아니면 멈춘다 (교육장 공용 브로커에 가짜 값을 올리지 않게)."""
    if host not in LOCAL_HOSTS:
        raise SystemExit(f"가짜 센서는 젯슨 안 브로커(localhost)에만 올려요. FAKE_HOST={host!r}는 안 돼요")
    return host


def run(client, rooms, rng, co2_max=950.0, clock=time.time, sleep=time.sleep, rounds=None):
    """PERIOD_SEC마다 방마다 상태를 한 칸 움직이고 업링크를 올린다. rounds를 주면 그만큼만 (시험용)."""
    states = [RoomState(r) for r in rooms]
    n = 0
    while rounds is None or n < rounds:
        now = clock()
        for s in states:
            step(s, rng, co2_max)
            for topic, payload in uplinks(s, now, rng, co2_max):
                client.publish(topic, payload, qos=1)
        print("가짜 센서: " + ", ".join(f"{s.room}번 방 {s.people}명 CO2 {round(s.co2)}ppm "
                                     f"{s.temperature:.1f}℃ {s.humidity:.0f}%" for s in states))
        n += 1
        if rounds is None or n < rounds:
            sleep(PERIOD_SEC)


def main(env=None):
    env = os.environ if env is None else env
    host = check_host(env.get("FAKE_HOST", "localhost"))
    rooms = parse_rooms(env.get("FAKE_ROOMS", "1,2,3"))
    if not rooms:
        raise SystemExit('FAKE_ROOMS에 방 번호를 넣어요. 예: FAKE_ROOMS=1,2,3')
    co2_max = float(env.get("FAKE_CO2_MAX", "950"))
    import paho.mqtt.client as mqtt  # 켤 때만 불러온다

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mspace-fake-sensors")
    client.connect(host, int(env.get("FAKE_PORT", "1883")), keepalive=30)
    client.loop_start()
    print(f"가짜 센서 시작: {host}, 방 {rooms}. 게이트웨이는 MQTT_HOST=localhost MQTT_ROOMS="
          + ",".join(f"{location_name(r)}:{r}" for r in rooms) + " 로 켜요")
    try:
        run(client, rooms, random.Random(), co2_max)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
