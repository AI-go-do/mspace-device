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


def keypad_line(code, seq=1):
    return json.dumps({"eventId": f"1-48213-{seq}", "roomId": 1, "sensorType": "keypad", "code": code, "ms": 10})


def pir_line(value, seq=1):  # 처리기 표에 없는 sensorType 예시
    return json.dumps({"eventId": f"1-48213-{seq}", "roomId": 1, "sensorType": "pir", "value": value, "ms": 10})


def occ_line(pir=1, light=512, seq=1, room=1):  # 우노가 보내는 재실 센서 줄
    return json.dumps({"eventId": f"{room}-48213-{seq}", "roomId": room, "sensorType": "occupancy",
                       "pir": pir, "light": light, "ms": 10})


@pytest.fixture
def run(monkeypatch):
    """가짜 우노 줄을 넣고 main()을 돌린 뒤, (우노에 쓴 값, 서버로 보낸 요청 목록)을 돌려준다."""
    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)  # 테스트에서는 기다리지 않는다

    def _run(lines, responses=None, dry_run=False, token="test-token"):
        config = gateway.Config(port="/dev/fake", dry_run=dry_run, device_token=token)
        ser = FakeSerial(lines)
        monkeypatch.setattr(gateway.serial, "Serial", lambda *a, **k: ser)
        sent = []
        queue = list(responses or [])

        def fake_post(url, json=None, timeout=None, headers=None):
            sent.append((url, json))
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


# ---- 출입 확인 (지영과 맞춘 형식) ----

def test_keypad_opened_sends_ok(run):
    written, sent = run([keypad_line("123456")], [FakeResponse({"result": "OPENED"})])
    assert written == [b"OK\n"]
    url, body = sent[0]
    assert url.endswith("/api/access/verify")
    assert set(body) == {"eventId", "roomId", "deviceId", "code"}  # 4개 필드만
    assert body["deviceId"] == "keypad-1"
    assert body["eventId"] == gateway.to_uuid("1-48213-1")


def test_keypad_denied_sends_no(run, capsys):
    written, _ = run([keypad_line("111111")], [FakeResponse({"result": "DENIED", "reason": "WRONG_CODE"})])
    assert written == [b"NO\n"]
    out = capsys.readouterr().out
    assert "WRONG_CODE" in out  # 거부 이유가 로그에 남는다
    assert "111111" not in out  # 번호(비밀)는 로그에 안 남는다


def test_keypad_server_down_is_denied(run):
    # 서버가 3번 다 실패하면 안전하게 거부한다
    errors = [requests.ConnectionError("down")] * gateway.RETRIES
    written, sent = run([keypad_line("123456")], errors)
    assert written == [b"NO\n"]
    assert len(sent) == gateway.RETRIES
    assert len({s[1]["eventId"] for s in sent}) == 1  # 재시도는 같은 eventId로


def test_retry_then_success(run):
    written, sent = run([keypad_line("123456")], [FakeResponse(status=500), FakeResponse({"result": "OPENED"})])
    assert written == [b"OK\n"]
    assert len(sent) == 2


# ---- 키패드가 아닌 줄 ----

def test_ignores_non_keypad_events(run):
    # 재실 센서 전달은 아직 안 한다 (다음 단계). 서버로 아무것도 안 보낸다
    written, sent = run([pir_line(1)])
    assert sent == [] and written == []


def test_skips_non_json_lines(run, capsys):
    _, sent = run(["", "hello", '{"code":"654321"', keypad_line("123456")], [FakeResponse({"result": "OPENED"})])
    assert len(sent) == 1  # 깨진 줄은 건너뛰고 정상 줄만 보낸다
    out = capsys.readouterr().out
    assert "JSON 아님" in out
    assert "654321" not in out  # 깨진 줄에 섞인 번호도 로그에 안 남는다


def test_empty_response_body_is_denied(run):
    written, _ = run([keypad_line("123456")], [FakeResponse(None)])
    assert written == [b"NO\n"]  # 서버가 빈 응답이면 안전하게 거부


def test_non_dict_response_is_denied(run):
    written, _ = run([keypad_line("123456")], [FakeResponse(["OPENED"])])
    assert written == [b"NO\n"]  # 응답 형식이 이상해도 멈추지 않고 거부


@pytest.mark.parametrize("bad", [
    {"eventId": "1-1-1", "roomId": 1},                     # code 없음
    {"eventId": None, "roomId": 1, "code": "123456"},      # eventId가 글자가 아님
    {"eventId": "1-1-1", "roomId": True, "code": "123456"},  # roomId가 정수가 아님
    {"eventId": "1-1-1", "roomId": 1, "code": "12a456"},   # code가 숫자 6자리가 아님
    {"eventId": "1-1-1", "roomId": 1, "code": "²²²²²²"},   # 0~9가 아닌 숫자 글자
    {"eventId": "\ud800", "roomId": 1, "code": "123456"},  # 깨진 글자 (UUID 변환에서 오류 나던 값)
])
def test_skips_malformed_keypad_line(run, bad):
    line = json.dumps({**bad, "sensorType": "keypad"})
    written, sent = run([line, keypad_line("123456")], [FakeResponse({"result": "OPENED"})])
    assert written == [b"OK\n"] and len(sent) == 1  # 이상한 줄은 건너뛰고 다음 줄은 처리


def test_no_wait_after_last_retry(run, monkeypatch):
    waits = []
    monkeypatch.setattr(gateway.time, "sleep", waits.append)
    run([keypad_line("123456")], [requests.ConnectionError("down")] * gateway.RETRIES)
    assert waits == [2, 1]  # 우노 켜짐 대기 2초, 재시도 전 1초. 마지막 실패 뒤에는 기다리지 않는다


def test_4xx_is_not_retried(run, capsys):
    written, sent = run([keypad_line("123456")], [FakeResponse({}, status=401)])
    assert written == [b"NO\n"]
    assert len(sent) == 1  # 401은 다시 보내도 같으니 한 번만 보낸다
    assert "401" in capsys.readouterr().out


def test_5xx_is_retried(run):
    written, sent = run([keypad_line("123456")], [FakeResponse({}, status=503), FakeResponse({"result": "OPENED"})])
    assert written == [b"OK\n"] and len(sent) == 2  # 서버 오류는 다시 보내서 성공


def test_missing_token_exits():
    with pytest.raises(SystemExit):
        gateway.main(gateway.Config(port="/dev/fake", dry_run=False, device_token=None))


# ---- 서버 없이 테스트 (DRY_RUN) ----

def test_dry_run(run, capsys):
    # DRY_RUN은 토큰 없이 된다
    written, sent = run([keypad_line("123456", 1), keypad_line("999999", 2), pir_line(1, 3)], dry_run=True, token=None)
    assert written == [b"OK\n", b"NO\n"]
    assert sent == []  # 서버로는 아무것도 안 보낸다
    assert "[DRY_RUN]" in capsys.readouterr().out


def test_no_port_exits():
    with pytest.raises(SystemExit):
        gateway.main(gateway.Config(port=None, dry_run=True))


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


def test_handle_line_without_serial():
    v = FakeVerifier({"result": "OPENED"})
    assert gateway.handle_line(keypad_line("123456") + "\n", v) == b"OK\n"
    assert v.bodies[0]["code"] == "123456"


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
    written, sent = run([occ_line(pir=1)], dry_run=True, token=None)
    assert written == [] and sent == []  # 우노에 답하지 않고, 서버로도 아직 안 보낸다
    out = capsys.readouterr().out
    assert "재실 1번 방: 있음" in out
    assert '"kind": "OCCUPANCY"' in out


def test_occupancy_not_sent_to_server_yet(run, capsys):
    written, sent = run([occ_line(pir=0)])
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


def test_occupancy_between_keypad_lines(run):
    written, _ = run([keypad_line("123456", 1), occ_line(seq=2), keypad_line("999999", 3)], dry_run=True, token=None)
    assert written == [b"OK\n", b"NO\n"]  # 센서 줄이 끼어도 키패드 답 순서는 그대로
