import math
import json

import numpy as np
import pytest
import tidy_eval as te
import vlm_check as vc
from room_camera import Detection


def frame(value, h=480, w=640):
    return np.full((h, w, 3), value, dtype=np.uint8)


def fake_send(answer, calls):
    def send(url, body):
        calls.append(body)
        return answer
    return send


@pytest.mark.parametrize("text,want", [("YES", True), ("Yes.", True), (" no", False), ("NO, nothing", False),
                                        ("maybe", None), ("", None)])
def test_parse_reads_yes_or_no(text, want):
    assert vc.parse(text) is want


def test_prepare_shrinks_long_side():
    import cv2
    img = cv2.imdecode(np.frombuffer(vc.prepare(frame(50, 960, 1280), side=640, eq=True), np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (480, 640)


@pytest.mark.parametrize("rule,status,new,want", [
    ("vlm", "CLEAN", True, "LEFT_ITEMS"), ("vlm", "LEFT_ITEMS", False, "CLEAN"),
    ("and", "LEFT_ITEMS", False, "CLEAN"), ("and", "NEEDS_CLEANING", True, "NEEDS_CLEANING"), ("and", "CLEAN", True, "CLEAN"),
    ("or", "CLEAN", True, "LEFT_ITEMS"), ("or", "NEEDS_CLEANING", False, "NEEDS_CLEANING"), ("or", "CLEAN", False, "CLEAN"),
    ("vlm", "LEFT_ITEMS", None, "LEFT_ITEMS")])
def test_combine_rules(rule, status, new, want):
    check = vc.VlmCheck("http://x", rule=rule, send=fake_send("", []))
    assert check.combine(status, new) == want


def test_bad_rule_is_refused():
    with pytest.raises(ValueError):
        vc.VlmCheck("http://x", rule="maybe", send=fake_send("", []))


def test_ask_sends_two_photos_and_caches_answer(tmp_path):
    calls = []
    cache = tmp_path / "vlm.json"
    ticks = iter([0.0, 7.5])
    check = vc.VlmCheck("http://x", cache=str(cache), send=fake_send("YES", calls), clock=lambda: next(ticks))
    assert check.ask(frame(0), frame(9)) == (True, 7.5, False)
    images = [p for p in calls[0]["messages"][0]["content"] if p["type"] == "image_url"]
    assert len(images) == 2 and calls[0]["temperature"] == 0
    saved = json.loads(cache.read_text())
    assert list(saved.values()) == [{"text": "YES", "seconds": 7.5, "yes": None}]  # 사진은 적지 않는다
    assert calls[0]["logprobs"] is True  # 첫 단어 후보 확률도 받는다 (이 사진의 "있음" 확률)

    again = vc.VlmCheck("http://x", cache=str(cache), send=fake_send("NO", calls))
    assert again.ask(frame(0), frame(9)) == (True, 7.5, True)  # 같은 사진이면 다시 묻지 않음
    assert len(calls) == 1


def test_from_env_is_off_without_url():
    assert vc.from_env({}) is None
    check = vc.from_env({"VLM_URL": "http://127.0.0.1:1", "VLM_RULE": "OR", "VLM_EQ": "1", "VLM_SIDE": "512"})
    assert (check.rule, check.eq, check.side) == ("or", True, 512)


def test_from_env_refuses_a_vlm_outside_this_jetson():
    with pytest.raises(ValueError, match="젯슨 안"):
        vc.from_env({"VLM_URL": "http://10.0.0.5:8091"})  # 채점에서도 사진이 밖으로 나가지 않게


def test_judge_combines_like_the_gateway():
    vlm = vc.VlmCheck("http://x", rule="or", send=fake_send("YES", []), clock=iter([0.0, 3.0]).__next__)
    check, _ = te.judge(frame(0), frame(1), lambda f: [], vlm=vlm)
    assert (check["yoloStatus"], check["status"]) == ("CLEAN", "NEEDS_CLEANING")
    assert check["trash"] == {"unknown": 1}


def test_judge_lets_vlm_fix_a_false_report():
    find = lambda f: [Detection("cell phone", 0.9, (10, 10, 20, 20))] if f[0, 0, 0] == 1 else []
    vlm = vc.VlmCheck("http://x", rule="and", send=fake_send("NO", []), clock=iter([0.0, 3.0]).__next__)
    check, seconds = te.judge(frame(0), frame(1), find, vlm=vlm)
    assert (check["yoloStatus"], check["vlm"], check["status"]) == ("LEFT_ITEMS", False, "CLEAN")
    assert seconds >= 3.0


def test_candidate_name_keeps_vlm_settings():
    assert "VLM_RULE=or" in te.candidate_name({"VLM_NAME": "smol", "VLM_RULE": "or", "VLM_URL": "http://127.0.0.1:8080"})
    only_url = te.candidate_name({"VLM_URL": "http://127.0.0.1:8080"})
    assert "VLM_URL" not in only_url and only_url == "VLM_RULE=or"  # VLM을 켠 것은 남긴다 (model_score가 읽음)


# ---------- 게이트웨이에서 쓰기 (VLM_URL, 10판 1등) ----------

import gateway  # noqa: E402
import room_camera  # noqa: E402
from room_camera import DESK_DIFF_CONF, SETTLE_SEC, RoomWatcher, apply_vlm, tidy_body  # noqa: E402

FRAME = frame(120, 720, 1280)


def person():
    return Detection("person", 0.9, (0, 0, 50, 100))


def base_check(status="CLEAN", left=None, trash=None):
    return {"roomId": 1, "status": status, "trash": dict(trash or {}), "leftItems": dict(left or {}),
            "confidence": {}, "checkedAt": "t", "reservationId": 7, "phase": "FIRST"}


def test_apply_vlm_or_adds_an_unknown_thing_with_low_confidence():
    c = apply_vlm(base_check(), True, "or")
    assert c["status"] == "NEEDS_CLEANING" and c["trash"] == {"unknown": 1} and c["vlm"] is True
    body = tidy_body(c, "camera-1")
    assert body["result"] == "LEFT_ITEMS" and body["items"] == [{"name": "unknown", "confidence": DESK_DIFF_CONF}]
    assert apply_vlm(base_check(), False, "or")["status"] == "CLEAN"
    found = apply_vlm(base_check("LEFT_ITEMS", left={"cell phone": 1}), False, "or")
    assert found["leftItems"] == {"cell phone": 1}  # or는 YOLO가 찾은 것을 지우지 않는다
    assert apply_vlm(base_check(), None, "or")["status"] == "CLEAN"  # 못 알아들으면 그대로


def test_apply_vlm_and_and_vlm_rules():
    assert apply_vlm(base_check("LEFT_ITEMS", left={"cup": 1}), False, "and")["status"] == "CLEAN"
    assert apply_vlm(base_check(), True, "and")["status"] == "CLEAN"
    assert apply_vlm(base_check(), True, "vlm")["status"] == "NEEDS_CLEANING"
    assert apply_vlm(base_check("LEFT_ITEMS", left={"cup": 1}), False, "vlm")["leftItems"] == {}


def run_room(w, dets, start, sec):
    out = []
    for t in range(start, start + sec):
        c = w.update(dets, now=t, photo=lambda: FRAME)[1]
        if c and c["status"] != "BASELINE_SAVED":
            out.append(c)
    return out


def test_watcher_keeps_vlm_entry_photo_and_attaches_pair_to_first_check():
    vlm = vc.VlmCheck("http://127.0.0.1:1", send=fake_send("YES", []))
    w = RoomWatcher(1, on_checkout=True, vlm=vlm)
    run_room(w, [], 0, 5)
    run_room(w, [person()], 5, 5)
    assert w.entry["vlm"][:2] == b"\xff\xd8"  # 사람이 보이기 직전 빈 방, VLM 크기 JPEG (메모리에만)
    w.request_check(1, 10)
    out = run_room(w, [], 10, SETTLE_SEC + 6)
    entry_jpg = w.entry["vlm"] if w.entry else None
    a, b = out[0]["vlmPhotos"]
    assert a[:2] == b[:2] == b"\xff\xd8" and entry_jpg is None  # 보낸 뒤 기준은 지운다


def test_recheck_asks_vlm_again_with_kept_entry_photo():
    """11/12 수연: 3시간 뒤에도 입실 전 사진과 지금 사진을 VLM에게 다시 묻는다 (입실 전 사진은 메모리에만 보관)."""
    vlm = vc.VlmCheck("http://127.0.0.1:1", send=fake_send("YES", []))
    w = RoomWatcher(1, on_checkout=True, vlm=vlm)
    run_room(w, [], 0, 5)
    run_room(w, [person()], 5, 5)
    entry = w.entry["vlm"]
    w.request_check(1, 10)
    first = run_room(w, [], 10, SETTLE_SEC + 6)[0]
    assert first["status"] == "CLEAN" and "vlmPhotos" in first  # YOLO는 깨끗해도 VLM 답을 기다리니 기준을 남긴다
    assert w.recheck_base["vlm"] == entry and w.entry is None
    w.request_check(1, 100, "RECHECK")
    again = run_room(w, [], 100, SETTLE_SEC + 6)[0]
    assert again["phase"] == "RECHECK" and again["vlmPhotos"][0] == entry
    assert w.recheck_base is None  # 다시 확인이 끝나면 사진도 버린다


def test_first_check_clean_by_vlm_too_drops_kept_photo(tmp_path):
    w = RoomWatcher(1, on_checkout=True, vlm=vc.VlmCheck("http://127.0.0.1:1", send=fake_send("NO", [])))
    w.recheck_base = {"reservationId": 7, "objects": {}, "vlm": b"x"}
    room_camera.ask_vlm_then_finish(w, base_check(), (b"a", b"b"), str(tmp_path / "log.jsonl"), None, None)
    assert w.recheck_base is None


def test_recheck_vlm_photo_is_dropped_when_someone_enters():
    vlm = vc.VlmCheck("http://127.0.0.1:1", send=fake_send("YES", []))
    w = RoomWatcher(1, on_checkout=True, vlm=vlm)
    run_room(w, [], 0, 5)
    run_room(w, [person()], 5, 5)
    w.request_check(1, 10)
    run_room(w, [], 10, SETTLE_SEC + 6)
    assert w.recheck_base["vlm"]
    run_room(w, [person()], 40, 5)
    assert w.recheck_base is None


def test_passer_by_does_not_cancel_recheck(monkeypatch):
    """11/12 예약 51: 지나가는 사람이 몇 초 찍혀 다시 확인이 취소됨. ENTRY_PERSON_SEC(10초) 넘게 보여야 들어온 것."""
    monkeypatch.setattr(room_camera, "ENTRY_PERSON_SEC", 10)
    vlm = vc.VlmCheck("http://127.0.0.1:1", send=fake_send("YES", []))
    w = RoomWatcher(1, on_checkout=True, vlm=vlm)
    run_room(w, [], 0, 5)
    run_room(w, [person()], 5, 15)
    w.request_check(1, 20)
    run_room(w, [], 20, SETTLE_SEC + 6)
    assert w.recheck_base["vlm"]
    run_room(w, [person()], 40, 5)  # 5초 지나감
    run_room(w, [], 45, 5)
    assert w.recheck_base is not None and w.person_since is None
    run_room(w, [person()], 50, 12)  # 12초 머묾: 들어온 것
    assert w.recheck_base is None


def test_ask_vlm_on_recheck_sends_vlm_confidence(tmp_path):
    sent = []
    w = RoomWatcher(1, on_checkout=True, vlm=vc.VlmCheck("http://127.0.0.1:1", send=lambda url, body: ("YES", 0.97)))
    check = {**base_check("LEFT_ITEMS", left={"handbag": 1}), "phase": "RECHECK",
             "confidence": {"handbag": [0.6]}}
    room_camera.ask_vlm_then_finish(w, check, (b"a", b"b"), str(tmp_path / "log.jsonl"), sent.append, None)
    assert sent[0]["phase"] == "RECHECK" and sent[0]["items"] == [{"name": "handbag", "confidence": 0.97}]


def test_watcher_without_vlm_attaches_nothing():
    w = RoomWatcher(1, on_checkout=True)
    run_room(w, [], 0, 5)
    run_room(w, [person()], 5, 5)
    w.request_check(1, 10)
    out = run_room(w, [], 10, SETTLE_SEC + 6)
    assert "vlmPhotos" not in out[0] and w.vlm is None


def test_ask_vlm_then_finish_sends_combined_result(tmp_path, capsys):
    sent = []
    w = RoomWatcher(1, on_checkout=True, vlm=vc.VlmCheck("http://127.0.0.1:1", send=fake_send("YES", []),
                                                         clock=iter([0.0, 31.0]).__next__))
    room_camera.ask_vlm_then_finish(w, base_check(), (b"a", b"b"), str(tmp_path / "log.jsonl"), sent.append, None)
    assert sent[0]["result"] == "LEFT_ITEMS" and sent[0]["items"][0]["name"] == "unknown"
    assert "VLM 1번 방 예약 7: 새 물건 있음 (31초)" in capsys.readouterr().out
    assert json.loads((tmp_path / "log.jsonl").read_text())["vlm"] is True


def test_ask_vlm_then_finish_falls_back_to_yolo_when_server_is_down(tmp_path, capsys):
    def down(url, body):
        raise OSError("connection refused")

    sent = []
    w = RoomWatcher(1, on_checkout=True, vlm=vc.VlmCheck("http://127.0.0.1:1", send=down))
    room_camera.ask_vlm_then_finish(w, base_check(), (b"a", b"b"), str(tmp_path / "log.jsonl"), sent.append, None)
    assert sent[0]["result"] == "CLEAN"  # 결과는 버리지 않고 YOLO 결과로 보낸다
    assert "YOLO 결과만 보내요" in capsys.readouterr().out


def test_run_room_camera_asks_vlm_in_another_thread(tmp_path, monkeypatch):
    class Cam:
        def read(self):
            return True, FRAME

    class Det:
        finds_objects = True

        def detect(self, f):
            return []

    class Once(RoomWatcher):
        def update(self, dets, now, pir=None, now_iso=None, detail=None, photo=None):
            assert photo is not None  # VLM을 켜면 화면을 넘긴다
            c = base_check()
            c["vlmPhotos"] = (b"a", b"b")
            return None, c

    started, sent = [], []
    monkeypatch.setattr(room_camera, "start_thread", lambda target, *args: started.append((target, args)))
    w = Once(1, on_checkout=True, vlm=vc.VlmCheck("http://127.0.0.1:1", send=fake_send("NO", [])))
    ticks = iter(range(10))
    with pytest.raises(StopIteration):
        room_camera.run_room_camera(w, Cam(), Det(), lambda e: None, log_path=str(tmp_path / "l.jsonl"),
                                    clock=lambda: next(ticks), sleep=lambda s: None, send_check=sent.append)
    assert started and started[0][0] is room_camera.ask_vlm_then_finish and not sent  # 카메라 스레드는 기다리지 않음
    target, args = started[0]
    target(*args)
    assert sent[0]["result"] == "CLEAN"


def test_make_vlm_only_talks_to_this_jetson(capsys):
    assert gateway.make_vlm(gateway.load_config({"PORT": "x"})) is None
    assert gateway.make_vlm(gateway.load_config({"PORT": "x", "VLM_URL": "http://10.0.0.5:8091"})) is None
    assert "젯슨 안(127.0.0.1)만" in capsys.readouterr().out
    config = gateway.load_config({"PORT": "x", "VLM_URL": "http://127.0.0.1:1", "VLM_RULE": "maybe"})
    assert config.vlm_rule == "maybe"
    vlm = gateway.make_vlm(config)
    assert vlm.rule == "or" and "or로 해요" in capsys.readouterr().out
    assert gateway.load_config({"PORT": "x"}).vlm_rule == "or"


def test_make_vlm_uses_the_same_photo_settings_as_scoring():
    vlm = gateway.make_vlm(gateway.load_config({"PORT": "x", "VLM_URL": "http://127.0.0.1:1",
                                                "VLM_SIDE": "512", "VLM_EQ": "1"}))
    assert (vlm.side, vlm.eq) == (512, True)
    plain = gateway.make_vlm(gateway.load_config({"PORT": "x", "VLM_URL": "http://127.0.0.1:1"}))
    assert (plain.side, plain.eq) == (vc.SIDE, False)
    assert vlm.timeout == vc.GATEWAY_TIMEOUT < vc.TIMEOUT  # 게이트웨이는 짧게, 채점은 길게
    assert vc.from_env({"VLM_URL": "http://127.0.0.1:1"}).timeout == vc.TIMEOUT


def test_vlm_check_passes_its_timeout_to_the_server_call(monkeypatch):
    seen = []
    monkeypatch.setattr(vc, "model_name", lambda url: "m")
    monkeypatch.setattr(vc.DIRECT, "open", lambda req, timeout: seen.append(timeout) or (_ for _ in ()).throw(OSError("x")))
    check = vc.VlmCheck("http://127.0.0.1:1", timeout=90)
    with pytest.raises(OSError):
        check.send(check.url, {})
    assert seen == [90]


def test_vlm_requests_ignore_proxy_settings(monkeypatch):
    import http.server
    import threading
    seen = []

    class Answer(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.path)
            body = json.dumps({"choices": [{"message": {"content": "NO"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Answer)
    threading.Thread(target=server.handle_request, daemon=True).start()
    monkeypatch.setenv("http_proxy", "http://10.255.255.1:9")  # 이 프록시로 가면 실패한다
    monkeypatch.setenv("no_proxy", "")
    import importlib
    importlib.reload(vc)  # 프록시 설정이 있는 젯슨에서 게이트웨이를 켠 것처럼
    assert vc.post(f"http://127.0.0.1:{server.server_port}", {}, timeout=5) == ("NO", None)  # 사진은 젯슨 안에서 바로 간다
    assert seen == ["/v1/chat/completions"]
    server.server_close()


def test_yes_probability_from_first_token():
    choice = {"logprobs": {"content": [{"token": "YES", "logprob": -0.05, "top_logprobs": [
        {"token": "YES", "logprob": math.log(0.9)}, {"token": "NO", "logprob": math.log(0.1)},
        {"token": "Maybe", "logprob": -9}]}]}}
    assert vc.yes_probability(choice) == 0.9
    assert vc.yes_probability({"message": {"content": "YES"}}) is None  # 옛 서버: 후보 없음
    only = {"logprobs": {"content": [{"token": "YES", "logprob": math.log(0.6)}]}}  # top_logprobs 없이 고른 토큰만
    assert vc.yes_probability(only) is None  # 반대쪽 몫을 모르면 0.6을 1.0으로 부풀리지 않는다


def test_ask_jpeg_returns_yes_probability(tmp_path):
    vlm = vc.VlmCheck("http://x", send=lambda url, body: ("YES", 0.97), clock=iter([0.0, 2.0]).__next__)
    assert vlm.ask_jpeg(b"a", b"b") == (True, 2.0, 0.97)
