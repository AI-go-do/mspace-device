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


def pir_line(value, seq=1):
    return json.dumps({"eventId": f"1-48213-{seq}", "roomId": 1, "sensorType": "pir", "value": value, "ms": 10})


@pytest.fixture
def run(monkeypatch):
    """가짜 우노 줄을 넣고 main()을 돌린 뒤, (우노에 쓴 값, 서버로 보낸 요청 목록)을 돌려준다."""
    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)  # 테스트에서는 기다리지 않는다
    monkeypatch.setattr(gateway, "PORT", "/dev/fake")

    def _run(lines, responses=None, dry_run=False):
        monkeypatch.setattr(gateway, "DRY_RUN", dry_run)
        ser = FakeSerial(lines)
        monkeypatch.setattr(gateway.serial, "Serial", lambda *a, **k: ser)
        sent = []
        queue = list(responses or [])

        def fake_post(url, json=None, timeout=None):
            sent.append((url, json))
            r = queue.pop(0) if queue else FakeResponse({})
            if isinstance(r, Exception):
                raise r
            return r

        monkeypatch.setattr(gateway.requests, "post", fake_post)
        with pytest.raises(StopIteration):
            gateway.main()
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
    assert "WRONG_CODE" in capsys.readouterr().out  # 거부 이유가 로그에 남는다


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


# ---- 센서 이벤트 ----

def test_sensor_event_posted_with_received_at(run):
    _, sent = run([pir_line(1)])
    url, body = sent[0]
    assert url.endswith("/api/sensor-events")
    assert body["sensorType"] == "pir" and body["value"] == 1
    assert "receivedAt" in body
    assert body["eventId"] == gateway.to_uuid("1-48213-1")


def test_skips_non_json_lines(run, capsys):
    _, sent = run(["", "hello", "{broken", pir_line(0)])
    assert len(sent) == 1  # 깨진 줄은 건너뛰고 정상 줄만 보낸다
    assert "JSON 아님" in capsys.readouterr().out


def test_empty_response_body(run):
    _, sent = run([pir_line(1)], [FakeResponse(None)])
    assert len(sent) == 1


# ---- 서버 없이 테스트 (DRY_RUN) ----

def test_dry_run(run, capsys):
    written, sent = run([keypad_line("123456", 1), keypad_line("999999", 2), pir_line(1, 3)], dry_run=True)
    assert written == [b"OK\n", b"NO\n"]
    assert sent == []  # 서버로는 아무것도 안 보낸다
    assert "[DRY_RUN]" in capsys.readouterr().out


def test_no_port_exits(monkeypatch):
    monkeypatch.setattr(gateway, "PORT", None)
    with pytest.raises(SystemExit):
        gateway.main()
