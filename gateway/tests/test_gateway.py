"""gateway.py 테스트.

실제 우노와 서버 없이 돌리기 위해 시리얼 포트와 HTTP 요청을 가짜로 바꿔 끼운다.
"""
import json
import uuid

import pytest
import requests

import gateway


class FakeSerial:
    """우노 대신 쓰는 가짜 시리얼. 준비한 줄을 차례로 돌려주고, 다 읽으면 루프를 끝낸다."""

    def __init__(self, lines):
        self.lines = [line.encode() for line in lines]  # 우노가 보낸 것처럼 바이트로 바꿔 둔다
        self.written = []  # 게이트웨이가 우노에 돌려준 값 (OK/NO)

    def readline(self):
        if not self.lines:
            raise StopIteration  # 무한 루프(main)를 끝내는 신호
        return self.lines.pop(0)

    def write(self, data):
        self.written.append(data)


class FakeResponse:
    """requests.post가 돌려주는 응답 흉내"""

    def __init__(self, body=None, status=200):
        self.body = body
        self.status_code = status
        self.content = json.dumps(body).encode() if body is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self.body


def pir_line(value, seq=1):  # 처리기 표에 없는 sensorType 예시
    return json.dumps({"eventId": f"1-48213-{seq}", "roomId": 1, "sensorType": "pir", "value": value, "ms": 10})


def occ_line(pir=1, light=512, seq=1, room=1):  # 우노가 보내는 재실 센서 줄
    return json.dumps({"eventId": f"{room}-48213-{seq}", "roomId": room, "sensorType": "occupancy",
                       "pir": pir, "light": light, "ms": 10})


@pytest.fixture
def run(monkeypatch):
    """가짜 우노 줄을 넣고 main()을 돌린 뒤, (우노에 쓴 값, 서버로 보낸 요청 목록)을 돌려준다."""
    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)  # 테스트에서는 기다리지 않는다

    def _run(lines, responses=None, dry_run=False, token="test-token", use_pir=False):
        config = gateway.Config(port="/dev/fake", dry_run=dry_run, device_token=token, use_pir=use_pir)
        ser = FakeSerial(lines)
        monkeypatch.setattr(gateway.serial, "Serial", lambda *a, **k: ser)
        sent = []
        queue = list(responses or [])

        def fake_post(url, json=None, timeout=None, headers=None, proxies=None, allow_redirects=True):
            sent.append((url, json))
            assert allow_redirects is False  # 리디렉션은 따라가지 않는다
            assert proxies == {"http": None, "https": None}  # 테스트 서버는 localhost라 프록시를 안 거친다
            assert headers == {"Authorization": "Bearer test-token"}  # 장치 토큰을 항상 같이 보낸다
            r = queue.pop(0) if queue else FakeResponse({})
            if isinstance(r, Exception):
                raise r
            return r

        monkeypatch.setattr(gateway.requests, "post", fake_post)
        with pytest.raises(StopIteration):
            gateway.main(config)
        return ser.written, sent

    return _run


# ---- eventId ----

def test_to_uuid_is_valid_and_stable():
    a = gateway.to_uuid("1-48213-17")
    assert uuid.UUID(a)  # UUID 형식이어야 한다
    assert a == gateway.to_uuid("1-48213-17")  # 같은 입력이면 같은 값 (재전송해도 중복 제거 가능)
    assert a != gateway.to_uuid("1-48213-18")


# ---- 출입 확인 (QR 입실이 쓰는 HttpVerifier, 지영과 맞춘 형식) ----

ACCESS_BODY = {"eventId": "67e130b8-f207-50cb-8d21-46c7ff5ac2a5", "roomId": 1, "deviceId": "qr-1", "code": "123456"}


@pytest.fixture
def verify(monkeypatch):
    """가짜 서버 응답을 차례로 주고 HttpVerifier.verify를 부른다. (결과, 보낸 요청, 기다린 초)"""
    def _verify(responses, server="http://127.0.0.1:8411"):
        sent, waits = [], []
        queue = list(responses)
        monkeypatch.setattr(gateway.time, "sleep", waits.append)

        def fake_post(url, json=None, timeout=None, headers=None, proxies=None, allow_redirects=True):
            sent.append((url, json))
            assert allow_redirects is False  # 리디렉션은 따라가지 않는다
            assert proxies == {"http": None, "https": None}  # 테스트 서버는 localhost라 프록시를 안 거친다
            assert headers == {"Authorization": "Bearer test-token"}  # 장치 토큰을 항상 같이 보낸다
            r = queue.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        monkeypatch.setattr(gateway.requests, "post", fake_post)
        return gateway.HttpVerifier(server, "test-token").verify(dict(ACCESS_BODY)), sent, waits

    return _verify


def test_verify_opened(verify):
    res, sent, _ = verify([FakeResponse({"result": "OPENED"})])
    assert res == {"result": "OPENED"}
    assert sent[0] == ("http://127.0.0.1:8411/api/access/verify", ACCESS_BODY)


def test_verify_server_down_gives_none_and_retries_same_event(verify):
    res, sent, waits = verify([requests.ConnectionError("down")] * gateway.RETRIES)
    assert res is None and len(sent) == gateway.RETRIES
    assert len({s[1]["eventId"] for s in sent}) == 1  # 재시도는 같은 eventId로
    assert waits == [1]  # 재시도 전 1초. 마지막 실패 뒤에는 기다리지 않는다


@pytest.mark.parametrize("first", [FakeResponse(status=500), FakeResponse({}, status=503)])
def test_verify_5xx_is_retried(verify, first):
    res, sent, _ = verify([first, FakeResponse({"result": "OPENED"})])
    assert res == {"result": "OPENED"} and len(sent) == 2


def test_verify_4xx_is_not_retried(verify, capsys):
    res, sent, _ = verify([FakeResponse({}, status=401)])
    assert res is None and len(sent) == 1 and "401" in capsys.readouterr().out


def test_verify_redirect_is_not_followed(verify):
    res, sent, _ = verify([FakeResponse({"result": "OPENED"}, status=307)])
    assert res is None and len(sent) == 1  # 3xx는 열지 않고, 다시 보내지도 않는다


def test_verify_empty_body_is_empty_answer(verify):
    assert verify([FakeResponse(None)])[0] == {}  # QrDoor가 빈 답을 거부로 본다


def test_dry_run_verifier_opens_only_123456(capsys):
    v = gateway.DryRunVerifier()
    assert v.verify(ACCESS_BODY) == {"result": "OPENED"}
    assert v.verify({**ACCESS_BODY, "code": "999999"})["result"] == "DENIED"
    out = capsys.readouterr().out
    assert "[DRY_RUN]" in out and "123456" not in out  # 번호는 로그에 안 남는다


def test_keypad_line_is_ignored(run):
    """키패드는 뺐다(11/10). 예전 우노가 keypad 줄을 보내도 서버에 묻지 않고, 깨진 줄도 건너뛴다."""
    line = json.dumps({"eventId": "1-1-1", "roomId": 1, "sensorType": "keypad", "code": "123456", "ms": 1})
    written, sent = run([line, "", "hello", '{"code":"654321"', occ_line()])  # 진짜 서버 모드: 요청이 있었다면 sent에 남는다
    assert written == [] and sent == []


def test_missing_token_exits():
    with pytest.raises(SystemExit):
        gateway.main(gateway.Config(port="/dev/fake", dry_run=False, device_token=None))


def test_no_port_exits():
    with pytest.raises(SystemExit):
        gateway.main(gateway.Config(port=None, dry_run=True))


@pytest.mark.parametrize("server, ok", [
    ("https://dev-mspace.java21.net", True),
    ("http://localhost:8411", True),
    ("http://127.0.0.1:8411", True),
    ("http://[::1]:8411", True),
    ("http://dev-mspace.java21.net", False),   # 원격 http는 토큰이 그대로 보여서 안 된다
    ("http://localhost.evil.com", False),      # 앞부분만 localhost인 가짜 주소
    ("http://192.168.0.5:8411", False),        # 같은 와이파이라도 http는 안 된다
    ("ftp://localhost", False),
    ("https://", False),
])
def test_is_safe_server(server, ok):
    assert gateway.is_safe_server(server) is ok


def test_remote_server_uses_env_proxy():
    assert gateway.HttpVerifier("https://dev-mspace.java21.net", "t").proxies is None  # 원격은 원래 설정 그대로
    assert gateway.HttpVerifier("http://127.0.0.1:8080", "t").proxies == {"http": None, "https": None}


def test_remote_http_server_exits():
    with pytest.raises(SystemExit):
        gateway.main(gateway.Config(server="http://dev-mspace.java21.net", port="/dev/fake", device_token="t"))


# ---- 설정 (Config, find_port) ----

def test_load_config_from_env(monkeypatch):
    monkeypatch.setattr(gateway, "find_port", lambda: "/dev/ttyUSB0")
    c = gateway.load_config({"SERVER": "http://core:8080", "DRY_RUN": "1", "DEVICE_TOKEN": "t"})
    assert c == gateway.Config(server="http://core:8080", dry_run=True, device_token="t", port="/dev/ttyUSB0")


def test_load_config_defaults_and_port_override(monkeypatch):
    monkeypatch.setattr(gateway, "find_port", lambda: "/dev/ttyUSB0")
    c = gateway.load_config({"PORT": "/dev/ttyACM9"})
    assert c.server == "http://localhost:8080" and c.dry_run is False and c.device_token is None
    assert c.port == "/dev/ttyACM9"  # PORT를 정하면 자동으로 찾지 않는다


def test_load_config_baseline_file(monkeypatch):
    monkeypatch.setattr(gateway, "find_port", lambda: None)
    assert gateway.load_config({}).baseline_file == "room_baseline.json"
    assert gateway.load_config({"BASELINE_FILE": "/var/b.json"}).baseline_file == "/var/b.json"
    assert gateway.load_config({"BASELINE_FILE": "off"}).baseline_file is None


def test_load_config_desk_area(monkeypatch, capsys):
    monkeypatch.setattr(gateway, "find_port", lambda: None)
    assert gateway.load_config({}).desk_area is None
    assert gateway.load_config({"DESK_AREA": "0.25, 0.22,0.64,0.83"}).desk_area == (0.25, 0.22, 0.64, 0.83)
    for bad in ["0.5,0.5,0.4,0.9", "a,b,c,d", "0.1,0.2,0.3", "0,0,1.5,1"]:
        assert gateway.load_config({"DESK_AREA": bad}).desk_area is None
    assert "DESK_AREA가 이상해서 그 부분 확대는 꺼요" in capsys.readouterr().out
    assert gateway.load_config({"FLOOR_AREA": "0,0.7,1,1"}).floor_area == (0.0, 0.7, 1.0, 1.0)
    assert gateway.load_config({"FLOOR_AREA": "1,1,0,0"}).floor_area is None
    assert "FLOOR_AREA가 이상해서" in capsys.readouterr().out


def test_load_config_reads_os_environ(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(gateway, "find_port", lambda: None)
    assert gateway.load_config().dry_run is True


def test_find_port_order(monkeypatch):
    fake = {"/dev/ttyUSB*": ["/dev/ttyUSB1", "/dev/ttyUSB0"], "/dev/cu.usbserial*": ["/dev/cu.usbserial-1"]}
    monkeypatch.setattr(gateway.glob, "glob", lambda p: fake.get(p, []))
    assert gateway.find_port() == "/dev/ttyUSB0"  # 앞 순서(ttyUSB)가 먼저, 그 안에서는 이름순


def test_find_port_none(monkeypatch):
    monkeypatch.setattr(gateway.glob, "glob", lambda p: [])
    assert gateway.find_port() is None


# ---- handle_line: 시리얼 없이 한 줄만 ----

class FakeVerifier:
    """서버 대신 정해진 답을 주는 확인기"""

    def __init__(self, answer):
        self.answer = answer
        self.bodies = []

    def verify(self, body):
        self.bodies.append(body)
        return self.answer


def test_handle_line_ignores_json_that_is_not_object():
    v = FakeVerifier({"result": "OPENED"})
    assert gateway.handle_line("[1, 2]", v) is None and v.bodies == []


def test_handle_line_unknown_sensor_type():
    v = FakeVerifier({"result": "OPENED"})
    assert gateway.handle_line(pir_line(1), v) is None and v.bodies == []  # 처리기 표에 없으면 건너뜀


@pytest.mark.parametrize("bad_type", [[], {}, None, 1])
def test_handle_line_non_string_sensor_type(bad_type):
    v = FakeVerifier({"result": "OPENED"})
    line = json.dumps({"sensorType": bad_type, "eventId": "1-1-1", "roomId": 1, "code": "123456"})
    assert gateway.handle_line(line, v) is None and v.bodies == []  # 리스트 등은 오류 없이 건너뜀


def test_make_verifier_picks_by_config():
    assert isinstance(gateway.make_verifier(gateway.Config(dry_run=True)), gateway.DryRunVerifier)
    http = gateway.make_verifier(gateway.Config(server="http://core", device_token="t"))
    assert isinstance(http, gateway.HttpVerifier) and http.url == "http://core/api/access/verify"


def test_worst_case_wait_is_shorter_than_uno_wait():
    # 우노의 "확인 중" 기다림(WAIT_MAX_MS 9초)보다 게이트웨이가 먼저 답해야 늦은 OK가 빨강을 뒤집지 않는다
    # requests는 연결 시간과 답 시간을 따로 재므로 둘 다 더한다
    worst = gateway.RETRIES * (gateway.CONNECT_TIMEOUT + gateway.TIMEOUT) + sum(range(1, gateway.RETRIES))
    assert worst <= 7


# ---- 재실 센서 (FR-DEV-101) ----

def test_occupancy_body_matches_spec():
    now = gateway.datetime(2026, 11, 5, 10, 0, 0, tzinfo=gateway.KST)
    body = gateway.occupancy_body(json.loads(occ_line(pir=1, light=300, room=2)), now)
    assert body == {
        "eventId": str(uuid.uuid5(gateway.EVENT_NS, "2-48213-1")),
        "roomId": 2,
        "deviceId": "sensor-2",
        "kind": "OCCUPANCY",
        "occupied": True,
        "sources": {"pir": True, "light": 300},
        "observedAt": "2026-11-05T10:00:00+09:00",
    }
    assert gateway.occupancy_body(json.loads(occ_line(pir=0)), now)["occupied"] is False


def test_occupancy_default_time_is_kst():
    body = gateway.occupancy_body(json.loads(occ_line()))
    assert body["observedAt"].endswith("+09:00")


def test_occupancy_dry_run_prints_body(run, capsys):
    written, sent = run([occ_line(pir=1)], dry_run=True, token=None, use_pir=True)
    assert written == [] and sent == []  # 우노에 답하지 않고, 서버로도 아직 안 보낸다
    out = capsys.readouterr().out
    assert "재실 1번 방: 있음" in out
    assert '"kind": "OCCUPANCY"' in out


def test_occupancy_not_sent_to_server_yet(run, capsys):
    written, sent = run([occ_line(pir=0)], use_pir=True)
    assert written == [] and sent == []
    out = capsys.readouterr().out
    assert "재실 1번 방: 없음" in out and "[DRY_RUN]" not in out


@pytest.mark.parametrize("field,value", [
    ("pir", 2), ("pir", True), ("pir", "1"), ("light", -1), ("light", 1024), ("light", 1.5),
    ("roomId", "1"), ("roomId", True), ("eventId", "abc"), ("eventId", 1),
])
def test_occupancy_bad_line_skipped(field, value, capsys):
    event = json.loads(occ_line())
    event[field] = value
    assert gateway.handle_line(json.dumps(event), gateway.DryRunVerifier()) is None
    assert "형식이 맞지 않는 센서 줄" in capsys.readouterr().out


# ---- 재실 이벤트 전송 (SensorSender) ----

@pytest.fixture
def sender(monkeypatch):
    """가짜 서버 응답을 차례로 돌려주는 SensorSender. (sender, 보낸 요청 목록)"""
    def _make(responses):
        sent = []
        queue = list(responses)

        def fake_post(url, json=None, timeout=None, headers=None, allow_redirects=True):
            sent.append((url, json, allow_redirects))
            assert headers == {"Authorization": "Bearer test-token"}
            r = queue.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        monkeypatch.setattr(gateway.requests, "post", fake_post)
        return gateway.SensorSender("https://core.example", "test-token"), sent
    return _make


def occ_body(seq=1):
    return gateway.occupancy_body(json.loads(occ_line(seq=seq)))


def test_sensor_sender_posts_and_pops(sender):
    s, sent = sender([FakeResponse({"result": "STORED"})])
    s.push(occ_body())
    assert s.step() == 0 and len(s.queue) == 0
    url, body, redirects = sent[0]
    assert url == "https://core.example/api/sensor-events" and body["kind"] == "OCCUPANCY"
    assert redirects is False  # 리디렉션을 따라가지 않는다 (토큰 보호)
    assert s.step() is None  # 큐가 비었으면 None


@pytest.mark.parametrize("status", [401, 403, 404, 408, 429, 500, 503])
def test_sensor_sender_keeps_event_on_config_or_server_error(sender, status):
    s, sent = sender([FakeResponse(status=status), FakeResponse({"result": "STORED"})])
    s.push(occ_body())
    assert s.step() == 1 and len(s.queue) == 1  # 버리지 않고 1초 뒤 다시
    assert s.step() == 0 and len(s.queue) == 0
    assert sent[0][1]["eventId"] == sent[1][1]["eventId"]  # 같은 eventId로 다시 보낸다


@pytest.mark.parametrize("status", [400, 409, 413, 422])
def test_sensor_sender_drops_bad_event(sender, status, capsys):
    s, _ = sender([FakeResponse(status=status)])
    s.push(occ_body())
    assert s.step() == 0 and len(s.queue) == 0
    assert "이것만 버려요" in capsys.readouterr().out


def test_sensor_sender_backoff_doubles_and_caps(sender):
    s, _ = sender([requests.ConnectionError()] * 7 + [FakeResponse({})])
    s.push(occ_body())
    waits = [s.step() for _ in range(7)]
    assert waits == [1, 2, 4, 8, 16, 30, 30]
    assert s.step() == 0 and s.backoff == 1  # 성공하면 다시 1초부터


def test_sensor_sender_queue_drops_oldest_when_full(sender):
    s, _ = sender([])
    for i in range(gateway.SENSOR_QUEUE_MAX + 5):
        s.push({"eventId": str(i)})
    assert len(s.queue) == gateway.SENSOR_QUEUE_MAX
    assert s.queue[0]["eventId"] == "5"  # 가장 오래된 5개가 밀려남


def test_occupancy_pushed_to_sender_when_on():
    pushed = []

    class FakeSender:
        def push(self, body):
            pushed.append(body)

    gateway.handle_line(occ_line(pir=1), gateway.HttpVerifier("https://core.example", "t"), FakeSender())
    assert len(pushed) == 1 and pushed[0]["occupied"] is True


def test_pir_off_by_default_keeps_only_light(run, capsys, monkeypatch):
    """11/12 PIR을 뺌: 기본은 PIR 값을 버리고 조도만 기억한다 (뺀 핀이 아무렇게나 1로 읽혀도 "있음"이 안 되게)."""
    monkeypatch.setattr(gateway, "LAST_PIR", {})
    monkeypatch.setattr(gateway, "LAST_LIGHT", {})
    written, sent = run([occ_line(pir=1, light=700)], dry_run=True, token=None)
    out = capsys.readouterr().out
    assert written == [] and sent == []
    assert "재실 1번 방 조도: 700 (PIR 안 씀" in out and "[DRY_RUN]" not in out and "PIR: 안 씀" in out
    assert gateway.fresh_pir(1) is None and gateway.fresh_light(1) == 700


def test_pir_off_does_not_push_occupancy():
    pushed = []

    class FakeSender:
        def push(self, body):
            pushed.append(body)

    gateway.handle_line(occ_line(pir=1), gateway.HttpVerifier("https://core.example", "t"), FakeSender(), use_pir=False)
    assert pushed == []


def test_load_config_use_pir(monkeypatch):
    monkeypatch.setattr(gateway, "find_port", lambda: None)
    assert gateway.load_config({}).use_pir is False
    assert gateway.load_config({"USE_PIR": "1"}).use_pir is True


def test_load_config_send_sensors(monkeypatch):
    monkeypatch.setattr(gateway, "find_port", lambda: None)
    assert gateway.load_config({"SEND_SENSORS": "1"}).send_sensors is True
    assert gateway.load_config({}).send_sensors is False


def test_remote_http_server_exits():
    cfg = gateway.Config(port="/dev/null", device_token="t", server="http://core.example.com")
    with pytest.raises(SystemExit, match="https"):
        gateway.main(cfg)


# ---- 정리 확인 사진 전송 (PhotoSender, core #115) ----

def test_photo_sender_posts_multipart_and_drops_rejected(monkeypatch, capsys):
    sent = []
    replies = [FakeResponse(status=201), FakeResponse(status=503), FakeResponse(status=201), FakeResponse(status=400)]

    def fake_post(url, data=None, files=None, timeout=None, headers=None, allow_redirects=True):
        sent.append((url, data, files, headers, allow_redirects))
        return replies.pop(0)

    monkeypatch.setattr(gateway.requests, "post", fake_post)
    s = gateway.PhotoSender("https://core.example", "test-token", 2)
    s.push({"reservationId": 7, "phase": "FIRST", "deviceId": "camera-2", "jpg": b"\xff\xd8a"})
    assert s.step() == 0 and not s.queue
    url, data, files, headers, redirects = sent[0]
    assert url == "https://core.example/api/rooms/2/tidy-photos"
    assert data == {"reservationId": "7", "phase": "FIRST", "deviceId": "camera-2"}
    assert files == {"file": ("first.jpg", b"\xff\xd8a", "image/jpeg")}
    assert headers == {"Authorization": "Bearer test-token"} and redirects is False
    s.push({"reservationId": 7, "phase": "RECHECK", "deviceId": "camera-2", "jpg": b"b"})
    assert s.step() == 1 and len(s.queue) == 1  # 서버 문제면 다시
    assert s.step() == 0 and not s.queue
    s.push({"reservationId": 7, "phase": "ENTRY", "deviceId": "camera-2", "jpg": b"c"})
    assert s.step() == 0 and not s.queue  # core가 ENTRY를 아직 안 받으면(400) 그 장만 버림
    assert "이것만 버려요" in capsys.readouterr().out
    assert s.queue.maxlen == gateway.PHOTO_QUEUE_MAX
