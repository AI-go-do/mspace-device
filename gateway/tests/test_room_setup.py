import json

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
