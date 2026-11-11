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
    assert list(saved.values()) == [{"text": "YES", "seconds": 7.5}]  # 사진은 적지 않는다

    again = vc.VlmCheck("http://x", cache=str(cache), send=fake_send("NO", calls))
    assert again.ask(frame(0), frame(9)) == (True, 7.5, True)  # 같은 사진이면 다시 묻지 않음
    assert len(calls) == 1


def test_from_env_is_off_without_url():
    assert vc.from_env({}) is None
    check = vc.from_env({"VLM_URL": "http://127.0.0.1:1", "VLM_RULE": "OR", "VLM_EQ": "1", "VLM_SIDE": "512"})
    assert (check.rule, check.eq, check.side) == ("or", True, 512)


def test_judge_lets_vlm_fix_a_false_report():
    find = lambda f: [Detection("cell phone", 0.9, (10, 10, 20, 20))] if f[0, 0, 0] == 1 else []
    vlm = vc.VlmCheck("http://x", rule="and", send=fake_send("NO", []), clock=iter([0.0, 3.0]).__next__)
    check, seconds = te.judge(frame(0), frame(1), find, vlm=vlm)
    assert (check["yoloStatus"], check["vlm"], check["status"]) == ("LEFT_ITEMS", False, "CLEAN")
    assert seconds >= 3.0


def test_candidate_name_keeps_vlm_settings():
    assert "VLM_RULE=or" in te.candidate_name({"VLM_NAME": "smol", "VLM_RULE": "or", "VLM_URL": "http://127.0.0.1:8080"})
    assert "VLM_URL" not in te.candidate_name({"VLM_URL": "http://127.0.0.1:8080"})
