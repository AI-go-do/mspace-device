"""room_env.py 테스트 (교육장 MQTT 센서 → 방 CO2·온도·습도)."""
import json
from datetime import datetime, timezone

import pytest
import room_env

import gateway

T0 = datetime(2026, 11, 9, 2, 0, 0, tzinfo=timezone.utc).timestamp()


def uplink(dev="a1", fcnt=1, at=T0, location="실습실", co2=900, temp=24.3, hum=61.5, **extra):
    obj = {"co2": co2, "temperature": temp, "humidity": hum, **extra}
    return json.dumps({
        "time": datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="milliseconds"),
        "fCnt": fcnt,
        "deviceInfo": {"devEui": dev, "deviceName": "AM107-" + dev,
                       "tags": {"location": location, "point": "후면 오른쪽"}},
        "object": {k: v for k, v in obj.items() if v is not None},
    }).encode()


def make(location="실습실"):
    pushed, logs = [], []
    env = room_env.RoomEnv(location, 1, "sensor-1", pushed.append, log=logs.append)
    return env, pushed, logs


@pytest.mark.parametrize("text,expected", [
    ("2026-10-20T07:48:42.752+00:00", datetime(2026, 10, 20, 7, 48, 42, 752000, tzinfo=timezone.utc)),
    ("2026-10-20T07:48:42Z", datetime(2026, 10, 20, 7, 48, 42, tzinfo=timezone.utc)),
    ("2026-10-20T07:48:42.123456789+00:00", datetime(2026, 10, 20, 7, 48, 42, 123456, tzinfo=timezone.utc)),
])
def test_parse_time(text, expected):
    assert room_env.parse_time(text) == expected.timestamp()


@pytest.mark.parametrize("text", [None, "", "어제", "2026-10-20T07:48:42", 123])
def test_parse_time_bad(text):
    assert room_env.parse_time(text) is None  # 시간대 없는 시각도 쓰지 않는다


def test_parse_uplink_reads_fields():
    r = room_env.parse_uplink(uplink(tvoc=284), received=T0 + 1)
    assert (r.dev, r.fcnt, r.location, r.point) == ("a1", 1, "실습실", "후면 오른쪽")
    assert (r.co2, r.temperature, r.humidity) == (900, 24.3, 61.5)
    assert r.at == T0
    assert r.keys == ("co2", "humidity", "temperature", "tvoc")


def test_future_time_uses_received():
    r = room_env.parse_uplink(uplink(at=T0 + 31), received=T0)
    assert r.at == T0
    r = room_env.parse_uplink(uplink(at=T0 + 29), received=T0)
    assert r.at == T0 + 29  # 30초 안쪽은 센서 시각 그대로


@pytest.mark.parametrize("co2,temp,hum,expected", [
    (20000, 24.0, 50.0, (None, 24.0, 50.0)),   # core 범위 밖 값은 그 값만 뺀다
    (800, 120.0, -3.0, (800, None, None)),
    ("800", True, None, (None, None, None)),     # 숫자가 아니면 뺀다
    (812.6, 24.0, 50.0, (813, 24.0, 50.0)),     # ppm은 정수로
])
def test_parse_uplink_ranges(co2, temp, hum, expected):
    r = room_env.parse_uplink(uplink(co2=co2, temp=temp, hum=hum), received=T0)
    assert (r.co2, r.temperature, r.humidity) == expected


@pytest.mark.parametrize("payload", [
    b"not json",
    b"[]",
    json.dumps({"object": {"co2": 1}}).encode(),                          # deviceInfo 없음
    json.dumps({"deviceInfo": {"devEui": "a"}}).encode(),                 # object 없음
    json.dumps({"deviceInfo": {"devEui": ""}, "object": {}}).encode(),
])
def test_bad_message_is_logged_not_raised(payload):
    env, pushed, logs = make()
    assert env.on_message("application/1/device/x/event/up", payload, received=T0) is None
    assert any("형식 안 맞음" in line for line in logs)
    assert env.tick(T0) is None and pushed == []


def test_duplicate_fcnt_counted_once():
    env, _, _ = make()
    assert env.on_message("t", uplink(fcnt=7), received=T0) is not None
    assert env.on_message("t", uplink(fcnt=7), received=T0) is None  # 게이트웨이 2대
    assert env.on_message("t", uplink(fcnt=8), received=T0) is not None


def test_seen_is_bounded(monkeypatch):
    monkeypatch.setattr(room_env, "SEEN_MAX", 3)
    env, _, _ = make()
    for n in range(5):
        env.on_message("t", uplink(fcnt=n), received=T0)
    assert len(env.seen) == 3


def test_other_location_ignored_but_shown_once():
    env, pushed, logs = make()
    env.on_message("t", uplink(dev="b", location="사무실", fcnt=1), received=T0)
    env.on_message("t", uplink(dev="b", location="사무실", fcnt=2), received=T0)
    assert env.tick(T0) is None and pushed == []
    assert sum("새 센서 b" in line for line in logs) == 1  # object 키는 기기마다 처음 한 번만


def test_average_per_device_latest_then_mean():
    env, pushed, _ = make()
    # 기기 a는 두 번 보냄 → 최근 값(1000)만, 기기 b는 600 → 평균 800
    env.on_message("t", uplink(dev="a", fcnt=1, at=T0, co2=400, temp=20.0, hum=40.0), received=T0)
    env.on_message("t", uplink(dev="a", fcnt=2, at=T0 + 60, co2=1000, temp=24.0, hum=50.0), received=T0 + 60)
    env.on_message("t", uplink(dev="b", fcnt=1, at=T0 + 30, co2=600, temp=22.0, hum=None), received=T0 + 30)
    body = env.tick(T0 + 61)
    assert body["ppm"] == 800
    assert body["temperatureC"] == 23.0
    assert body["humidityPct"] == 50.0  # 습도 없는 기기는 습도 평균에서만 뺀다
    assert body["kind"] == "CO2" and body["roomId"] == 1 and body["deviceId"] == "sensor-1"
    assert body["source"] == "mqtt"
    assert body["observedAt"] == "2026-11-09T11:01:00+09:00"  # 가장 최근 측정 시각, 한국 시각
    assert pushed == [body]


def test_late_old_reading_does_not_replace_newer():
    env, _, _ = make()
    env.on_message("t", uplink(dev="a", fcnt=2, at=T0 + 60, co2=1000), received=T0 + 60)
    env.on_message("t", uplink(dev="a", fcnt=1, at=T0, co2=400), received=T0 + 61)  # 늦게 온 옛 값
    assert env.summary(T0 + 61)["ppm"] == 1000


def test_stale_devices_left_out():
    env, _, _ = make()
    env.on_message("t", uplink(dev="a", fcnt=1, at=T0, co2=2000), received=T0)
    env.on_message("t", uplink(dev="b", fcnt=1, at=T0 + 301, co2=500), received=T0 + 301)
    assert env.summary(T0 + 301)["ppm"] == 500  # a는 5분 넘게 안 와서 빠짐
    assert env.summary(T0 + 700) is None


def test_sends_every_minute_only_with_new_values():
    env, pushed, _ = make()
    env.on_message("t", uplink(fcnt=1), received=T0)
    assert env.tick(T0) is not None
    env.on_message("t", uplink(fcnt=2, at=T0 + 10), received=T0 + 10)
    assert env.tick(T0 + 30) is None            # 1분이 안 됨
    assert env.tick(T0 + 60) is not None
    assert env.tick(T0 + 130) is None           # 새 값이 없으면 같은 값을 다시 보내지 않는다
    assert len(pushed) == 2


def test_no_co2_no_event():
    env, pushed, _ = make()
    env.on_message("t", uplink(co2=None), received=T0)
    assert env.tick(T0) is None and pushed == []  # core CO2 이벤트는 ppm이 꼭 있어야 한다


def test_eventids_unique():
    env, pushed, _ = make()
    env.on_message("t", uplink(fcnt=1), received=T0)
    env.tick(T0)
    env.on_message("t", uplink(fcnt=2, at=T0 + 60), received=T0 + 60)
    env.tick(T0 + 60)
    assert pushed[0]["eventId"] != pushed[1]["eventId"]


class FakeReason:
    def __init__(self, failure):
        self.is_failure = failure

    def __str__(self):
        return "Not authorized" if self.is_failure else "Success"


class FakeMqttClient:
    def __init__(self, api, client_id=None, clean_session=None):
        self.api, self.client_id, self.clean_session = api, client_id, clean_session
        self.subscribed, self.published, self.auth, self.connected = [], [], None, None

    def username_pw_set(self, user, password):
        self.auth = (user, password)

    def reconnect_delay_set(self, min_delay, max_delay):
        self.delay = (min_delay, max_delay)

    def connect_async(self, host, port, keepalive):
        self.connected = (host, port, keepalive)

    def subscribe(self, topic, qos):
        self.subscribed.append((topic, qos))

    def publish(self, *a, **k):  # 쓰이면 안 된다
        self.published.append(a)


class FakeMqtt:
    class CallbackAPIVersion:
        VERSION2 = "v2"
    Client = FakeMqttClient


class Msg:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload


def test_make_client_subscribe_only():
    env, _, _ = make()
    c = room_env.make_client(env, "mspace-x-reader", "broker", "user", "pw", mqtt=FakeMqtt)
    assert c.client_id == "mspace-x-reader" and c.clean_session is False and c.api == "v2"
    assert c.auth == ("user", "pw") and c.connected == ("broker", 1883, 30) and c.delay == (1, 5)
    c.on_connect(c, None, None, FakeReason(False), None)
    assert c.subscribed == [(room_env.TOPIC, 1)]
    c.on_message(c, None, Msg("application/1/device/a/event/up", uplink(fcnt=3)))
    assert env.latest["a1"].fcnt == 3
    assert c.published == []


def test_make_client_rejected_does_not_subscribe(capsys):
    env, _, _ = make()
    c = room_env.make_client(env, "id", "broker", None, None, mqtt=FakeMqtt)
    assert c.auth is None  # 아이디 없으면 익명
    c.on_connect(c, None, None, FakeReason(True), None)
    assert c.subscribed == []
    assert "접속 거절" in capsys.readouterr().out


def test_run_ticks_survives_error():
    class Boom:
        calls = 0

        def tick(self):
            Boom.calls += 1
            raise RuntimeError("x")

    class Stop:
        def is_set(self):
            return Boom.calls >= 2

    room_env.run_ticks(Boom(), sleep=lambda s: None, stop=Stop())
    assert Boom.calls == 2


def test_config_reads_mqtt_env():
    c = gateway.load_config({"PORT": "x", "MQTT_ROOM": "1", "MQTT_HOST": "h", "MQTT_USERNAME": "u",
                             "MQTT_PASSWORD": "secret", "MQTT_LOCATION": "회의실", "MQTT_PORT": "1884"})
    assert (c.mqtt_room, c.mqtt_host, c.mqtt_username, c.mqtt_location, c.mqtt_port) == (1, "h", "u", "회의실", 1884)
    assert "secret" not in repr(c)  # 비밀번호는 화면에 안 찍힌다
    off = gateway.load_config({"PORT": "x"})
    assert off.mqtt_room is None and off.mqtt_location == "실습실"


def test_start_room_env_wiring():
    made = {}

    def fake_make(env, client_id, host, user, pw, port):
        made.update(env=env, client_id=client_id, host=host, user=user, pw=pw, port=port)
        return "client"

    config = gateway.Config(dry_run=True, mqtt_room=2, mqtt_host="h", mqtt_username="u", mqtt_password="p",
                            mqtt_client_id="me-reader")
    env, client = gateway.start_room_env(config, None, make_client=fake_make, start_thread=False)
    assert client == "client"
    assert (made["client_id"], made["host"], made["user"], made["pw"], made["port"]) == ("me-reader", "h", "u", "p", 1883)
    assert env.room_id == 2 and env.device_id == "sensor-2" and env.location == "실습실"


def test_start_room_env_needs_host(capsys):
    assert gateway.start_room_env(gateway.Config(mqtt_room=1), None, start_thread=False) is None
    assert "MQTT_HOST" in capsys.readouterr().out
