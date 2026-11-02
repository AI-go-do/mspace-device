"""M-SPACE 게이트웨이 (노트북/젠슨용)

우노가 USB 시리얼로 보내는 JSON 한 줄을 읽어서 서버로 보낸다.
- 센서 이벤트(pir, radar, light) -> POST {SERVER}/api/sensor-events
- 키패드 번호(keypad)           -> POST {SERVER}/api/access/verify, 결과를 우노에 OK/NO로 돌려줌

서버가 아직 없으면 DRY_RUN=1 로 실행하면 보낼 내용만 화면에 찍는다.

설치:  pip3 install pyserial requests
실행:  DRY_RUN=1 python3 gateway.py
       SERVER=http://서버주소:8080 python3 gateway.py
"""
import glob
import json
import os
import time
from datetime import datetime, timezone

import requests
import serial

SERVER = os.environ.get("SERVER", "http://localhost:8080")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
PORT = os.environ.get("PORT") or (glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/cu.usbmodem*") + [None])[0]
RETRIES = 3


def post(path, body):
    """같은 eventId로 최대 3번 재시도한다. 서버가 eventId로 중복을 걸러야 하는 이유."""
    if DRY_RUN:
        print("[DRY_RUN] POST", path, json.dumps(body, ensure_ascii=False))
        return {"ok": body.get("code") == "123456"}  # 테스트용: 123456만 통과
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(SERVER + path, json=body, timeout=3)
            r.raise_for_status()
            return r.json() if r.content else {}
        except requests.RequestException as e:
            print(f"전송 실패 {attempt}/{RETRIES}: {e}")
            time.sleep(attempt)
    return None


def main():
    if not PORT:
        raise SystemExit("우노를 못 찾았어요. USB 연결을 확인하거나 PORT=/dev/ttyACM0 처럼 지정하세요.")
    print("연결:", PORT, "| 서버:", "DRY_RUN" if DRY_RUN else SERVER)
    ser = serial.Serial(PORT, 115200, timeout=1)
    time.sleep(2)  # 우노는 시리얼 연결 시 재부팅됨

    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print("JSON 아님:", line)
            continue

        # 장치에는 시계가 없어서 받은 시각을 게이트웨이가 붙인다
        event["receivedAt"] = datetime.now(timezone.utc).isoformat()

        if event.get("sensorType") == "keypad":
            res = post("/api/access/verify", event)
            ok = bool(res and res.get("ok"))
            ser.write(b"OK\n" if ok else b"NO\n")
            print("출입", event["code"], "->", "열림" if ok else "거부")
        else:
            post("/api/sensor-events", event)
            if not DRY_RUN:
                print(event["sensorType"], event["value"])


if __name__ == "__main__":
    main()
