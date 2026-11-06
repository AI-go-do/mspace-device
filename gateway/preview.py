"""방 안 카메라가 지금 보는 화면을 브라우저로 보여 준다 (설치·시연 확인용, 기본은 꺼짐).

PREVIEW_PORT=8080 을 붙여 실행하면 같은 네트워크(Tailscale 포함)의 브라우저에서
http://<젯슨 주소>:8080 으로 볼 수 있다. 찾은 사람·물건 상자와 사람 수를 그려서 보여 준다.
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


class Preview:
    """마지막 화면 한 장(JPEG)을 들고 있다가 브라우저에 계속 흘려보낸다."""

    def __init__(self, port, host="0.0.0.0"):
        self.port = port
        self.host = host
        self.jpeg = None
        self.lock = threading.Lock()
        self.server = None

    def update(self, frame, dets, people):
        """상자를 그린 사본을 JPEG로 바꿔 둔다. 원본 frame은 바꾸지 않는다."""
        import cv2
        img = frame.copy()
        for d in dets:
            x, y, w, h = (int(v) for v in d.box)
            color = COLORS.get(d.label, OTHER)
            cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
            cv2.putText(img, f"{d.label} {d.score:.2f}", (x, max(15, y - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        cv2.putText(img, f"people: {people}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
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
        print(f"카메라 화면 보기: 브라우저에서 http://<젯슨 주소>:{self.port} (예: Tailscale IP)")
        return self
