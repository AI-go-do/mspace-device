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
    assert gateway.load_config({"PREVIEW_PORT": "8080"}).preview_port == 8080
    assert gateway.start_preview(None) is None
    started = []
    monkeypatch.setattr(preview.Preview, "start", lambda self: started.append(self.port) or self)
    assert gateway.start_preview(8080).port == 8080 and started == [8080]
