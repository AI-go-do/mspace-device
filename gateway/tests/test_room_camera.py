"""room_camera.py 테스트. 카메라·OpenCV·모델 없이 찾은 목록을 직접 넣는다."""
import json
import os

import numpy as np
import pytest

import gateway
import room_camera
from room_camera import (BEAT_SEC, EMPTY_CHECK_SEC, ENTRY_EMPTY_SEC, PENDING_MAX_SEC, SETTLE_SEC, Detection, RoomWatcher, check_message, iou, nms,
                         parse_yolo, run_room_camera, tile_boxes, detect_tiled, cut_by_tile, tidy_body,
                         desk_box, detect_desk, merge)


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


def test_nms_keeps_two_people_sitting_close():
    """겹침 0.54: 물건이면 하나로 합치지만, 사람은 붙어 앉은 두 명으로 남긴다."""
    p1, p2 = Detection("person", 0.9, (0, 0, 10, 10)), Detection("person", 0.8, (3, 0, 10, 10))
    assert nms([p1, p2]) == [p1, p2]
    c1, c2 = Detection("cup", 0.9, (0, 0, 10, 10)), Detection("cup", 0.8, (3, 0, 10, 10))
    assert nms([c1, c2]) == [c1]
    same = Detection("person", 0.7, (0.5, 0, 10, 10))  # 거의 같은 상자는 여전히 한 명
    assert nms([p1, same]) == [p1]


def test_nms_one_thing_with_two_names_keeps_higher_score():
    """11/7 방 사진 4장 모두: 책상 리모컨을 "리모컨"과 "휴대폰"으로 거의 같은 상자에 동시에 봄."""
    remote = Detection("remote", 0.65, (621, 461, 59, 55))
    phone = Detection("cell phone", 0.33, (620, 461, 59, 56))
    assert nms([phone, remote]) == [remote]


def test_nms_keeps_person_and_phone_in_hand():
    """사람과 손에 든 휴대폰은 상자가 많이 겹쳐도 따로 남긴다."""
    p = Detection("person", 0.9, (0, 0, 10, 10))
    phone = Detection("cell phone", 0.5, (0, 0, 10, 9))
    assert nms([p, phone]) == [p, phone]


def test_remote_also_seen_as_phone_is_not_left_phone():
    """방 비품 리모컨에 "휴대폰" 이름이 같이 붙어도 "휴대폰 두고 감"으로 알리지 않는다."""
    remote = Detection("remote", 0.65, (621, 461, 59, 55))
    phone = Detection("cell phone", 0.33, (620, 461, 59, 56))
    w = RoomWatcher(1, room_items={"remote": 1})
    w.update([remote], now=0)
    for t in range(1, 6):
        w.update([person()], now=t)
    checks = [c for c in (w.update(nms([remote, phone]), now=6 + i)[1] for i in range(EMPTY_CHECK_SEC + 5)) if c]
    assert [c["status"] for c in checks] == ["CLEAN"]


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


def test_parse_yolo_lower_bar_for_objects_than_people():
    out = np.zeros((1, 84, 3), dtype=np.float32)
    out[0, :4, :] = 100
    out[0, 4 + 0, 0] = 0.35    # 사람 0.35 → 안 셈 (사람은 0.4부터)
    out[0, 4 + 67, 1] = 0.35   # 휴대폰(67번) 0.35 → 잡음 (물건은 0.3부터)
    out[0, 4 + 67, 2] = 0.25   # 0.25 → 버림
    out[0, :4, 2] = [500, 500, 10, 10]
    assert [d.label for d in parse_yolo(out, 1, 1)] == ["cell phone"]


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


def test_no_check_while_pir_still_feels_someone():
    """카메라엔 0명이어도 PIR이 움직임을 느끼는 동안은 정리 확인을 미룬다 (사각지대에 사람이 있을 수 있음)."""
    w = RoomWatcher(1)
    t = 0
    for _ in range(3):
        w.update([], now=t)
        t += 1
    for _ in range(5):
        w.update([person()], now=t)
        t += 1
    for _ in range(EMPTY_CHECK_SEC + 30):
        assert w.update([thing("cup")], now=t, pir=1)[1] is None
        t += 1
    checks = [w.update([thing("cup")], now=t + i, pir=0)[1] for i in range(EMPTY_CHECK_SEC + 5)]
    assert [c["status"] for c in checks if c] == ["NEEDS_CLEANING"]  # PIR이 꺼진 뒤 30초에 한 번


def test_baseline_is_announced_when_saved():
    w = RoomWatcher(1)
    _, note = w.update([thing("book", 10, 10)], now=0)
    assert note["status"] == "BASELINE_SAVED" and note["objects"] == {"book": 1}
    assert check_message(note) == "깨끗한 방 기준 저장 1번 방: 물건 1개 (책 1)"
    assert w.update([thing("book", 10, 10)], now=1)[1] is None  # 한 번만 알림


def test_no_baseline_saves_one_instead_of_flagging_everything():
    """켤 때부터 사람이 있어 기준이 없으면, 퇴실 때 결과를 내지 않고 그때 모습을 기준으로 삼는다."""
    w = RoomWatcher(1)
    t = 0
    for _ in range(5):
        w.update([person()], now=t)
        t += 1
    notes = [w.update([thing("laptop")], now=t + i)[1] for i in range(EMPTY_CHECK_SEC + 5)]
    notes = [n for n in notes if n]
    assert [n["status"] for n in notes] == ["BASELINE_SAVED"]
    assert "기준이 없어" in check_message(notes[0])
    assert run_visit(w, [thing("laptop"), thing("cup", 400, 400)], start=1000)["trash"] == {"cup": 1}


def test_detail_is_used_only_for_baseline_and_check():
    calls = []

    def detail():
        calls.append(1)
        return [thing("cell phone", 900, 50)]  # 4조각으로 보니 멀리 있던 폰이 보임

    w = RoomWatcher(1)
    _, note = w.update([], now=0, detail=detail)
    assert note["objects"] == {"cell phone": 1} and len(calls) == 1
    t = 1
    for _ in range(5):
        w.update([person()], now=t, detail=detail)
        t += 1
    checks = [w.update([], now=t + i, detail=detail)[1] for i in range(EMPTY_CHECK_SEC + 5)]
    assert [c["status"] for c in checks if c] == ["CLEAN"]  # 기준과 같은 눈으로 봐서 원래 폰은 새 물건 아님
    assert len(calls) == 2  # 평소에는 부르지 않음


def test_tile_boxes_cover_screen_with_overlap():
    tiles = tile_boxes(1920, 1080)
    assert tiles[0] == (0, 0, 1056, 594) and tiles[3] == (864, 486, 1056, 594)
    assert tiles[0][2] > tiles[1][0]  # 왼쪽 조각 끝(1056)이 오른쪽 조각 시작(864)보다 뒤: 가운데가 겹침


def test_detect_tiled_moves_boxes_back_and_merges():
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    sizes = []

    def detect(img):
        sizes.append(img.shape[:2])
        if img.shape[:2] == (1080, 1920):
            return [person(0)]
        return [thing("cell phone", 10, 10)]  # 조각 안 (10, 10)

    dets = detect_tiled(detect, frame)
    assert sizes[0] == (1080, 1920) and sizes[1:] == [(594, 1056)] * 4
    phones = sorted(d.box[:2] for d in dets if d.label == "cell phone")
    assert phones == [(10, 10), (10, 496), (874, 10), (874, 496)]  # 조각 위치만큼 옮겨짐
    assert [d.label for d in dets].count("person") == 1


def test_detect_tiled_skips_cut_and_already_found_objects():
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    calls = []

    def detect(img):
        calls.append(1)
        if len(calls) == 1:
            return [thing("laptop", 100, 100)]  # 한 장 전체에서 찾은 노트북
        if len(calls) == 2:  # 왼쪽 위 조각만 뭔가 찾았다고 치자
            return [Detection("laptop", 0.8, (1010, 300, 46, 40)),  # 조각 오른쪽 경계에 잘린 반쪽
                    Detection("laptop", 0.7, (105, 105, 20, 20)),   # 이미 찾은 노트북 안쪽
                    Detection("cup", 0.6, (500, 300, 20, 20))]      # 새로 보인 컵
        return []

    assert sorted(d.label for d in detect_tiled(detect, frame)) == ["cup", "laptop"]


def test_cut_by_tile_ignores_real_screen_edges():
    assert cut_by_tile((1040, 10, 16, 16), 0, 0, 1056, 594, 1920, 1080)        # 조각 오른쪽 경계 = 잘림
    assert not cut_by_tile((1040, 10, 16, 16), 864, 0, 1056, 594, 1920, 1080)  # 화면 끝이면 안 잘림
    assert not cut_by_tile((0, 0, 16, 16), 0, 0, 1056, 594, 1920, 1080)


def test_moved_item_is_not_new():
    """리모컨을 다른 자리로 옮겨도 개수가 같으면 원래 있던 것."""
    w = RoomWatcher(1)
    w.update([thing("remote", 10, 10)], now=0)  # 기준: 리모컨 1
    for t in range(1, 6):
        w.update([person()], now=t)
    checks = [w.update([thing("remote", 900, 600)], now=6 + i)[1] for i in range(EMPTY_CHECK_SEC + 5)]
    assert [c["status"] for c in checks if c] == ["CLEAN"]


def test_room_items_zero_or_up_to_n_is_fine():
    """방 비품 리모컨 1개: 바구니에 넣어 안 보여도(0), 책상에 나와 있어도(1) 괜찮고, 2개면 1개만 잡는다."""
    for seen, expected in [([], "CLEAN"), ([thing("remote")], "CLEAN"),
                           ([thing("remote"), thing("remote", 600, 600)], "LEFT_ITEMS")]:
        w = RoomWatcher(1, room_items={"remote": 1})
        w.update([], now=0)  # 기준: 리모컨이 바구니 안이라 안 보임
        for t in range(1, 6):
            w.update([person()], now=t)
        checks = [c for c in (w.update(seen, now=6 + i)[1] for i in range(EMPTY_CHECK_SEC + 5)) if c]
        assert [c["status"] for c in checks] == [expected]
        if expected == "LEFT_ITEMS":
            assert checks[0]["leftItems"] == {"remote": 1}


def test_parse_room_items():
    assert gateway.parse_room_items("remote:1, book:3") == {"remote": 1, "book": 3}
    assert gateway.parse_room_items("remote, :2, cup:x, ") == {}
    assert gateway.load_config({"ROOM_ITEMS": "remote:1"}).room_items == {"remote": 1}
    assert gateway.load_config({}).room_items == {}


def visit_then_checkout(w, after, checkout_at=None, t0=0):
    """기준(빈 방 4초) → 사람 5초 → 나감. checkout_at초에 서버가 퇴실을 알려 줌. 결과 목록을 돌려준다."""
    out = []
    for i in range(ENTRY_EMPTY_SEC + 1):
        w.update([], now=t0 + i)  # 깨끗한 방 기준 + 입실 직전 빈 방 후보
    t = t0 + ENTRY_EMPTY_SEC + 1
    for _ in range(5):
        w.update([person()], now=t)
        t += 1
    for i in range(200):
        if checkout_at is not None and i == checkout_at:
            w.request_check(77, t)
        c = w.update(after, now=t)[1]
        if c:
            out.append(c)
        t += 1
    return out


def test_checkout_mode_waits_for_server_not_30_seconds():
    """퇴실 모드: 다 같이 나가도(화장실) 서버가 퇴실이라 하기 전엔 확인하지 않는다."""
    w = RoomWatcher(1, on_checkout=True)
    assert visit_then_checkout(w, [thing("cup")]) == []  # 200초 동안 비어 있어도 확인하지 않음


def test_checkout_mode_checks_after_settle_and_tags_reservation():
    w = RoomWatcher(1, on_checkout=True)
    checks = visit_then_checkout(w, [thing("cup"), thing("cell phone", 600, 600)], checkout_at=50)
    result = checks
    assert len(result) == 1 and result[0]["reservationId"] == 77
    assert result[0]["trash"] == {"cup": 1} and result[0]["leftItems"] == {"cell phone": 1}


def test_checkout_mode_waits_until_room_is_empty():
    """퇴실 알림이 와도 사람(또는 PIR)이 있으면 비고 10초 뒤에 확인한다."""
    w = RoomWatcher(1, on_checkout=True)
    for t in range(4):
        w.update([], now=t)
    assert w.update([person()], now=4)[1] is None  # 입실: 직전 빈 방이 예약 기준
    w.request_check(5, 5)
    assert w.update([person()], now=5)[1] is None
    assert all(w.update([], now=6 + i, pir=1)[1] is None for i in range(20))  # PIR 있음
    got = [w.update([], now=30 + i)[1] for i in range(15)]
    assert [g["reservationId"] for g in got if g] == [5]


def test_checkout_pending_expires_after_10_minutes(capsys):
    w = RoomWatcher(1, on_checkout=True)
    w.update([], now=0)
    w.request_check(5, 1)
    for t in range(1, 700, 10):
        w.update([person()], now=t)  # 다음 예약 사람이 계속 있음
    assert w.pending is None and "건너뜀" in capsys.readouterr().out


# ---- 예약 기준 (Q-22 B안: 사람이 처음 보이기 직전 빈 방과 비교) ----

class Room:
    """퇴실 모드 RoomWatcher를 1초씩 돌리는 도우미. 결과(check)만 모은다."""

    def __init__(self, w, t=0):
        self.w, self.t, self.out = w, t, []

    def run(self, dets, sec=1, pir=None):
        for _ in range(sec):
            c = self.w.update(dets, now=self.t, pir=pir)[1]
            if c and c["status"] != "BASELINE_SAVED":
                self.out.append(c)
            self.t += 1
        return self

    def checkout(self, rid):
        self.w.request_check(rid, self.t)
        return self


def test_next_team_is_not_blamed_for_what_the_last_team_left():
    """앞 팀이 컵을 두고 감 → 앞 팀 결과에 컵. 다음 팀은 입실 직전 빈 방(컵 있음)이 기준이라 깨끗."""
    r = Room(RoomWatcher(1, on_checkout=True))
    r.run([], 5).run([person()], 5).checkout(1).run([thing("cup")], SETTLE_SEC + 6)
    assert [(c["reservationId"], c["trash"]) for c in r.out] == [(1, {"cup": 1})]
    r.run([thing("cup")], 30).run([person(), thing("cup")], 5).checkout(2).run([thing("cup")], SETTLE_SEC + 6)
    assert [(c["reservationId"], c["status"]) for c in r.out[1:]] == [(2, "CLEAN")]


def test_break_in_the_middle_keeps_the_first_baseline():
    """화장실에 다 같이 다녀와도 기준은 처음 입실 직전 빈 방 그대로 (그 사이 둔 컵도 잡힘)."""
    r = Room(RoomWatcher(1, on_checkout=True))
    r.run([], 5).run([person()], 3).run([thing("cup")], 20).run([person(), thing("cup")], 3)
    r.checkout(7).run([thing("cup")], SETTLE_SEC + 6)
    assert [(c["reservationId"], c["trash"]) for c in r.out] == [(7, {"cup": 1})]


def test_no_empty_moment_means_no_result(capsys):
    """방이 3초도 안 비고 사람이 보이면 기준이 없다. 결과는 SKIPPED라 서버로 안 간다 (어질러짐 신고가 받음)."""
    r = Room(RoomWatcher(1, on_checkout=True))
    r.run([], 2).run([person()], 5).checkout(3).run([thing("cup")], SETTLE_SEC + 6)
    assert [c["status"] for c in r.out] == ["SKIPPED"] and "reservationId" not in r.out[0]
    assert "기준이 없어" in check_message(r.out[0])


def test_next_team_walks_in_before_the_check_skips_the_last_team(capsys):
    """퇴실 알림 뒤 방이 3초는 비었지만 10초 전에 다음 팀이 들어오면: 앞 예약은 건너뛰고, 그 빈 방이 다음 기준."""
    r = Room(RoomWatcher(1, on_checkout=True))
    r.run([], 5).run([person()], 5).checkout(1).run([thing("cup")], 5)
    r.run([person(), thing("cup")], 5)
    assert r.out == [] and r.w.pending is None and "예약 1 확인 전에" in capsys.readouterr().out
    r.checkout(2).run([thing("cup")], SETTLE_SEC + 6)
    assert [(c["reservationId"], c["status"]) for c in r.out] == [(2, "CLEAN")]


def test_expired_check_drops_that_teams_baseline():
    """퇴실 뒤 10분 동안 방이 안 비면(연달아 예약) 앞 팀 기준을 버린다. 다음 팀은 기준 없음 → SKIPPED."""
    r = Room(RoomWatcher(1, on_checkout=True))
    r.run([], 5).run([person()], 5).checkout(1).run([person()], PENDING_MAX_SEC + 5)
    assert r.w.entry is None
    r.checkout(2).run([thing("cup")], SETTLE_SEC + 6)
    assert [c["status"] for c in r.out] == ["SKIPPED"]


def test_entry_baseline_survives_restart_and_is_deleted_after_check(tmp_path):
    base = str(tmp_path / "room_baseline.json")
    r = Room(RoomWatcher(1, on_checkout=True, baseline_path=base))
    r.run([], 5).run([person()], 3)
    entry = tmp_path / "room_baseline_entry.json"
    assert json.loads(entry.read_text())["objects"] == {}
    r2 = Room(RoomWatcher(1, on_checkout=True, baseline_path=base), t=100)  # 젯슨을 껐다 켬
    r2.run([person()], 3).checkout(4).run([thing("cup")], SETTLE_SEC + 6)
    assert [(c["reservationId"], c["trash"]) for c in r2.out] == [(4, {"cup": 1})]
    assert not entry.exists()  # 보냈으면 지움


@pytest.mark.parametrize("text", ["{broken", "null", "[]", '"x"', "1", '{"roomId": 2, "objects": {}}',
                                  '{"roomId": 1, "objects": {"cup": Infinity}}'])
def test_broken_entry_file_is_ignored(tmp_path, capsys, text):
    """깨졌거나 모양이 다른 파일이면 켤 때 멈추지 않고 버린다."""
    base = tmp_path / "room_baseline.json"
    (tmp_path / "room_baseline_entry.json").write_text(text)
    w = RoomWatcher(1, on_checkout=True, baseline_path=str(base))
    assert w.entry is None and "못 읽어서" in capsys.readouterr().out
    assert not (tmp_path / "room_baseline_entry.json").exists()


def test_practice_mode_does_not_keep_entry_baseline():
    w = RoomWatcher(1)
    for t in range(5):
        w.update([], now=t)
    w.update([person()], now=5)
    assert w.entry is None and w.snapshot is None


def test_tidy_body_matches_server_format():
    check = {"roomId": 1, "status": "LEFT_ITEMS", "trash": {"cup": 2}, "reservationId": 9,
             "leftItems": {"laptop": 1, "cell phone": 1, "backpack": 1}, "checkedAt": "2026-11-06T17:00:00+09:00"}
    body = tidy_body(check, "camera-1", event_id="e1")
    assert body == {"eventId": "e1", "reservationId": 9, "deviceId": "camera-1", "result": "NEEDS_CLEANING",
                    "items": ["backpack", "cell phone", "cup", "cup", "laptop"],
                    "leftItems": ["BAG", "LAPTOP", "PHONE"], "observedAt": "2026-11-06T17:00:00+09:00"}
    clean = tidy_body({**check, "trash": {}, "leftItems": {"book": 1}}, "camera-1")
    assert clean["result"] == "CLEAN" and clean["leftItems"] == [] and clean["items"] == ["book"]


def test_run_room_camera_sends_only_reservation_checks(monkeypatch):
    sent = []

    class W(RoomWatcher):
        n = 0

        def update(self, dets, now, pir=None, now_iso=None, detail=None, photo=None):
            W.n += 1
            if W.n == 1:
                return None, {"roomId": 1, "status": "BASELINE_SAVED", "objects": {}, "checkedAt": "t"}
            return None, {"roomId": 1, "status": "CLEAN", "trash": {}, "leftItems": {}, "checkedAt": "t",
                          "reservationId": 3}

    with pytest.raises(StopIteration):
        run_room_camera(W(1), FakeCam(4), FakeDetector(), lambda e: None, log_path=os.devnull,
                        sleep=lambda s: None, send_check=sent.append)
    assert [b["reservationId"] for b in sent] == [3] and sent[0]["result"] == "CLEAN"


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
        def update(self, dets, now, pir=None, now_iso=None, detail=None, photo=None):
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

    monkeypatch.setattr(room_camera, "make_detector", lambda path, desk=None: Det())
    monkeypatch.setattr(room_camera, "open_camera", lambda dev: "cam")

    def fake_run(watcher, cam, detector, push, pir_now, on_frame=None, preview=None, send_check=None):
        gateway.LAST_PIR[3] = (1, gateway.time.monotonic())
        on_frame()
        called.update(room=watcher.room_id, cam=cam, check=watcher.check_objects, pir=pir_now(),
                      checkout=watcher.on_checkout, send=send_check)

    monkeypatch.setattr(room_camera, "run_room_camera", fake_run)
    gateway.start_room_camera(gateway.Config(room_camera="1", room_camera_room=3), None).join(timeout=1)
    assert called == {"room": 3, "cam": "cam", "check": False, "pir": 1, "checkout": False, "send": None}
    assert gateway.camera_alive(3)


def test_start_room_camera_uses_checkout_mode_when_sending(monkeypatch):
    """서버로 보내는 중(sensors 있음)이면 퇴실 모드 + 예약 상태 묻기 + 결과 전송을 켠다."""
    called, polled = {}, []

    class Det:
        finds_objects = True

    monkeypatch.setattr(room_camera, "make_detector", lambda path, desk=None: Det())
    monkeypatch.setattr(room_camera, "open_camera", lambda dev: "cam")
    monkeypatch.setattr(gateway.SensorSender, "start", lambda self: self)
    monkeypatch.setattr(gateway.CheckoutPoller, "start", lambda self: polled.append(self.url) or self)

    def fake_run(watcher, cam, detector, push, pir_now, on_frame=None, preview=None, send_check=None):
        called.update(checkout=watcher.on_checkout, send=send_check is not None)

    monkeypatch.setattr(room_camera, "run_room_camera", fake_run)
    cfg = gateway.Config(room_camera="1", room_camera_room=2, server="https://x.example", device_token="t")
    gateway.start_room_camera(cfg, object()).join(timeout=1)
    assert called == {"checkout": True, "send": True}
    assert polled == ["https://x.example/api/rooms/2/current"]


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
    class Cam:
        frames = [(False, None), (False, None)] + [(True, "f")] * 7

        def read(self):
            if not self.frames:
                raise StopIteration
            return self.frames.pop(0)

    class Det:
        finds_objects = True
        calls = 0

        def detect(self, frame):
            Det.calls += 1
            return [Detection("person", 0.9, (0, 0, 50, 100))] if Det.calls == 1 else []

    seen = []
    clock = iter(range(0, 1000, 20))
    with pytest.raises(StopIteration):
        run_room_camera(RoomWatcher(1), Cam(), Det(), lambda e: None,
                        log_path=tmp_path,  # 폴더라서 쓰기 실패
                        clock=lambda: next(clock), sleep=lambda s: None,
                        on_frame=lambda: seen.append(1), warn_every=100)
    out = capsys.readouterr().out
    assert out.count("사진을 못 읽어요") == 1       # 매 장마다 찍지 않음
    assert len(seen) == 7
    assert "깨끗한 방 기준 저장 1번 방" in out and "기록 파일에 못 썼어요" in out  # 기록 실패해도 카메라는 계속


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


def test_run_room_camera_feeds_preview():
    got = []

    class P:
        def update(self, frame, dets, people, area=None):
            got.append((people, area))

    with pytest.raises(StopIteration):
        run_room_camera(RoomWatcher(1), FakeCam(2), FakeDetector(), lambda e: None,
                        clock=iter(range(100)).__next__, sleep=lambda s: None, preview=P())
    assert got  # 읽은 장마다 화면 보기에 넘김


def test_preview_error_does_not_stop_counting():
    class Broken:
        def update(self, frame, dets, people, area=None):
            raise RuntimeError("인코딩 실패")

    pushed, seen = [], []
    with pytest.raises(StopIteration):
        run_room_camera(RoomWatcher(1), FakeCam(2), FakeDetector(), pushed.append,
                        clock=iter(range(100)).__next__, sleep=lambda s: None, preview=Broken(),
                        on_frame=lambda: seen.append(1))
    assert pushed and seen  # 화면 보기가 망가져도 인원 이벤트와 살아 있음 표시는 계속


# ---- 퇴실 알기 (서버 예약 상태) ----

class FakeResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self.body = body

    def json(self):
        return self.body


def test_checkout_poller_fires_only_when_last_ended_changes(capsys):
    answers = [FakeResp(200, {"lastEnded": {"reservationId": 4}}),   # 켜기 전에 끝난 예약: 기억만
               FakeResp(200, {"lastEnded": {"reservationId": 4}}),
               FakeResp(200, {"current": {"reservationId": 5}, "lastEnded": {"reservationId": 4}}),
               FakeResp(200, {"lastEnded": {"reservationId": 5}}),   # 5번 퇴실!
               FakeResp(200, {"lastEnded": None})]
    fired, seen = [], []

    def get(url, **kw):
        seen.append((url, kw["headers"]["Authorization"], kw["allow_redirects"]))
        return answers.pop(0)

    p = gateway.CheckoutPoller("https://x", "tok", 2, fired.append, get=get)
    for _ in range(5):
        p.poll_once()
    assert fired == [5]
    assert seen[0] == ("https://x/api/rooms/2/current", "Bearer tok", False)


def test_checkout_poller_survives_errors_and_warns_once(capsys):
    def boom(url, **kw):
        raise gateway.requests.ConnectionError("끊김")

    answers = iter([FakeResp(401), FakeResp(200, {"lastEnded": {"reservationId": 1}}),
                    FakeResp(200, {"lastEnded": {"reservationId": 2}})])
    fired = []
    p = gateway.CheckoutPoller("https://x", "t", 1, fired.append, get=boom)
    p.poll_once()
    p.poll_once()
    p.get = lambda url, **kw: next(answers)
    p.poll_once()  # 401
    p.poll_once()  # 처음 성공: 기억만
    p.poll_once()
    out = capsys.readouterr().out
    assert out.count("못 물어요") == 1 and "다시 됨" in out
    assert fired == [2]


def test_tidy_sender_drops_404_and_409():
    s = gateway.SensorSender("https://x", "t", path="/api/rooms/1/tidy-checks", drop=gateway.TIDY_DROP)
    assert s.url == "https://x/api/rooms/1/tidy-checks"
    assert 404 in s.drop and 409 in s.drop and 404 not in gateway.SENSOR_DROP


def test_baseline_survives_restart(tmp_path, capsys):
    path = str(tmp_path / "base.json")
    w = RoomWatcher(1, baseline_path=path)
    w.update([thing("remote")], now=0, now_iso="2026-11-06T17:00:00+09:00")
    with open(path) as f:
        assert json.load(f) == {"roomId": 1, "objects": {"remote": 1}, "savedAt": "2026-11-06T17:00:00+09:00"}

    again = RoomWatcher(1, baseline_path=path)  # 껐다 켬
    assert again.baseline == {"remote": 1}
    assert "저장된 깨끗한 방 기준 불러옴 1번 방: 물건 1개 (리모컨 1)" in capsys.readouterr().out
    _, check = again.update([thing("remote")], now=0)
    assert check is None  # 빈 방으로 켜도 기준을 다시 찍지 않는다
    assert run_visit(again, [thing("remote"), thing("cup", 400, 400)], start=100)["trash"] == {"cup": 1}


def test_clean_check_widens_baseline_file(tmp_path):
    path = str(tmp_path / "base.json")
    w = RoomWatcher(1, baseline_path=path, room_items={"remote": 1})
    w.update([], now=0)  # 리모컨이 바구니에 있을 때 찍은 기준
    assert run_visit(w, [thing("remote")], start=100)["status"] == "CLEAN"
    with open(path) as f:
        assert json.load(f)["objects"] == {"remote": 1}


@pytest.mark.parametrize("content", ["{broken", '{"roomId": 2, "objects": {"cup": 1}}', '{"roomId": 1}',
                                     '{"roomId": 1, "objects": ["cup"]}'])
def test_bad_baseline_file_takes_new_one(tmp_path, capsys, content):
    path = tmp_path / "base.json"
    path.write_text(content)
    w = RoomWatcher(1, baseline_path=str(path))
    assert w.baseline is None and "새로 찍어요" in capsys.readouterr().out
    w.update([thing("remote")], now=0)
    assert json.loads(path.read_text())["objects"] == {"remote": 1}


def test_baseline_write_failure_keeps_running(tmp_path, capsys):
    w = RoomWatcher(1, baseline_path=str(tmp_path / "no-dir" / "base.json"))
    _, note = w.update([thing("remote")], now=0)
    assert note["status"] == "BASELINE_SAVED" and w.baseline == {"remote": 1}
    assert "기준 파일을 못 썼어요" in capsys.readouterr().out


def test_no_baseline_path_keeps_memory_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    RoomWatcher(1).update([thing("remote")], now=0)
    assert list(tmp_path.iterdir()) == []


# ---- 책상 확대 (DESK_AREA) ----

def test_desk_box_turns_ratio_into_pixels():
    assert desk_box(1920, 1080, (0.25, 0.2, 0.75, 0.8)) == (480, 216, 960, 648)


def test_detect_desk_moves_boxes_back_to_screen():
    frame = np.zeros((1000, 2000, 3), dtype=np.uint8)
    shapes = []

    def detect(img):
        shapes.append(img.shape[:2])
        return [Detection("cell phone", 0.6, (10, 20, 30, 15))] if len(shapes) == 1 else []

    found = detect_desk(detect, frame, (0.25, 0.2, 0.75, 0.8))
    assert shapes[0] == (600, 1000)  # 책상만 잘라서 본다 (+ 책상 4조각)
    assert len(shapes) == 5
    assert [(d.label, d.box) for d in found] == [("cell phone", (510, 220, 30, 15))]


def test_merge_counts_same_object_once():
    whole = [thing("cup", 100, 100)]
    desk = [Detection("cup", 0.9, (105, 105, 30, 30)), Detection("cell phone", 0.5, (400, 400, 20, 10))]
    assert sorted(d.label for d in merge(whole, desk)) == ["cell phone", "cup"]


def test_yolo_detailed_adds_desk_only_when_set():
    class Y(room_camera.YoloDetector):
        def __init__(self, desk_area):
            self.desk_area = desk_area
            self.calls = 0

        def detect(self, frame):
            self.calls += 1
            return []

    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    plain, desk = Y(None), Y((0.2, 0.2, 0.8, 0.8))
    plain.detect_detailed(frame)
    desk.detect_detailed(frame)
    assert plain.calls == 5 and desk.calls == 10  # 전체+4조각, 책상+책상 4조각


def test_preview_gets_desk_area_in_pixels():
    got = []

    class P:
        def update(self, frame, dets, people, area=None):
            got.append(area)

    class D(FakeDetector):
        desk_area = (0.5, 0.5, 1.0, 1.0)

    class Cam:
        frames = [np.zeros((100, 200, 3), dtype=np.uint8)]

        def read(self):
            if not self.frames:
                raise StopIteration
            return True, self.frames.pop()

    with pytest.raises(StopIteration):
        run_room_camera(RoomWatcher(1), Cam(), D(), lambda e: None,
                        clock=iter(range(100)).__next__, sleep=lambda s: None, preview=P())
    assert got == [(100, 50, 100, 50)]
