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


def test_opened_sends_access_verify_fields():
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

    def fake_run(door, device, on_reply, frames):
        called.update(room=door.room_id, device=device, frames=frames)

    monkeypatch.setattr(qr_door, "open_camera", lambda d: "cam")
    monkeypatch.setattr(qr_door, "run_camera", fake_run)
    cfg = gateway.Config(qr_camera="1", qr_room_id=3)
    gateway.start_qr(cfg, FakeVerifier({}), lambda r: None).join(timeout=1)
    assert called == {"room": 3, "device": "1", "frames": "cam"}


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


def test_run_camera_keeps_going_after_error(monkeypatch):
    class Boom(FakeDetector):
        calls = 0

        def detectAndDecode(self, frame):
            Boom.calls += 1
            if Boom.calls == 1:
                raise RuntimeError("깨진 사진")
            return super().detectAndDecode(frame)

    replies = []
    door = QrDoor(FakeVerifier({"result": "OPENED"}), 1)
    cam = FakeCam([(True, "x"), (True, "123456")])
    with pytest.raises(StopIteration):
        run_camera(door, 0, replies.append, frames=(cam, Boom()))
    assert replies == [b"OK\n"]  # 첫 장 오류 뒤에도 다음 QR을 처리함


def test_start_qr_stops_when_camera_missing(monkeypatch):
    """카메라를 못 열면 스레드에서 조용히 죽지 않고 시작할 때 바로 멈춘다."""
    def no_cam(device):
        raise SystemExit("웹캠을 열 수 없어요")

    monkeypatch.setattr(qr_door, "open_camera", no_cam)
    with pytest.raises(SystemExit):
        gateway.start_qr(gateway.Config(qr_camera="9"), FakeVerifier({}), lambda r: None)


def test_parse_qr_only_ascii_digits():
    assert parse_qr("١٢٣٤٥٦", 1) is None  # 아랍 숫자 모양은 받지 않음


def test_load_config_ignores_odd_digits():
    cfg = gateway.load_config({"QR_ROOM_ID": "²", "ROOM_CAMERA_ROOM": "²"})
    assert cfg.qr_room_id == gateway.Config.qr_room_id
    assert cfg.room_camera_room == gateway.Config.room_camera_room


def test_reply_error_is_retried_not_dropped(monkeypatch, capsys):
    monkeypatch.setattr(qr_door.time, "sleep", lambda s: None)
    sent, fails = [], [1]

    def flaky(reply):
        if fails:
            fails.pop()
            raise OSError("시리얼 끊김")
        sent.append(reply)

    door = QrDoor(FakeVerifier({"result": "OPENED"}), 1)
    with pytest.raises(StopIteration):
        run_camera(door, 0, flaky, frames=(FakeCam([(True, "123456")]), FakeDetector()))
    assert sent == [b"OK\n"]
    assert "1/3" in capsys.readouterr().out


def test_reply_gives_up_loudly(monkeypatch, capsys):
    monkeypatch.setattr(qr_door.time, "sleep", lambda s: None)

    def broken(reply):
        raise OSError("없음")

    assert qr_door.send_reply(broken, b"OK\n") is False
    assert "끝내 못 보냈어요" in capsys.readouterr().out
