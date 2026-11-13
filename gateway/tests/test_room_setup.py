import json

import pytest

import gateway
import room_setup
from room_camera import Detection


def test_decide_keeps_items_seen_in_most_frames():
    frames = [{"remote": 1}] * 9 + [{}]  # 10장 중 9장에서 리모컨 1개 (한 장은 가려짐)
    assert room_setup.decide_room_items(frames, 0.8) == ({"remote": 1}, {})


def test_decide_drops_things_seen_only_a_few_times():
    frames = [{"remote": 1, "cell phone": 1}] * 2 + [{"remote": 1}] * 8  # 휴대폰은 2장에서만 (잘못 봄)
    assert room_setup.decide_room_items(frames, 0.8) == ({"remote": 1}, {})


def test_decide_picks_the_count_seen_in_most_frames():
    frames = [{"book": 3}] * 7 + [{"book": 2}] * 3  # 책 3권인데 가끔 2권으로 셈
    assert room_setup.decide_room_items(frames, 0.8) == ({"book": 2}, {})
    assert room_setup.decide_room_items(frames, 0.7) == ({"book": 3}, {})


def test_decide_reports_trash_instead_of_making_it_a_room_item():
    frames = [{"remote": 1, "cup": 1}] * 10
    assert room_setup.decide_room_items(frames, 0.8, trash={"cup"}) == ({"remote": 1}, {"cup": 1})


def test_watch_skips_frames_with_people():
    frames = iter(["a", "b", "c"])
    dets = {"a": [Detection("remote", 0.9, (0, 0, 1, 1))],
            "b": [Detection("person", 0.9, (0, 0, 1, 1)), Detection("backpack", 0.9, (0, 0, 1, 1))],
            "c": [Detection("remote", 0.9, (0, 0, 1, 1)), Detection("chair", 0.9, (0, 0, 1, 1))]}
    clock = [0.0]

    def sleep(sec):
        clock[0] += sec

    counted, skipped = room_setup.watch(lambda f: dets[f], lambda: next(frames), minutes=20 / 60, every=10,
                                        now=lambda: clock[0], sleep=sleep, log=lambda *a: None)
    assert counted == [{"remote": 1}, {"remote": 1}] and skipped == 1  # 의자(가구)는 비품으로 안 셈


def test_profile_round_trip(tmp_path):
    path = str(tmp_path / "room_profile.json")
    room_setup.write_profile(path, {"remote": 1}, frames=30, skipped=2, minutes=5)
    assert json.load(open(path, encoding="utf-8"))["frames"] == 30
    assert room_setup.load_profile_items(path) == {"remote": 1}
    assert room_setup.load_profile_items(str(tmp_path / "none.json")) is None


def test_gateway_uses_profile_only_without_room_items(tmp_path):
    path = str(tmp_path / "room_profile.json")
    room_setup.write_profile(path, {"remote": 1, "book": 2}, frames=30, skipped=0, minutes=5)
    config = gateway.load_config({"ROOM_PROFILE": path, "PORT": "x"})
    assert gateway.room_items(config) == {"remote": 1, "book": 2}
    config = gateway.load_config({"ROOM_PROFILE": path, "ROOM_ITEMS": "remote:2", "PORT": "x"})
    assert gateway.room_items(config) == {"remote": 2}  # 직접 적은 게 먼저
    config = gateway.load_config({"ROOM_PROFILE": "off", "PORT": "x"})
    assert gateway.room_items(config) == {}


REMOTE = Detection("remote", 0.9, (0, 0, 1, 1))
PERSON = Detection("person", 0.9, (0, 0, 1, 1))
CUP = Detection("cup", 0.9, (0, 0, 1, 1))


def auto(tmp_path, done):
    return room_setup.AutoRegister(str(tmp_path / "room_profile.json"), done, minutes=1, every=10, log=lambda *a: None)


def test_auto_register_collects_empty_room_then_writes_profile(tmp_path):
    got = []
    reg = auto(tmp_path, got.append)
    assert reg.need == 6  # 1분 / 10초
    calls = []
    for i in range(30):  # 1초마다 한 장 → 10초에 한 번만 자세히 센다
        reg.feed(float(i * 2), [], lambda: calls.append(1) or [REMOTE])
    assert len(calls) == 6 and got == [{"remote": 1}] and reg.done
    assert room_setup.load_profile_items(str(tmp_path / "room_profile.json")) == {"remote": 1}
    reg.feed(100.0, [], lambda: calls.append(1) or [REMOTE])
    assert len(calls) == 6  # 끝나면 더 안 셈


def test_auto_register_pauses_while_people_are_in_the_room(tmp_path):
    got = []
    reg = auto(tmp_path, got.append)
    for i in range(3):
        reg.feed(i * 10.0, [], lambda: [REMOTE])
    for i in range(3, 20):  # 사람이 들어와 있는 동안은 세지 않음
        reg.feed(i * 10.0, [PERSON], lambda: [REMOTE])
    assert len(reg.counted) == 3 and not got
    for i in range(20, 23):  # 다시 비면 이어서
        reg.feed(i * 10.0, [], lambda: [REMOTE])
    assert got == [{"remote": 1}]


def test_auto_register_starts_over_when_trash_keeps_showing(tmp_path):
    got = []
    reg = auto(tmp_path, got.append)
    for i in range(6):
        reg.feed(i * 10.0, [], lambda: [REMOTE, CUP])
    assert not got and reg.counted == [] and not reg.done  # 파일 안 쓰고 처음부터
    assert room_setup.load_profile_items(str(tmp_path / "room_profile.json")) is None
    for i in range(6, 12):
        reg.feed(i * 10.0, [], lambda: [REMOTE])
    assert got == [{"remote": 1}]


def test_auto_register_keeps_going_when_profile_cannot_be_written(tmp_path):
    """파일을 못 쓰면 예외를 카메라 루프로 안 넘기고, 모은 장을 버린 뒤 다시 모은다 (모은 장이 끝없이 늘지 않게)."""
    got, logs = [], []
    reg = room_setup.AutoRegister(str(tmp_path / "no-dir" / "room_profile.json"), got.append, minutes=1, every=10,
                                  log=logs.append)
    for i in range(6):
        reg.feed(i * 10.0, [], lambda: [REMOTE])
    assert not got and not reg.done and reg.counted == []
    assert "못 적어서" in logs[-1]


def test_setup_command_does_nothing_with_profile_off(monkeypatch):
    import room_camera
    monkeypatch.setattr(room_camera, "open_camera", lambda *a: pytest.fail("카메라를 열면 안 됨"))
    monkeypatch.setattr(room_camera, "make_detector", lambda *a: object())
    with pytest.raises(SystemExit, match="ROOM_PROFILE=off"):
        room_setup.main({"ROOM_CAMERA": "0", "YOLO_MODEL": "gateway/yolov8s.onnx", "ROOM_PROFILE": "off", "PORT": "none"})


def test_camera_loop_feeds_the_register():
    import room_camera

    class Cam:
        def read(self):
            return True, "frame"

    class Det:
        def detect(self, frame):
            return []

        def detect_detailed(self, frame):
            return [REMOTE]

    class Stop(Exception):
        pass

    seen = []

    class Reg:
        def feed(self, now, dets, detail):
            seen.append(detail())
            raise Stop

    class Watcher:
        room_id, desk, photos, vlm = 1, None, False, None

        def update(self, *a, **k):
            return None, None

    def sleep(_):
        raise Stop

    try:
        room_camera.run_room_camera(Watcher(), Cam(), Det(), lambda e: None, register=Reg(), sleep=sleep)
    except Stop:
        pass
    assert seen == [[REMOTE]]


def test_gateway_registers_only_without_room_items_or_profile(monkeypatch, tmp_path):
    import room_camera

    class Det:
        finds_objects = True

    monkeypatch.setattr(room_camera, "make_detector", lambda *a, **k: Det())
    monkeypatch.setattr(room_camera, "open_camera", lambda dev: "cam")
    got = {}
    monkeypatch.setattr(room_camera, "run_room_camera",
                        lambda watcher, *a, register=None, **k: got.update(reg=register, watcher=watcher))
    path = str(tmp_path / "room_profile.json")

    def start(**kw):
        config = gateway.Config(room_camera="1", room_profile=path, baseline_file=None, **kw)
        gateway.start_room_camera(config, None).join(timeout=1)
        return got["reg"], got["watcher"]

    reg, watcher = start()
    assert isinstance(reg, room_setup.AutoRegister)
    reg.on_done({"remote": 1})
    assert watcher.room_items == {"remote": 1}  # 등록이 끝나면 바로 그 비품으로 판정
    assert start(room_items={"remote": 1})[0] is None  # 직접 적었으면 등록 안 함
    room_setup.write_profile(path, {"remote": 1}, frames=30, skipped=0, minutes=5)
    reg, watcher = start()
    assert reg is None and watcher.room_items == {"remote": 1}  # 파일이 있으면 그걸 씀
