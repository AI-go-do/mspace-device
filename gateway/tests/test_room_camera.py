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
    gateway.CAMERA_ROOMS[1] = gateway.time.monotonic()
    gateway.handle_line(line, object(), sender)
    assert sender.bodies == [] and gateway.fresh_pir(1) == 1
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

    def fake_run(watcher, cam, detector, push, pir_now, fall=None, on_frame=None):
        gateway.LAST_PIR[3] = (1, gateway.time.monotonic())
        on_frame()
        called.update(room=watcher.room_id, cam=cam, check=watcher.check_objects, pir=pir_now(),
                      fall=fall.hold_sec)

    monkeypatch.setattr(room_camera, "run_room_camera", fake_run)
    gateway.start_room_camera(gateway.Config(room_camera="1", room_camera_room=3), None).join(timeout=1)
    assert called == {"room": 3, "cam": "cam", "check": False, "pir": 1, "fall": 15}
    assert gateway.camera_alive(3)


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


# ---- 이상 탐지 (쓰러짐 의심) ----

def lying(x=100, y=300):
    return Detection("person", 0.9, (x, y, 200, 80))  # 가로가 넓은 상자 = 누운 자세


def test_fall_alert_after_hold_and_clear():
    from room_camera import FallWatcher, fall_message
    f = FallWatcher(1, hold_sec=15)
    assert f.update([person()], 0) is None          # 서 있으면 아무 일 없음
    assert f.update([lying()], 1) is None           # 누움 시작
    assert f.update([lying(x=105)], 10) is None     # 거의 그 자리, 아직 15초 안 됨
    alert = f.update([lying(x=102)], 16, now_iso="t")
    assert alert == {"roomId": 1, "status": "FALL_SUSPECTED", "lyingSec": 15, "checkedAt": "t"}
    assert "쓰러짐 의심" in fall_message(alert)
    assert f.update([lying()], 20) is None          # 한 번만 알림
    assert f.update([person()], 25) is None         # 일어나도 3초는 기다린다 (한 장 놓침과 구별)
    cleared = f.update([person()], 28)              # 3초 넘게 안 누워 있으면 해제
    assert cleared["status"] == "FALL_CLEARED" and "해제" in fall_message(cleared)
    assert f.update([], 29) is None


def test_fall_moving_person_resets():
    from room_camera import FallWatcher
    f = FallWatcher(1, hold_sec=15)
    f.update([lying(x=0)], 0)
    assert f.update([lying(x=400)], 10) is None     # 다른 자리로 움직이면 다시 셈
    assert f.update([lying(x=400)], 20) is None     # 새 자리 기준 10초
    assert f.update([lying(x=400)], 26)["status"] == "FALL_SUSPECTED"


def test_run_room_camera_sends_fall_alert(tmp_path):
    from room_camera import FallWatcher

    class LyingDetector:
        finds_objects = True

        def detect(self, frame):
            return [lying()]

    pushed = []
    clock = iter(range(0, 1000, 10))
    log = tmp_path / "log.jsonl"
    with pytest.raises(StopIteration):
        run_room_camera(RoomWatcher(1), FakeCam(6), LyingDetector(), pushed.append, pir_now=lambda: None,
                        log_path=log, clock=lambda: next(clock), sleep=lambda s: None, fall=FallWatcher(1, 15))
    alerts = [e for e in pushed if e["sources"].get("alert")]
    assert alerts and alerts[0]["sources"]["alert"] == "FALL_SUSPECTED" and alerts[0]["occupied"] is True
    assert "FALL_SUSPECTED" in log.read_text(encoding="utf-8")


def test_load_config_fall_sec():
    assert gateway.load_config({"FALL_SEC": "0"}).fall_sec == 0
    assert gateway.load_config({"FALL_SEC": "x"}).fall_sec == 15


def test_main_runs_cameras_without_uno(monkeypatch, capsys):
    """우노가 없어도 카메라를 켰으면 끄지 않고 카메라만 돌린다. QR 답은 화면에 보여 준다."""
    started = []

    def fake_qr(config, verifier, on_reply):
        on_reply(b"OK\n")
        on_reply(b"NO\n")

    monkeypatch.setattr(gateway, "start_qr", fake_qr)
    monkeypatch.setattr(gateway, "start_room_camera", lambda config, sensors: started.append("room"))

    def stop(s):
        raise StopIteration

    monkeypatch.setattr(gateway.time, "sleep", stop)
    with pytest.raises(StopIteration):
        gateway.main(gateway.Config(port=None, dry_run=True, qr_camera="0", room_camera="2"))
    out = capsys.readouterr().out
    assert started == ["room"] and "우노 없음" in out and "초록(열림)" in out and "빨강(거부)" in out


def test_main_without_uno_or_camera_stops():
    with pytest.raises(SystemExit):
        gateway.main(gateway.Config(port=None, dry_run=True))


def test_fall_one_missed_frame_keeps_timer():
    """YOLO가 누운 사람을 한 장 놓쳐도 타이머가 처음부터 다시 시작하지 않는다."""
    from room_camera import FallWatcher
    f = FallWatcher(1, hold_sec=15)
    f.update([lying()], 0)
    assert f.update([], 8) is None                  # 한 장 놓침
    assert f.update([lying()], 9) is None
    assert f.update([lying()], 15)["status"] == "FALL_SUSPECTED"
    assert f.update([], 16) is None                 # 알린 뒤 한 장 놓쳐도 해제하지 않음
    assert f.update([lying()], 17) is None          # 다시 보여도 또 알리지 않음


def test_camera_alive_and_fresh_pir_expire():
    gateway.CAMERA_ROOMS[1] = 100.0
    gateway.LAST_PIR[1] = (1, 100.0)
    assert gateway.camera_alive(1, now=110) and gateway.fresh_pir(1, now=110) == 1
    assert not gateway.camera_alive(1, now=131)     # 카메라가 30초 넘게 못 읽으면 우노가 다시 보냄
    assert gateway.fresh_pir(1, now=131) is None    # 우노 PIR도 30초 넘으면 안 씀
    assert not gateway.camera_alive(2) and gateway.fresh_pir(2) is None


def test_dead_camera_lets_uno_send_again():
    got = []

    class Sender:
        def push(self, b):
            got.append(b)

    line = json.dumps({"eventId": "1-1-1", "roomId": 1, "sensorType": "occupancy", "pir": 1, "light": 500})
    gateway.CAMERA_ROOMS[1] = gateway.time.monotonic() - 60
    gateway.handle_line(line, object(), Sender())
    assert len(got) == 1


def test_run_room_camera_warns_on_read_failure_and_log_error(tmp_path, capsys):
    from room_camera import FallWatcher

    class Cam:
        frames = [(False, None), (False, None), (True, "f"), (True, "f")]

        def read(self):
            if not self.frames:
                raise StopIteration
            return self.frames.pop(0)

    class LyingDetector:
        finds_objects = True

        def detect(self, frame):
            return [lying()]

    pushed, seen = [], []
    clock = iter(range(0, 1000, 10))
    with pytest.raises(StopIteration):
        run_room_camera(RoomWatcher(1), Cam(), LyingDetector(), pushed.append,
                        log_path=tmp_path,  # 폴더라서 쓰기 실패
                        clock=lambda: next(clock), sleep=lambda s: None, fall=FallWatcher(1, 5),
                        on_frame=lambda: seen.append(1), warn_every=100)
    out = capsys.readouterr().out
    assert out.count("사진을 못 읽어요") == 1       # 매 장마다 찍지 않음
    assert seen == [1, 1]
    assert any(e["sources"].get("alert") == "FALL_SUSPECTED" for e in pushed)  # 기록 실패해도 core엔 감
    assert "기록 파일에 못 썼어요" in out


def test_fall_box_jitter_keeps_alert():
    """상자가 한 장만 흔들려도 알림이 풀리거나 타이머가 다시 시작하지 않는다."""
    from room_camera import FallWatcher
    f = FallWatcher(1, hold_sec=15)
    f.update([lying()], 0)
    assert f.update([lying(x=400)], 5) is None      # 한 장 흔들림
    assert f.update([lying()], 6) is None
    assert f.update([lying()], 15)["status"] == "FALL_SUSPECTED"
    assert f.update([lying(x=400)], 16) is None     # 알린 뒤 흔들려도 해제 안 함
    assert f.update([lying()], 17) is None


def test_pir_change_sends_right_away():
    w = RoomWatcher(1, check_objects=False)
    assert w.update([], 0, pir=0)[0]["occupied"] is False
    assert w.update([], 1, pir=0)[0] is None
    event = w.update([], 2, pir=1)[0]               # 사람 수는 0 그대로, PIR만 바뀜
    assert event is not None and event["occupied"] is True


def test_failed_detection_does_not_count_as_alive():
    class Broken:
        finds_objects = False

        def detect(self, frame):
            raise RuntimeError("모델 오류")

    seen = []
    with pytest.raises(StopIteration):
        run_room_camera(RoomWatcher(1), FakeCam(3), Broken(), lambda e: None,
                        clock=iter(range(100)).__next__, sleep=lambda s: None, on_frame=lambda: seen.append(1))
    assert seen == []
