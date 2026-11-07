import sys
import types
import urllib.request

import gateway
import preview
from room_camera import Detection


class FakeImg:
    def copy(self):
        return self


def fake_cv2(monkeypatch, ok=True):
    drawn = []
    cv2 = types.SimpleNamespace(
        FONT_HERSHEY_SIMPLEX=0, IMWRITE_JPEG_QUALITY=1,
        rectangle=lambda img, a, b, color, t: drawn.append(("box", a, b)),
        putText=lambda img, text, *a: drawn.append(("text", text)),
        imencode=lambda ext, img, params: (ok, types.SimpleNamespace(tobytes=lambda: b"JPEG")),
    )
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    return drawn


def test_update_draws_boxes_and_count(monkeypatch):
    drawn = fake_cv2(monkeypatch)
    p = preview.Preview(0)
    p.update(FakeImg(), [Detection("person", 0.9, (10, 20, 30, 40)), Detection("cup", 0.5, (0, 0, 5, 5))], 1)
    assert ("box", (10, 20), (40, 60)) in drawn
    assert ("text", "people: 1") in drawn
    assert p.latest() == b"JPEG"


def test_update_draws_desk_area(monkeypatch):
    drawn = fake_cv2(monkeypatch)
    preview.Preview(0).update(FakeImg(), [], 0, area=(100, 50, 200, 100))
    assert ("box", (100, 50), (300, 150)) in drawn and ("text", "desk") in drawn


def test_text_grows_with_screen_width():
    assert preview.text_size(types.SimpleNamespace(shape=(1080, 1920, 3))) == (1.2, 2)
    assert preview.text_size(types.SimpleNamespace(shape=(480, 640, 3))) == (0.5, 1)
    assert preview.text_size(FakeImg()) == (0.5, 1)  # 크기를 모르면 작은 기본값


def test_update_keeps_old_frame_when_encode_fails(monkeypatch):
    fake_cv2(monkeypatch, ok=False)
    p = preview.Preview(0)
    p.update(FakeImg(), [], 0)
    assert p.latest() is None


def test_server_serves_page(monkeypatch):
    p = preview.Preview(0, host="127.0.0.1").start()  # 0 = 빈 포트 아무거나
    port = p.server.server_address[1]
    html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read().decode()
    assert "/stream" in html and "저장 안 함" in html
    p.jpeg = b"JPEG"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/stream", timeout=2) as r:
        assert r.headers["Content-Type"].startswith("multipart/x-mixed-replace")
        assert b"JPEG" in r.read(60)
    p.server.shutdown()


def test_preview_off_by_default(monkeypatch):
    assert gateway.load_config({}).preview_port is None
    assert gateway.load_config({}).preview_host == "127.0.0.1"
    assert gateway.load_config({"PREVIEW_HOST": "100.1.2.3"}).preview_host == "100.1.2.3"
    assert gateway.load_config({"PREVIEW_PORT": "8080"}).preview_port == 8080
    assert gateway.start_preview(None) is None
    started = []
    monkeypatch.setattr(preview.Preview, "start", lambda self: started.append((self.host, self.port)) or self)
    assert gateway.start_preview(8080).port == 8080 and started == [("127.0.0.1", 8080)]
    gateway.start_preview(8090, "100.1.2.3")
    assert started[-1] == ("100.1.2.3", 8090)


def test_preview_port_out_of_range_does_not_stop_camera(capsys):
    assert gateway.start_preview(70000) is None
    assert "다른 번호로" in capsys.readouterr().out


def test_preview_port_in_use_does_not_stop_camera(monkeypatch, capsys):
    def busy(self):
        raise OSError("Address already in use")

    monkeypatch.setattr(preview.Preview, "start", busy)
    assert gateway.start_preview(8080) is None
    assert "다른 번호로" in capsys.readouterr().out


def test_preview_host_tailscale_finds_ip(monkeypatch, capsys):
    started = []
    monkeypatch.setattr(preview.Preview, "start", lambda self: started.append(self.host) or self)
    gateway.start_preview(8090, "tailscale", find_tailscale=lambda: "100.126.0.9")
    gateway.start_preview(8090, "tailscale", find_tailscale=lambda: None)
    assert started == ["100.126.0.9", "127.0.0.1"]
    assert "Tailscale 주소를 못 찾아서" in capsys.readouterr().out


def test_tailscale_ip_waits_until_ready():
    class Out:
        def __init__(self, code, text):
            self.returncode, self.stdout = code, text

    answers = [OSError("없음"), Out(1, ""), Out(0, "100.126.215.96\n")]

    def run(*a, **k):
        x = answers.pop(0)
        if isinstance(x, Exception):
            raise x
        return x

    slept = []
    assert gateway.tailscale_ip(run=run, sleep=slept.append, wait=1) == "100.126.215.96"
    assert slept == [1, 1]
    assert gateway.tailscale_ip(run=lambda *a, **k: Out(1, ""), tries=2, sleep=slept.append) is None
