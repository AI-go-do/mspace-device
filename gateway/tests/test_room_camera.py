"""room_camera.py 테스트. 카메라·OpenCV·모델 없이 찾은 목록을 직접 넣는다."""
import json

import numpy as np
import pytest

import gateway
import room_camera
from room_camera import (BEAT_SEC, EMPTY_CHECK_SEC, Detection, RoomWatcher, check_message, iou, nms,
                         parse_yolo, run_room_camera)


def person(x=0):
    return Detection("person", 0.9, (x, 0, 50, 100))


def thing(label, x=200, y=200):
    return Detection(label, 0.8, (x, y, 40, 40))


@pytest.fixture(autouse=True)
def clean_globals():
    gateway.CAMERA_ROOMS.clear()
    gateway.LAST_PIR.clear()
    yield
    gateway.CAMERA_ROOMS.clear()
    gateway.LAST_PIR.clear()


# ---- 모델 출력 ----

def test_iou():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1
    assert iou((0, 0, 10, 10), (20, 20, 5, 5)) == 0
    assert iou((0, 0, 10, 10), (5, 0, 10, 10)) == pytest.approx(50 / 150)
    assert iou((0, 0, 0, 0), (0, 0, 0, 0)) == 0


def test_nms_keeps_best_of_overlapping_same_label():
    a, b = Detection("cup", 0.9, (0, 0, 10, 10)), Detection("cup", 0.5, (1, 1, 10, 10))
    c = Detection("bottle", 0.6, (1, 1, 10, 10))  # 다른 물건은 겹쳐도 남김
    assert nms([b, a, c]) == [a, c]


def test_parse_yolo():
    out = np.zeros((1, 84, 3), dtype=np.float32)
    out[0, :4, 0] = [320, 320, 64, 128]   # 사람
    out[0, 4 + 0, 0] = 0.9
    out[0, :4, 1] = [100, 100, 20, 20]    # 컵 (cup = 41번)
    out[0, 4 + 41, 1] = 0.7
    out[0, 4 + 41, 2] = 0.1               # 점수 낮음 → 버림
    dets = parse_yolo(out, scale_x=2.0, scale_y=1.0)
    assert [d.label for d in dets] == ["person", "cup"]
    assert dets[0].box == pytest.approx((576.0, 256.0, 128.0, 128.0))


# ---- 인원 세기 ----

def test_event_shape_uses_existing_occupancy_api():
    w = RoomWatcher(1)
    event, _ = w.update([person(), person(100)], now=0, pir=1, now_iso="2026-11-06T12:00:00+09:00")
    assert event["kind"] == "OCCUPANCY" and event["roomId"] == 1 and event["deviceId"] == "camera-1"
    assert event["occupied"] is True
    assert event["sources"] == {"camera": True, "people": 2, "pir": True}
    assert event["observedAt"] == "2026-11-06T12:00:00+09:00"


def test_sends_on_change_and_every_10_seconds():
    w = RoomWatcher(1)
    sent = []
    t = 0
    for count in [0, 0, 0, 2, 2, 2, 2]:  # 가운데 값이라 2명이 3번 이상 보여야 바뀐다
        event, _ = w.update([person(i * 60) for i in range(count)], now=t)
        if event:
            sent.append((t, event["sources"]["people"]))
        t += 1
    assert sent == [(0, 0), (5, 2)]
    event, _ = w.update([person(), person(60)], now=5 + BEAT_SEC)
    assert event is not None  # 그대로여도 10초마다


def test_one_missed_frame_does_not_flip_count():
    w = RoomWatcher(1)
    for t in range(5):
        w.update([person()], now=t)
    event, _ = w.update([], now=5)  # 한 번 놓침
    assert event is None


def test_pir_alone_keeps_occupied():
    event, _ = RoomWatcher(1).update([], now=0, pir=1)
    assert event["occupied"] is True and event["sources"]["people"] == 0


# ---- 정리 확인 ----

def run_visit(w, after_dets, start=0):
    """처음 빈 방(기준) → 사람 들어옴 → 모두 나감 → 30초 뒤 확인 결과."""
    t = start
    for _ in range(3):
        w.update([thing("chair", 0, 0)], now=t)  # 깨끗한 빈 방 (의자는 원래 있음)
        t += 1
    for _ in range(5):
        w.update([person(), thing("chair", 0, 0)], now=t)
        t += 1
    check = None
    for _ in range(EMPTY_CHECK_SEC + 10):
        _, c = w.update(after_dets, now=t)
        check = check or c
        t += 1
    return check


def test_clean_room():
    check = run_visit(RoomWatcher(1), [thing("chair", 0, 0)])
    assert check["status"] == "CLEAN"
    assert check_message(check) == "정리 확인 1번 방: 깨끗함"


def test_trash_and_left_items():
    after = [thing("cup", 100, 100), thing("cup", 300, 100), thing("laptop", 500, 100)]
    check = run_visit(RoomWatcher(1), after)
    assert check["status"] == "LEFT_ITEMS"
    assert check["trash"] == {"cup": 2} and check["leftItems"] == {"laptop": 1}
    assert check_message(check) == "정리 확인 1번 방: 두고 간 물건 노트북 1 / 치울 것 컵 2"


def test_item_that_was_there_when_clean_is_ignored():
    w = RoomWatcher(1)
    t = 0
    for _ in range(3):
        w.update([thing("book", 10, 10)], now=t)  # 원래 꽂혀 있는 책
        t += 1
    for _ in range(5):
        w.update([person()], now=t)
        t += 1
    checks = [w.update([thing("book", 12, 10)], now=t + i)[1] for i in range(EMPTY_CHECK_SEC + 5)]
    assert [c["status"] for c in checks if c] == ["CLEAN"]


def test_check_only_once_per_visit_and_off_without_model():
    w = RoomWatcher(1)
    check = run_visit(w, [thing("cup")])
    assert check["status"] == "NEEDS_CLEANING"
    _, again = w.update([thing("cup")], now=10_000)
    assert again is None  # 다시 사람이 왔다 가야 확인한다
    assert run_visit(RoomWatcher(1, check_objects=False), [thing("cup")]) is None


# ---- 카메라 루프 ----

class FakeCam:
    def __init__(self, n):
        self.n = n

    def read(self):
        if self.n == 0:
            raise StopIteration
        self.n -= 1
        return (self.n % 2 == 0), object()  # 한 번씩 읽기 실패도 섞음


class FakeDetector:
    finds_objects = True

    def detect(self, frame):
        return [person()]


def test_run_room_camera_pushes_and_logs(tmp_path):
    pushed = []
    clock = iter(range(1000))
    with pytest.raises(StopIteration):
        run_room_camera(RoomWatcher(2), FakeCam(4), FakeDetector(), pushed.append, pir_now=lambda: 0,
                        log_path=tmp_path / "log.jsonl", clock=lambda: next(clock), sleep=lambda s: None)
    assert pushed and pushed[0]["roomId"] == 2 and pushed[0]["sources"]["people"] == 1


def test_run_room_camera_writes_check_log(tmp_path, monkeypatch):
    class OneCheck(RoomWatcher):
        def update(self, dets, now, pir=None, now_iso=None):
            return None, {"roomId": 1, "status": "CLEAN", "trash": {}, "leftItems": {}, "checkedAt": "t"}

    log = tmp_path / "log.jsonl"
    with pytest.raises(StopIteration):
        run_room_camera(OneCheck(1), FakeCam(1), FakeDetector(), lambda e: None, log_path=log,
                        sleep=lambda s: None)
    assert json.loads(log.read_text())["status"] == "CLEAN"


# ---- 게이트웨이 연결 ----

def test_load_config_room_camera(monkeypatch):
    monkeypatch.setattr(gateway, "find_port", lambda: None)
    c = gateway.load_config({"ROOM_CAMERA": "1", "ROOM_CAMERA_ROOM": "2", "YOLO_MODEL": "m.onnx"})
    assert (c.room_camera, c.room_camera_room, c.yolo_model) == ("1", 2, "m.onnx")
    c = gateway.load_config({})
    assert (c.room_camera, c.room_camera_room, c.yolo_model) == (None, 1, None)


def test_uno_occupancy_is_not_sent_when_camera_owns_room(capsys):
    class Sender:
        def __init__(self):
            self.bodies = []

        def push(self, b):
            self.bodies.append(b)

    line = json.dumps({"eventId": "1-1-1", "roomId": 1, "sensorType": "occupancy", "pir": 1, "light": 500})
    sender = Sender()
    gateway.CAMERA_ROOMS.add(1)
    gateway.handle_line(line, object(), sender)
    assert sender.bodies == [] and gateway.LAST_PIR == {1: 1}
    assert "카메라 이벤트에 합쳐" in capsys.readouterr().out
    gateway.CAMERA_ROOMS.clear()
    gateway.handle_line(line, object(), sender)
    assert len(sender.bodies) == 1  # 카메라가 없으면 원래대로 보냄


def test_camera_push_targets(capsys):
    body = {"kind": "OCCUPANCY"}
    got = []

    class Sender:
        def push(self, b):
            got.append(b)

    gateway.camera_push(gateway.Config(), Sender())(body)
    assert got == [body]
    gateway.camera_push(gateway.Config(dry_run=True), None)(body)
    gateway.camera_push(gateway.Config(), None)(body)
    out = capsys.readouterr().out
    assert "[DRY_RUN] POST /api/sensor-events" in out and "SEND_SENSORS=1로 켜기" in out


def test_start_room_camera(monkeypatch):
    called = {}

    class Det:
        finds_objects = False

    monkeypatch.setattr(room_camera, "make_detector", lambda path: Det())
    monkeypatch.setattr(room_camera, "open_camera", lambda dev: "cam")

    def fake_run(watcher, cam, detector, push, pir_now):
        gateway.LAST_PIR[3] = 1
        called.update(room=watcher.room_id, cam=cam, check=watcher.check_objects, pir=pir_now())

    monkeypatch.setattr(room_camera, "run_room_camera", fake_run)
    gateway.start_room_camera(gateway.Config(room_camera="1", room_camera_room=3), None).join(timeout=1)
    assert called == {"room": 3, "cam": "cam", "check": False, "pir": 1}
    assert 3 in gateway.CAMERA_ROOMS


def test_main_starts_room_camera(monkeypatch):
    from test_gateway import FakeSerial

    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)
    monkeypatch.setattr(gateway.serial, "Serial", lambda *a, **k: FakeSerial([]))
    started = []
    monkeypatch.setattr(gateway, "start_room_camera", lambda config, sensors: started.append(config.room_camera))
    with pytest.raises(StopIteration):
        gateway.main(gateway.Config(port="/dev/fake", dry_run=True, room_camera="1"))
    assert started == ["1"]


def test_run_room_camera_keeps_going_after_error(tmp_path):
    class Flaky(FakeDetector):
        calls = 0

        def detect(self, frame):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("모델 오류")
            return super().detect(frame)

    pushed = []
    clock = iter(range(1000))
    with pytest.raises(StopIteration):
        run_room_camera(RoomWatcher(2), FakeCam(6), Flaky(), pushed.append, pir_now=lambda: 0,
                        log_path=tmp_path / "log.jsonl", clock=lambda: next(clock), sleep=lambda s: None)
    assert Flaky.calls >= 2 and pushed  # 첫 장 오류 뒤에도 계속 셈


def test_yolo_detector_uses_onnxruntime(monkeypatch):
    """젯슨 OpenCV 4.6은 YOLOv8을 못 읽어서, onnxruntime이 있으면 그걸로 돌린다."""
    import sys
    import types

    import numpy as np

    out = np.zeros((1, 84, 1), dtype=np.float32)
    out[0, :4, 0] = [320, 320, 100, 200]  # 가운데 사람 하나
    out[0, 4, 0] = 0.9

    class Session:
        def __init__(self, path, providers):
            self.providers = providers

        def get_inputs(self):
            return [types.SimpleNamespace(name="images")]

        def run(self, names, feed):
            assert "images" in feed
            return [out]

    ort = types.SimpleNamespace(get_available_providers=lambda: ["CPUExecutionProvider"], InferenceSession=Session)
    cv2 = types.SimpleNamespace(dnn=types.SimpleNamespace(blobFromImage=lambda *a, **k: "blob"), __version__="4.6.0")
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    from room_camera import YoloDetector

    det = YoloDetector("m.onnx")
    found = det.detect(np.zeros((640, 640, 3)))
    assert [d.label for d in found] == ["person"]
