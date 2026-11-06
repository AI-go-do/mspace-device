"""desk_diff.py 테스트. 그림으로 만든 가짜 책상 사진으로 확인한다 (카메라 없이)."""
import json

import cv2
import numpy as np
import pytest

import room_camera
from desk_diff import DeskCompare, find_changes, match_light, prepare
from room_camera import Detection, RoomWatcher

AREA = (0.25, 0.2, 0.75, 0.9)


def desk(*objects, light=1.0, noise=0):
    """밝은 책상(가운데)이 있는 1920×1080 화면. objects = (x, y, w, h, 색)."""
    img = np.full((1080, 1920, 3), 60, np.uint8)
    cv2.rectangle(img, (500, 230), (1420, 950), (190, 210, 225), -1)  # 책상
    for x, y, w, h, color in objects:
        cv2.rectangle(img, (x, y), (x + w, y + h), color, -1)
    if noise:
        rng = np.random.default_rng(0)
        img = np.clip(img.astype(int) + rng.integers(-noise, noise, img.shape), 0, 255).astype(np.uint8)
    return np.clip(img.astype(np.float32) * light, 0, 255).astype(np.uint8)


PHONE = (900, 400, 40, 80, (20, 20, 20))
PAPER = (1100, 600, 120, 90, (250, 250, 250))
CARD = (700, 700, 45, 30, (40, 60, 160))


def test_finds_new_things_and_ignores_noise():
    base, _ = prepare(desk(noise=6), AREA)
    now, _ = prepare(desk(PHONE, PAPER, CARD, noise=6), AREA)
    assert len(find_changes(base, now)) == 3
    assert find_changes(base, prepare(desk(noise=6), AREA)[0]) == []


def test_light_change_is_not_a_thing():
    base, _ = prepare(desk(), AREA)
    assert find_changes(base, prepare(desk(light=0.8), AREA)[0]) == []
    assert match_light(base, base).mean() == pytest.approx(base.mean(), abs=1)


def test_whole_desk_changed_means_camera_moved():
    base, _ = prepare(desk(), AREA)
    moved = np.roll(desk(), 300, axis=1)
    assert find_changes(base, prepare(moved, AREA)[0]) is None


def test_changes_come_back_in_screen_pixels():
    d = DeskCompare(AREA)
    assert d.changes(desk()) is None  # 기준 사진이 없으면 비교 안 함
    d.set_base(desk())
    (box,) = d.changes(desk(PHONE))
    x, y, w, h = box
    assert x <= 900 and y <= 400 and x + w >= 940 and y + h >= 480  # 휴대폰 자리를 감싼다
    assert w < 200 and h < 200


def test_base_photo_survives_restart(tmp_path, capsys):
    path = str(tmp_path / "desk.png")
    DeskCompare(AREA, path=path).set_base(desk(), [Detection("remote", 0.8, (800, 800, 30, 90))])
    again = DeskCompare(AREA, path=path)
    assert again.ready and again.known == [(800.0, 800.0, 30.0, 90.0)]
    assert "불러옴" in capsys.readouterr().out
    assert len(again.changes(desk(PHONE))) == 1

    assert not DeskCompare((0.1, 0.1, 0.9, 0.9), path=path).ready  # 책상 위치가 바뀌면 새로 찍는다
    (tmp_path / "desk.png.json").write_text("{broken")
    assert not DeskCompare(AREA, path=path).ready
    assert "새로 찍어요" in capsys.readouterr().out


def test_base_photo_write_failure_keeps_memory(tmp_path, capsys):
    d = DeskCompare(AREA, path=str(tmp_path / "no-dir" / "desk.png"))
    d.set_base(desk())
    assert d.ready and "못 썼어요" in capsys.readouterr().out


# ---- 정리 확인에 합치기 ----

def visit(w, after, frame, start=0):
    """깨끗한 빈 방 → 사람 → 모두 나가고 30초 뒤 확인."""
    t = start
    clean = desk()
    for _ in range(3):
        w.update([], now=t, photo=lambda: clean)
        t += 1
    for _ in range(5):
        w.update([Detection("person", 0.9, (0, 0, 50, 100))], now=t)
        t += 1
    check = None
    for _ in range(room_camera.EMPTY_CHECK_SEC + 10):
        _, c = w.update(after, now=t, photo=lambda: frame)
        check = check or c
        t += 1
    return check


def test_unknown_things_become_cleaning_and_named_ones_left_items():
    names = {}

    def labeler(frame, box):
        return names.get(int(box[0]) // 100)

    w = RoomWatcher(1, desk=DeskCompare(AREA), labeler=labeler)
    names[8] = "power bank"  # 휴대폰 자리(x≈890)에 이름이 붙었다고 치자
    check = visit(w, [], desk(PHONE, PAPER))
    assert check["leftItems"] == {"power bank": 1}
    assert check["trash"] == {"unknown": 1}
    assert check["status"] == "LEFT_ITEMS"
    assert room_camera.check_message(check) == "정리 확인 1번 방: 두고 간 물건 보조배터리 1 / 치울 것 모르는 물건 1"


def test_desk_skips_things_the_model_already_counted_and_moved_room_items():
    w = RoomWatcher(1, desk=DeskCompare(AREA), room_items={"remote": 1})
    phone = Detection("cell phone", 0.7, (900, 400, 40, 80))
    check = visit(w, [phone], desk(PHONE))
    assert check["leftItems"] == {"cell phone": 1} and check["trash"] == {}  # 두 번 세지 않음

    w = RoomWatcher(1, desk=DeskCompare(AREA))
    remote_at = (700, 700, 45, 30)
    clean = desk((*remote_at, (30, 30, 30)))
    for t in range(3):
        w.update([Detection("remote", 0.8, remote_at)], now=t, photo=lambda: clean)
    w.had_people = True
    w.empty_since = None
    moved = desk((1000, 800, 45, 30, (30, 30, 30)))
    check = None
    for t in range(10, 10 + room_camera.EMPTY_CHECK_SEC + 5):
        _, c = w.update([Detection("remote", 0.8, (1000, 800, 45, 30))], now=t, photo=lambda: moved)
        check = check or c
    assert check["status"] == "CLEAN"  # 리모컨을 옮긴 빈자리는 "모르는 물건"이 아님


def test_clean_check_refreshes_desk_photo_and_photo_is_optional():
    d = DeskCompare(AREA)
    w = RoomWatcher(1, desk=d)
    assert visit(w, [], desk(light=0.9))["status"] == "CLEAN"
    assert d.base.mean() < prepare(desk(), AREA)[0].mean()  # 깨끗하면 새 사진으로 바꿔 둠

    plain = RoomWatcher(1)  # DESK_PHOTO를 안 켜면 사진을 넘겨도 쓰지 않는다
    assert visit(plain, [], desk(PHONE, PAPER))["status"] == "CLEAN"


def test_turning_desk_photo_on_later_takes_a_photo(tmp_path):
    base = tmp_path / "b.json"
    base.write_text(json.dumps({"roomId": 1, "objects": {}}))
    d = DeskCompare(AREA)
    w = RoomWatcher(1, baseline_path=str(base), desk=d)
    _, note = w.update([], now=0, photo=lambda: desk())
    assert note["status"] == "BASELINE_SAVED" and d.ready


def test_label_region_picks_the_thing_in_the_middle():
    frame = desk(PHONE)
    seen = []

    def detect(crop):
        seen.append(crop.shape)
        h, w = crop.shape[:2]
        return [Detection("person", 0.9, (0, 0, w, h)),
                Detection("cup", 0.9, (0, 0, 10, 10)),                       # 가장자리: 다른 물건
                Detection("cell phone", 0.5, (w / 2 - 20, h / 2 - 40, 40, 80))]

    assert room_camera.label_region(detect, frame, (900, 400, 40, 80)) == "cell phone"
    assert seen[0][0] >= 160  # 작은 곳도 넉넉히 잘라서 본다
    assert room_camera.label_region(lambda c: [], frame, (900, 400, 40, 80)) is None


def test_gateway_turns_desk_photo_on_only_with_desk_area(capsys, tmp_path):
    import gateway

    class Det:
        finds_objects = True

        def detect(self, frame):
            return []

    assert gateway.desk_compare(gateway.Config(), Det()) == (None, None)
    assert gateway.desk_compare(gateway.Config(desk_photo=True), Det()) == (None, None)
    assert "DESK_AREA" in capsys.readouterr().out
    cfg = gateway.Config(desk_photo=True, desk_area=AREA, baseline_file=str(tmp_path / "b.json"))
    d, labeler = gateway.desk_compare(cfg, Det())
    assert d.path == str(tmp_path / "b_desk.png") and labeler(desk(), (900, 400, 40, 80)) is None
    env = gateway.load_config({"DESK_PHOTO": "1", "LABEL_MODEL": "w.onnx", "PORT": "x"})
    assert env.desk_photo and env.label_model == "w.onnx"


def test_label_model_names_file_and_fallback(tmp_path, capsys):
    (tmp_path / "w.names.json").write_text(json.dumps(["power bank", "paper"]))
    assert room_camera.load_names(str(tmp_path / "w.onnx")) == ["power bank", "paper"]
    assert room_camera.load_names(str(tmp_path / "none.onnx")) is room_camera.COCO_NAMES
    room_camera.make_labeler(object(), str(tmp_path / "missing.onnx"))
    assert "지금 모델로" in capsys.readouterr().out


def test_join_near_merges_pieces_of_one_thing():
    from desk_diff import join_near
    assert join_near([(0, 0, 10, 10), (15, 0, 10, 10), (100, 100, 5, 5)]) == [(0, 0, 25, 10), (100, 100, 5, 5)]
