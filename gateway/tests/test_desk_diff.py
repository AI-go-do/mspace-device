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


def side_light(img, strength=-0.4):
    """책상 한쪽에 부드러운 그늘 (블라인드·문을 여닫아 한쪽만 어두워짐)."""
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]].astype(np.float32)
    glow = 1 + strength * np.exp(-((xx - 700) ** 2 + (yy - 450) ** 2) / (2 * 120.0 ** 2))
    return np.clip(img.astype(np.float32) * glow[..., None], 0, 255).astype(np.uint8)


def test_local_light_ignores_one_sided_light_but_still_finds_things():
    base, _ = prepare(desk(), AREA)
    lit, _ = prepare(side_light(desk()), AREA)
    assert find_changes(base, lit)  # 전체 밝기만 맞추면 한쪽 그늘을 물건으로 잘못 봄
    assert find_changes(base, lit, light="local") == []
    found = find_changes(base, prepare(side_light(desk(PHONE)), AREA)[0], light="local")
    assert len(found) == 1


def test_ssim_ignores_light_and_shade_but_still_finds_things():
    base, _ = prepare(desk(noise=6), AREA)
    assert find_changes(base, prepare(desk(noise=6), AREA)[0], light="ssim") == []
    assert find_changes(base, prepare(desk(light=0.7, noise=6), AREA)[0], light="ssim") == []
    assert find_changes(base, prepare(side_light(desk(noise=6)), AREA)[0], light="ssim") == []
    assert len(find_changes(base, prepare(desk(PHONE, PAPER, CARD, noise=6), AREA)[0], light="ssim")) == 3
    found = find_changes(base, prepare(side_light(desk(PHONE, noise=6)), AREA)[0], light="ssim")
    assert len(found) == 1


def test_desk_settings_come_from_env(capsys):
    import desk_diff
    import gateway
    config = gateway.load_config({"DESK_THRESH": "50", "DESK_MIN_AREA": "0.004", "DESK_LIGHT": "local", "PORT": "x"})
    d = DeskCompare(AREA, **desk_diff.settings(config))
    assert d.tune == {"thresh": 50, "min_area": 0.004, "light": "local", "move": False, "move_same": 1.0,
                      "keep_small": 0.0}
    config = gateway.load_config({"DESK_THRESH": "999", "DESK_MIN_AREA": "abc", "PORT": "x"})
    assert (config.desk_thresh, config.desk_min_area, config.desk_light) == (35, 0.0012, "mean")
    assert "DESK_THRESH가 이상해서" in capsys.readouterr().out
    assert gateway.load_config({"DESK_LIGHT": "ssim", "PORT": "x"}).desk_light == "ssim"
    assert gateway.load_config({"DESK_LIGHT": "fancy", "PORT": "x"}).desk_light == "mean"


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
    assert again.known_labels == ["remote"]  # 껐다 켜도 이름이 남아 DESK_MOVE가 같은 물건을 알아본다
    assert "불러옴" in capsys.readouterr().out
    assert len(again.changes(desk(PHONE))) == 1

    assert not DeskCompare((0.1, 0.1, 0.9, 0.9), path=path).ready  # 책상 위치가 바뀌면 새로 찍는다
    (tmp_path / "desk.png.json").write_text("{broken")
    assert not DeskCompare(AREA, path=path).ready
    assert "새로 찍어요" in capsys.readouterr().out


def test_old_meta_without_labels_still_loads(tmp_path):
    path = str(tmp_path / "desk.png")
    DeskCompare(AREA, path=path).set_base(desk(), [Detection("remote", 0.8, (800, 800, 30, 90))])
    meta = json.loads((tmp_path / "desk.png.json").read_text())
    del meta["labels"]  # 이름을 안 적던 옛 파일
    (tmp_path / "desk.png.json").write_text(json.dumps(meta))
    again = DeskCompare(AREA, path=path)
    assert again.ready and again.known_labels == []


def test_photo_and_meta_from_different_saves_are_not_used(tmp_path, capsys):
    path = str(tmp_path / "desk.png")
    DeskCompare(AREA, path=path).set_base(desk())
    old_meta = (tmp_path / "desk.png.json").read_text()
    DeskCompare(AREA, path=path).set_base(desk(PHONE), [Detection("remote", 0.8, (900, 400, 40, 80))])
    (tmp_path / "desk.png.json").write_text(old_meta)  # 사진만 바뀌고 꺼진 경우
    assert not DeskCompare(AREA, path=path).ready
    assert "짝이 안 맞음" in capsys.readouterr().out


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
    assert room_camera.check_message(check).splitlines()[0] == "정리 확인 1번 방: 두고 간 물건 보조배터리 1 / 치울 것 모르는 물건 1"


def test_moved_chair_is_not_trash():
    w = RoomWatcher(1, desk=DeskCompare(AREA), labeler=lambda frame, box: "chair")
    check = visit(w, [], desk(PAPER))
    assert check["status"] == "CLEAN" and check["trash"] == {}  # 의자 자리 바뀜은 정리 대상이 아님


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


def test_skipped_compare_keeps_old_photo(capsys):
    d = DeskCompare(AREA)
    w = RoomWatcher(1, desk=d)
    w.update([], now=0, photo=lambda: desk())
    old = d.base.copy()
    moved = np.roll(desk(PHONE), 300, axis=1)  # 카메라가 움직임 → 비교 못 함
    check = None
    w.had_people = True
    for t in range(10, 10 + room_camera.EMPTY_CHECK_SEC + 5):
        _, c = w.update([], now=t, photo=lambda: moved)
        check = check or c
    assert check["status"] == "CLEAN" and (d.base == old).all()  # 물건이 찍힌 사진을 깨끗한 기준으로 바꾸지 않음
    assert "비교를 건너뜀" in capsys.readouterr().out


def test_wrong_names_file_is_an_error_and_labeler_falls_back(capsys):
    out = np.zeros((1, 4 + 22, 10), np.float32)
    with pytest.raises(room_camera.LabelCountMismatch):
        room_camera.parse_yolo(out, 1, 1)  # 22종 모델인데 이름표 없음(80종)

    class Bad:
        def detect(self, crop):
            raise room_camera.LabelCountMismatch("22 vs 80")

    class Good:
        def detect(self, crop):
            h, w = crop.shape[:2]
            return [Detection("cup", 0.9, (w / 2 - 5, h / 2 - 5, 10, 10))]

    good = Good()
    labeler = room_camera.make_labeler(good)
    assert labeler(desk(), (900, 400, 40, 80)) == "cup"
    import types
    orig = room_camera.YoloDetector
    room_camera.YoloDetector = lambda path: types.SimpleNamespace(detect=Bad().detect, names=[])
    try:
        labeler = room_camera.make_labeler(good, "w.onnx")
        assert labeler(desk(), (900, 400, 40, 80)) == "cup"
    finally:
        room_camera.YoloDetector = orig
    assert "지금 모델로" in capsys.readouterr().out
    with pytest.raises(room_camera.LabelCountMismatch):
        room_camera.make_labeler(Bad())(desk(), (900, 400, 40, 80))  # 기본 모델 자체가 틀리면 숨기지 않음


def test_entry_desk_photo_is_the_last_empty_room_not_the_first_clean_one(tmp_path):
    """앞 팀이 둔 종이는 입실 직전 사진에 이미 있어서 다음 팀 탓이 아니다. 다음 팀이 둔 카드만 잡힌다."""
    entry = DeskCompare(AREA, path=str(tmp_path / "entry_desk.png"))
    w = RoomWatcher(1, on_checkout=True, desk=DeskCompare(AREA), entry_desk=entry)
    who = Detection("person", 0.9, (0, 0, 50, 100))
    t = 0

    def run(dets, frame, sec):
        nonlocal t
        out = []
        for _ in range(sec):
            c = w.update(dets, now=t, photo=lambda: frame)[1]
            if c and "reservationId" in c:
                out.append(c)
            t += 1
        return out

    run([], desk(), 5)                       # 깨끗한 방
    run([who], desk(), 3)                    # 앞 팀(8) 입실
    w.request_check(8, t)
    first = run([], desk(PAPER), room_camera.SETTLE_SEC + 6)  # 종이를 두고 나감
    assert [(c["reservationId"], c["trash"]) for c in first] == [(8, {"unknown": 1})]
    run([], desk(PAPER), 5)                  # 종이가 남은 빈 방
    run([who], desk(PAPER), 3)               # 다음 팀(9) 입실 → 입실 직전 사진(종이 있음) 저장
    assert entry.ready and (tmp_path / "entry_desk.png").exists()
    w.request_check(9, t)
    got = run([], desk(PAPER, CARD), room_camera.SETTLE_SEC + 6)
    assert [(c["reservationId"], c["trash"]) for c in got] == [(9, {"unknown": 1})]  # 카드만
    assert not entry.ready and not (tmp_path / "entry_desk.png").exists()  # 확인 뒤 지움


def test_look_does_not_save_and_clear_removes_files(tmp_path):
    d = DeskCompare(AREA, path=str(tmp_path / "d.png"))
    img = d.look(desk())
    assert not d.ready and not (tmp_path / "d.png").exists()
    d.set_prepared(img, [(1, 2, 3, 4)])
    assert d.ready and d.known == [(1.0, 2.0, 3.0, 4.0)] and (tmp_path / "d.png.json").exists()
    d.clear()
    assert not d.ready and not list(tmp_path.iterdir())


def test_gateway_gives_entry_desk_only_in_checkout_mode_with_desk_photo(monkeypatch, tmp_path):
    import gateway

    class Det:
        finds_objects = True

        def detect(self, frame):
            return []

    got = []
    monkeypatch.setattr(room_camera, "make_detector", lambda path, desk=None, floor=None, grid=2: Det())
    monkeypatch.setattr(room_camera, "open_camera", lambda dev: "cam")
    monkeypatch.setattr(gateway.SensorSender, "start", lambda self: self)
    monkeypatch.setattr(gateway.CheckoutPoller, "start", lambda self: self)
    monkeypatch.setattr(room_camera, "run_room_camera", lambda watcher, *a, **k: got.append(watcher))
    cfg = gateway.Config(room_camera="1", server="https://x.example", device_token="t", desk_photo=True,
                         desk_area=AREA, baseline_file=str(tmp_path / "b.json"))
    gateway.start_room_camera(cfg, object()).join(timeout=1)  # 퇴실 모드
    gateway.start_room_camera(cfg, None).join(timeout=1)      # 연습 모드
    assert got[0].entry_desk.path == str(tmp_path / "b_entry_desk.png")
    assert got[0].entry_path == str(tmp_path / "b_entry.json")
    assert got[1].entry_desk is None
    gateway.CAMERA_ROOMS.clear()  # 다른 테스트에 카메라 방이 남지 않게


def test_entry_photo_and_counts_from_different_saves_are_not_mixed(tmp_path, capsys):
    """새 사진을 쓴 뒤 개수 파일을 쓰기 전에 꺼지면, 켤 때 옛 개수 + 새 사진을 쓰지 않고 둘 다 버린다."""
    base = str(tmp_path / "b.json")
    entry = DeskCompare(AREA, path=str(tmp_path / "b_entry_desk.png"))
    w = RoomWatcher(1, on_checkout=True, baseline_path=base, entry_desk=entry)
    who = Detection("person", 0.9, (0, 0, 50, 100))
    for t in range(5):
        w.update([], now=t, photo=lambda: desk())
    w.update([who], now=5, photo=lambda: desk())
    assert json.loads((tmp_path / "b_entry.json").read_text())["deskSum"]
    old_counts = (tmp_path / "b_entry.json").read_text()
    entry.set_prepared(entry.look(desk(PAPER)))           # 다음 기준 사진만 쓰고 꺼졌다고 치자
    (tmp_path / "b_entry.json").write_text(old_counts)

    again = DeskCompare(AREA, path=str(tmp_path / "b_entry_desk.png"))
    w2 = RoomWatcher(1, on_checkout=True, baseline_path=base, entry_desk=again)
    assert w2.entry is None and not again.ready and "같은 때" in capsys.readouterr().out

    ok = DeskCompare(AREA, path=str(tmp_path / "c_entry_desk.png"))
    w3 = RoomWatcher(1, on_checkout=True, baseline_path=str(tmp_path / "c.json"), entry_desk=ok)
    for t in range(5):
        w3.update([], now=t, photo=lambda: desk())
    w3.update([who], now=5, photo=lambda: desk())
    ok2 = DeskCompare(AREA, path=str(tmp_path / "c_entry_desk.png"))
    assert RoomWatcher(1, on_checkout=True, baseline_path=str(tmp_path / "c.json"), entry_desk=ok2).entry is not None


REMOTE = (700, 700, 45, 30, (30, 30, 30))
MOVED_REMOTE = (1000, 800, 30, 45, (30, 30, 30))      # 옮기면서 돌림
FLIPPED_REMOTE = (700, 700, 45, 30, (120, 120, 170))  # 같은 자리에서 뒤집음 (뒷면 색이 다름)


@pytest.mark.parametrize("same", [0.67, 1.0])
def test_move_drops_moved_and_flipped_room_things_but_keeps_new_ones(same):
    """원래 있던 리모컨을 옮기거나 뒤집은 것은 빼고, 새로 둔 물건은 남긴다 (DESK_MOVE=1)."""
    base, _ = prepare(desk(REMOTE, noise=6), AREA)
    moved_img, _ = prepare(desk(MOVED_REMOTE, noise=6), AREA)
    flipped, _ = prepare(desk(FLIPPED_REMOTE, noise=6), AREA)
    assert len(find_changes(base, moved_img, light="ssim")) == 2   # 끄면 빈자리·새 자리 둘 다 "달라짐"
    out = []
    assert find_changes(base, moved_img, light="ssim", move=True, moved=out, move_same=same) == []
    assert len(out) == 2
    assert len(find_changes(base, flipped, light="ssim")) == 1
    assert find_changes(base, flipped, light="ssim", move=True, move_same=same) == []
    assert len(find_changes(base, flipped, light="ssim", move=True, move_same=5.0)) == 1  # 기준을 높이면 덜 뺌
    with_paper, _ = prepare(desk(MOVED_REMOTE, PAPER, noise=6), AREA)
    assert len(find_changes(base, with_paper, light="ssim", move=True, move_same=same)) == 1  # 크기가 다른 새 물건은 그대로
    empty, _ = prepare(desk(noise=6), AREA)
    assert len(find_changes(empty, prepare(desk(PHONE, noise=6), AREA)[0], light="ssim", move=True, move_same=same)) == 1


def test_move_does_not_count_a_flipped_remote_seen_as_laptop():
    """뒤집힌 리모컨을 YOLO가 laptop으로 봐도, 기준 때 리모컨이 있던 자리·크기면 새 물건으로 안 센다."""
    import tidy_eval
    before, after = desk(REMOTE, noise=6), desk(FLIPPED_REMOTE, noise=6)
    remote = Detection("remote", 0.8, (700, 700, 45, 30))
    laptop = Detection("laptop", 0.4, (698, 699, 50, 33))

    def find(frame):
        return [remote] if frame is before else [laptop]

    check, _ = tidy_eval.judge(before, after, find, room_items={"remote": 1}, desk=DeskCompare(AREA, light="ssim", move=True))
    assert check["status"] == "CLEAN"
    check, _ = tidy_eval.judge(before, after, find, room_items={"remote": 1}, desk=DeskCompare(AREA, light="ssim"))
    assert check["leftItems"] == {"laptop": 1}  # 끄면 지금처럼 잘못 신고
    phone = Detection("cell phone", 0.7, (900, 400, 40, 80))
    check, _ = tidy_eval.judge(before, desk(FLIPPED_REMOTE, PHONE, noise=6),
                               lambda f: [remote] if f is before else [laptop, phone],
                               room_items={"remote": 1}, desk=DeskCompare(AREA, light="ssim", move=True))
    assert check["leftItems"] == {"cell phone": 1}  # 새로 둔 휴대폰은 그대로


def test_move_settings_come_from_env():
    import desk_diff
    import gateway
    config = gateway.load_config({"DESK_MOVE": "1", "DESK_MOVE_SAME": "0.67", "DESK_LIGHT": "ssim", "PORT": "x"})
    assert desk_diff.settings(config)["move"] is True and desk_diff.settings(config)["move_same"] == 0.67
    assert config.desk_light == "ssim"
    assert gateway.load_config({"PORT": "x"}).desk_move_same == 1.0  # 9판 1등


def test_entry_desk_keeps_known_labels_for_move():
    """예약 기준 책상 사진에도 기준 때 물건 이름을 같이 둔다 (뒤집힌 리모컨을 laptop으로 봐도 빼게)."""
    d = DeskCompare(AREA, light="ssim", move=True)
    d.set_prepared(d.look(desk(REMOTE)), [(700, 700, 45, 30)], ["remote"])
    assert d.known_labels == ["remote"]
    d.clear()
    assert d.known_labels == [] and d.moved == []


def test_keep_small_still_finds_things_when_much_of_the_view_changed():
    """DESK_KEEP_SMALL: 화면이 크게 달라져도(35% 넘게) 포기하지 않고 물건 크기 변화만 본다 (11/12 1차 11쌍)."""
    base, _ = prepare(desk(), AREA)
    shade = (500, 230, 600, 720, (0, 0, 0))  # 책상 왼쪽 절반 넘게 어둡게 (그늘)
    phone = (1300, 400, 40, 80, (20, 20, 20))
    now, _ = prepare(desk(shade, phone), AREA)
    assert find_changes(base, now, light="ssim") is None  # 예전처럼 비교를 건너뜀
    found = find_changes(base, now, light="ssim", keep_small=0.05)
    assert len(found) == 1  # 크게 바뀐 곳은 빼고 휴대폰 자리만
    x, y, w, h = found[0]
    assert w * h < 0.05 * base.shape[0] * base.shape[1]
    assert find_changes(base, prepare(desk(shade), AREA)[0], light="ssim", keep_small=0.05) == []  # 그늘만이면 없음


def test_keep_small_changes_nothing_when_the_view_did_not_change_much():
    base, _ = prepare(desk(noise=6), AREA)
    now, _ = prepare(desk(PHONE, PAPER, CARD, noise=6), AREA)
    assert find_changes(base, now, keep_small=0.05) == find_changes(base, now)


def test_small_change_on_a_chair_is_a_thing_with_furniture_small():
    """DESK_FURNITURE_SMALL: 의자로 이름 붙은 곳이라도 의자보다 훨씬 작으면 의자 위·옆 물건 (11/12 035 가방)."""
    chair = Detection("chair", 0.8, (1000, 500, 400, 400))
    w = RoomWatcher(1, desk=DeskCompare(AREA), labeler=lambda frame, box: "chair")
    assert visit(w, [chair], desk(PAPER))["status"] == "CLEAN"  # 기본(0)은 예전처럼 가구로 보고 뺀다
    w = RoomWatcher(1, desk=DeskCompare(AREA, furniture_small=0.5), labeler=lambda frame, box: "chair")
    check = visit(w, [chair], desk(PAPER))
    assert check["status"] == "NEEDS_CLEANING" and check["trash"] == {"unknown": 1}


def test_chair_sized_change_is_still_the_chair_with_furniture_small():
    """의자만큼 달라진 곳(의자를 옮김)은 그대로 가구로 보고 뺀다. 가구를 못 찾았을 때도 예전처럼 뺀다."""
    chair = Detection("chair", 0.8, (1090, 590, 140, 110))
    w = RoomWatcher(1, desk=DeskCompare(AREA, furniture_small=0.5), labeler=lambda frame, box: "chair")
    assert visit(w, [chair], desk(PAPER))["status"] == "CLEAN"
    w = RoomWatcher(1, desk=DeskCompare(AREA, furniture_small=0.5), labeler=lambda frame, box: "chair")
    assert visit(w, [], desk(PAPER))["status"] == "CLEAN"


def test_keep_small_and_furniture_small_settings():
    import desk_diff
    import gateway
    config = gateway.load_config({"DESK_KEEP_SMALL": "0.05", "DESK_FURNITURE_SMALL": "0.5", "PORT": "x"})
    assert config.desk_keep_small == 0.05 and config.desk_furniture_small == 0.5
    d = DeskCompare(AREA, **desk_diff.settings(config))
    assert d.tune["keep_small"] == 0.05 and d.furniture_small == 0.5
    off = gateway.load_config({"PORT": "x"})
    assert off.desk_keep_small == 0.0 and off.desk_furniture_small == 0.0  # 기본은 예전과 같음
