import json
import random

import pytest

import fake_env_sensors as fake
import gateway
import room_env


def test_uplinks_look_like_real_sensors():
    """가짜 업링크를 진짜 센서 읽는 코드가 그대로 읽는다."""
    s = fake.RoomState(2, people=2, co2=700)
    out = fake.uplinks(s, 1_700_000_000.0, random.Random(1))
    assert len(out) == 1  # 방 하나에 센서 하나
    topic, payload = out[0]
    assert topic.startswith("application/fake/device/") and topic.endswith("/event/up")
    r = room_env.parse_uplink(payload, 1_700_000_000.0)
    assert r.location == "가상-2" and r.fcnt == 0 and 600 < r.co2 < 800
    assert r.temperature is not None and r.humidity is not None
    assert fake.uplinks(s, 1_700_000_060.0, random.Random(1))[0][1] != payload  # fCnt가 늘어난다


def test_co2_never_passes_max():
    """1000ppm이 넘으면 core가 실제 예약자에게 환기 알림을 보내므로 상한을 지킨다."""
    rng = random.Random(0)
    s = fake.RoomState(1, people=4, co2=900)
    for _ in range(500):
        fake.step(s, rng, co2_max=950)
        assert fake.OUTDOOR_CO2 - 20 <= s.co2 <= 950
        assert 20 <= s.humidity <= 80


def test_empty_room_goes_back_to_outdoor_air():
    s = fake.RoomState(1, people=0, co2=900)

    class NoChange(random.Random):
        def random(self):
            return 0.99  # 사람 수 안 바뀜

    for _ in range(60):
        fake.step(s, NoChange(0))
    assert s.co2 < 500


@pytest.mark.parametrize("host", ["data.withio.net", "10.0.0.5", ""])
def test_refuses_shared_broker(host):
    with pytest.raises(SystemExit):
        fake.check_host(host)


def test_run_publishes_every_room():
    sent = []

    class Client:
        def publish(self, topic, payload, qos):
            sent.append((topic, qos))

    fake.run(Client(), [1, 3], random.Random(0), clock=lambda: 1_700_000_000.0, sleep=lambda s: None, rounds=2)
    assert len(sent) == 2 * 2 and all(q == 1 for _, q in sent)  # 방 2개 × 2번
    assert fake.parse_rooms("1, 2,x,3") == [1, 2, 3]


def test_router_sends_each_room():
    """MQTT 접속 하나로 방 여러 개: 공간 이름이 맞는 방으로만 간다."""
    pushed, logs = [], []
    envs = [room_env.RoomEnv(fake.location_name(r), r, f"sensor-{r}", pushed.append, log=logs.append) for r in (1, 2)]
    router = room_env.RoomRouter(envs, log=logs.append)
    now = 1_700_000_000.0
    rng = random.Random(0)
    for r in (1, 2):
        for topic, payload in fake.uplinks(fake.RoomState(r, co2=500 + r * 100), now, rng):
            router.on_message(topic, payload, received=now)
    other = fake.uplinks(fake.RoomState(9), now, rng)[0]
    router.on_message(*other, received=now)
    router.on_message("t", b"not json", received=now)
    sent = router.tick(now + 1)
    assert sorted(b["roomId"] for b in sent) == [1, 2]
    assert {b["deviceId"] for b in sent} == {"sensor-1", "sensor-2"}
    assert any("가상-9" in line for line in logs) and any("형식 안 맞음" in line for line in logs)


def test_mqtt_rooms_config_and_wiring():
    c = gateway.load_config({"PORT": "x", "MQTT_ROOMS": "가상-1:1, 가상-2:2, 잘못", "MQTT_HOST": "localhost"})
    assert c.mqtt_rooms == (("가상-1", 1), ("가상-2", 2))
    assert gateway.env_rooms(c) == c.mqtt_rooms
    assert gateway.env_rooms(gateway.Config(mqtt_room=3)) == (("실습실", 3),)
    assert gateway.env_rooms(gateway.Config()) == ()
    made = {}

    def fake_make(env, client_id, host, user, pw, port):
        made.update(env=env, host=host)
        return "client"

    env, _ = gateway.start_room_env(gateway.Config(dry_run=True, mqtt_rooms=c.mqtt_rooms, mqtt_host="localhost"),
                                    None, make_client=fake_make, start_thread=False)
    assert isinstance(env, room_env.RoomRouter) and made["host"] == "localhost"
    assert sorted(e.room_id for e in env.envs.values()) == [1, 2]


def test_published_co2_stays_under_max():
    """방 상태가 상한에 닿아도 센서마다 더하는 흔들림 때문에 넘지 않는다."""
    s = fake.RoomState(1, people=4, co2=950)
    for seed in range(50):
        for _, payload in fake.uplinks(s, 1_700_000_000.0, random.Random(seed), co2_max=950):
            assert json.loads(payload)["object"]["co2"] <= 950


def test_duplicate_location_is_skipped():
    assert gateway.parse_mqtt_rooms("가상-1:1,가상-1:2,가상-2:3") == (("가상-1", 1), ("가상-2", 3))


def test_each_room_gets_its_own_sender():
    """한 방이 403으로 막혀도 다른 방이 줄 뒤에서 기다리지 않게 방마다 전송 줄을 따로 둔다."""
    made = {}

    class Sender:
        def __init__(self, room):
            self.room, self.sent = room, []

        def push(self, body):
            self.sent.append(body)

    def make_sender(room):
        made[room] = Sender(room)
        return made[room]

    shared = Sender(0)
    config = gateway.Config(mqtt_rooms=(("가상-1", 1), ("가상-2", 2)), mqtt_host="localhost")
    env, _ = gateway.start_room_env(config, shared, make_client=lambda *a, **k: "c", start_thread=False,
                                    make_sender=make_sender)
    assert sorted(made) == [1, 2]
    for e in env.envs.values():
        e.push({"roomId": e.room_id})
    assert made[1].sent == [{"roomId": 1}] and made[2].sent == [{"roomId": 2}] and shared.sent == []
    one, _ = gateway.start_room_env(gateway.Config(mqtt_room=1, mqtt_host="localhost"), shared,
                                    make_client=lambda *a, **k: "c", start_thread=False, make_sender=make_sender)
    one.push({"roomId": 1})
    assert shared.sent == [{"roomId": 1}]  # 방 하나면 원래대로 같은 줄
