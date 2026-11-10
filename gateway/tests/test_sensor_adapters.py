import importlib
import json
import random

import pytest

import fake_env_sensors as fake
import gateway
import room_env
import sensor_adapters as sa
from room_camera import RoomWatcher


def occupancy_from_uno():
    return gateway.occupancy_body({"eventId": "abc-1", "roomId": 2, "sensorType": "occupancy", "pir": 1, "light": 300})


def occupancy_from_camera():
    return RoomWatcher(3).event(2, 1, "2026-11-10T12:00:00+09:00")


def co2_from_mqtt():
    """가짜 센서 업링크 → RoomEnv(실습실 센서와 같은 길) → core CO2 이벤트."""
    pushed = []
    env = room_env.RoomEnv(fake.location_name(1), 1, "sensor-1", pushed.append, log=lambda *_: None)
    now = 1_700_000_000.0
    for topic, payload in fake.uplinks(fake.RoomState(1, co2=600), now, random.Random(0)):
        env.on_message(topic, payload, received=now)
    env.tick(now + 1)
    assert len(pushed) == 1
    return pushed[0]


@pytest.mark.parametrize("make", [occupancy_from_uno, occupancy_from_camera, co2_from_mqtt])
def test_every_adapter_sends_the_common_shape(make):
    """센서가 달라도 core로 나가는 모양은 같다 (어댑터 패턴)."""
    body = make()
    assert sa.check_event(body) == []
    json.dumps(body)  # 그대로 보낼 수 있다


def test_adapter_table_points_to_real_code():
    """ADAPTERS 표가 실제 함수 이름과 어긋나지 않게 한다."""
    assert {a.kind for a in sa.ADAPTERS} == set(sa.REQUIRED)
    for a in sa.ADAPTERS:
        target = importlib.import_module(a.module)
        for part in a.name.split("."):
            target = getattr(target, part)
        assert callable(target)


@pytest.mark.parametrize("body, problem", [
    ("x", "dict가 아님"),
    ({"kind": "NOISE"}, "모르는 kind"),
    ({**{"kind": "CO2", "eventId": "x", "roomId": True, "deviceId": "d", "ppm": 500, "source": "mqtt",
         "observedAt": "2026-11-10T12:00:00"}}, "roomId"),
])
def test_check_event_catches_bad_shape(body, problem):
    assert any(problem in p for p in sa.check_event(body))


def test_check_event_catches_details():
    good = co2_from_mqtt()
    assert any("UUID" in p for p in sa.check_event({**good, "eventId": "not-uuid"}))
    assert any("시간대" in p for p in sa.check_event({**good, "observedAt": "2026-11-10T12:00:00"}))
    assert any("모르는 키" in p for p in sa.check_event({**good, "people": 3}))
    assert any("temperatureC" in p for p in sa.check_event({**good, "temperatureC": "22"}))
