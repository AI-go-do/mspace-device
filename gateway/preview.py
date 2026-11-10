"""방 안 카메라가 지금 보는 화면을 브라우저로 보여 준다 (설치·시연 확인용, 기본은 꺼짐).

PREVIEW_PORT=8090 을 붙여 실행하면 젯슨 안에서만 열린다(127.0.0.1). 맥에서 보려면
PREVIEW_HOST=$(tailscale ip -4) 로 Tailscale 주소에만 열어서 팀 Tailscale 안에서만 보이게 한다. 찾은 사람·물건 상자와 사람 수를 그려서 보여 준다.
사진은 메모리에 마지막 한 장만 두고, 파일로 저장하거나 서버로 보내지 않는다.
"""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>M-SPACE 방 안 카메라</title>
<style>body{margin:0;background:#111;color:#eee;font-family:sans-serif;text-align:center}
img{max-width:100%;height:auto}</style></head>
<body><h3>방 안 카메라 (저장 안 함)</h3><img src="/stream"></body></html>"""

COLORS = {"person": (0, 200, 0)}  # 사람은 초록, 물건은 주황
OTHER = (0, 140, 255)
DESK = (255, 200, 0)  # 책상 부분(DESK_AREA)은 하늘색


def text_size(img):
    """화면 폭에 맞춘 글자 크기와 선 굵기. 1280 폭이면 0.8, 풀HD(1920)면 1.2."""
    shape = getattr(img, "shape", None)
    width = shape[1] if shape is not None and len(shape) > 1 else 640
    size = max(0.5, width / 1600)
    return size, max(1, round(size * 2))


class Preview:
    """마지막 화면 한 장(JPEG)을 들고 있다가 브라우저에 계속 흘려보낸다."""

    def __init__(self, port, host="127.0.0.1"):
        self.port = port
        self.host = host
        self.jpeg = None
        self.lock = threading.Lock()
        self.server = None

    def update(self, frame, dets, people, area=None, floor=None):
        """상자를 그린 사본을 JPEG로 바꿔 둔다. 원본 frame은 바꾸지 않는다.

        area: 책상 부분(DESK_AREA) 픽셀 상자. 있으면 하늘색으로 그려서 책상에 맞는지 보며 고칠 수 있게 한다.
        floor: 바닥 부분(FLOOR_AREA) 픽셀 상자. 같은 색으로 "floor"라고 그린다.
        """
        import cv2
        img = frame.copy()
        size, thick = text_size(img)
        for box, name in ((area, "desk"), (floor, "floor")):
            if box:
                x, y, w, h = (int(v) for v in box)
                cv2.rectangle(img, (x, y), (x + w, y + h), DESK, thick)
                cv2.putText(img, name, (x + 5, y + int(30 * size)), cv2.FONT_HERSHEY_SIMPLEX, size, DESK, thick)
        for d in dets:
            x, y, w, h = (int(v) for v in d.box)
            color = COLORS.get(d.label, OTHER)
            cv2.rectangle(img, (x, y), (x + w, y + h), color, thick)
            cv2.putText(img, f"{d.label} {d.score:.2f}", (x, max(int(30 * size), y - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, size, color, thick)
        cv2.putText(img, f"people: {people}", (15, int(60 * size)), cv2.FONT_HERSHEY_SIMPLEX, 2 * size,
                    (255, 255, 255), thick + 1)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            with self.lock:
                self.jpeg = buf.tobytes()

    def latest(self):
        with self.lock:
            return self.jpeg

    def start(self):
        preview = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 접속할 때마다 화면에 찍지 않는다
                pass

            def do_GET(self):
                if self.path == "/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()
                    try:
                        while True:
                            jpeg = preview.latest()
                            if jpeg:
                                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
                            time.sleep(0.5)
                    except (BrokenPipeError, ConnectionResetError):
                        return  # 브라우저를 닫음
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        print(f"카메라 화면 보기: 브라우저에서 http://{self.host}:{self.port}")
        return self
