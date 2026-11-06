"""qr_door.py 테스트. 카메라·OpenCV·서버 없이 가짜로 바꿔 끼운다."""
import pytest

import gateway
import qr_door
from qr_door import QrDoor, parse_qr, run_camera


class FakeVerifier:
    """보낸 body를 모아 두고 정해 둔 답을 돌려준다."""

    def __init__(self, answer):
        self.answer = answer
        self.bodies = []

    def verify(self, body):
        self.bodies.append(body)
        return self.answer


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.mark.parametrize("text, expected", [
    ("123456", (1, "123456")),
    (" 123456\n", (1, "123456")),
    ("MSPACE:1:000123", (1, "000123")),
    ("MSPACE:2:123456", None),      # 다른 방 QR
    ("12345", None),                # 5자리
    ("1234567", None),              # 7자리
    ("https://evil.example/123456", None),
    ("", None),
    (None, None),
])
def test_parse_qr(text, expected):
    assert parse_qr(text, 1) == expected


def test_opened_sends_same_fields_as_keypad():
    v = FakeVerifier({"result": "OPENED"})
    assert QrDoor(v, 1).handle("123456") == b"OK\n"
    body = v.bodies[0]
    assert set(body) == {"eventId", "roomId", "deviceId", "code"}
    assert body["roomId"] == 1 and body["deviceId"] == "qr-1" and body["code"] == "123456"
    assert len(body["eventId"]) == 36


@pytest.mark.parametrize("answer", [{"result": "DENIED", "reason": "WRONG_CODE"}, None, "이상한 답", {}])
def test_not_opened_is_no(answer):
    assert QrDoor(FakeVerifier(answer), 1).handle("123456") == b"NO\n"


def test_foreign_qr_is_no_without_asking_server():
    v = FakeVerifier({"result": "OPENED"})
    assert QrDoor(v, 1).handle("hello") == b"NO\n"
    assert v.bodies == []


def test_same_qr_is_ignored_for_5_seconds():
    v, clock = FakeVerifier({"result": "OPENED"}), Clock()
    door = QrDoor(v, 1, clock=clock)
    assert door.handle("123456") == b"OK\n"
    clock.t += 4.9
    assert door.handle("123456") is None   # 계속 비추는 중
    clock.t += 0.2
    assert door.handle("123456") == b"OK\n"  # 5초 지나면 다시 묻는다
    assert door.handle("654321") == b"OK\n"  # 다른 QR은 바로
    assert len(v.bodies) == 3


def test_empty_text_is_ignored():
    assert QrDoor(FakeVerifier({"result": "OPENED"}), 1).handle("") is None


class FakeCam:
    def __init__(self, frames):
        self.frames = list(frames)

    def read(self):
        if not self.frames:
            raise StopIteration  # 루프를 끝내는 신호
        return self.frames.pop(0)


class FakeDetector:
    def detectAndDecode(self, frame):
        return frame, None, None  # 테스트에선 frame 자리에 QR 글자를 바로 넣는다


def test_run_camera_passes_replies(monkeypatch):
    monkeypatch.setattr(qr_door.time, "sleep", lambda s: None)
    replies = []
    door = QrDoor(FakeVerifier({"result": "OPENED"}), 1)
    cam = FakeCam([(True, ""), (False, None), (True, "123456")])
    with pytest.raises(StopIteration):
        run_camera(door, 0, replies.append, frames=(cam, FakeDetector()))
    assert replies == [b"OK\n"]


def test_load_config_qr(monkeypatch):
    monkeypatch.setattr(gateway, "find_port", lambda: None)
    c = gateway.load_config({"QR_CAMERA": "0", "QR_ROOM_ID": "2"})
    assert c.qr_camera == "0" and c.qr_room_id == 2
    c = gateway.load_config({"QR_ROOM_ID": "abc"})
    assert c.qr_camera is None and c.qr_room_id == 1


def test_start_qr_runs_in_background(monkeypatch):
    called = {}

    def fake_run(door, device, on_reply):
        called.update(room=door.room_id, device=device)

    monkeypatch.setattr(qr_door, "run_camera", fake_run)
    cfg = gateway.Config(qr_camera="1", qr_room_id=3)
    gateway.start_qr(cfg, FakeVerifier({}), lambda r: None).join(timeout=1)
    assert called == {"room": 3, "device": "1"}


def test_main_starts_qr_and_replies_go_to_uno(monkeypatch):
    """QR_CAMERA가 있으면 main()이 QR을 켜고, QR 답도 같은 우노 시리얼로 간다."""
    from test_gateway import FakeSerial

    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)
    ser = FakeSerial([])
    monkeypatch.setattr(gateway.serial, "Serial", lambda *a, **k: ser)
    started = {}

    def fake_start(config, verifier, on_reply):
        started["camera"] = config.qr_camera
        on_reply(b"OK\n")  # 카메라가 QR을 읽어 열림이 나왔다고 치고

    monkeypatch.setattr(gateway, "start_qr", fake_start)
    with pytest.raises(StopIteration):
        gateway.main(gateway.Config(port="/dev/fake", dry_run=True, qr_camera="0"))
    assert started == {"camera": "0"}
    assert ser.written == [b"OK\n"]
