"""M-SPACE 게이트웨이 (노트북/젯슨용)

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

# 설정값은 코드를 고치지 않고 실행할 때 환경변수로 바꿀 수 있다
SERVER = os.environ.get("SERVER", "http://localhost:8080")  # 예약 서버(core) 주소
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # True면 서버에 안 보내고 화면에만 찍는다 (서버 없이 테스트용)
# 우노가 꽂힌 USB 포트. 지정 안 하면 리눅스(ttyACM, ttyUSB)·맥(cu.usbmodem)에서 처음 찾은 것을 쓴다. 없으면 None
PORT = os.environ.get("PORT") or (glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/cu.usbmodem*") + [None])[0]
RETRIES = 3  # 서버 전송 실패 시 최대 시도 횟수


def post(path, body):
    """서버로 보내고 응답(JSON)을 돌려준다. 3번 모두 실패하면 None.

    같은 eventId로 다시 보내기 때문에, 서버가 eventId로 중복을 걸러야 한 번만 저장된다.
    """
    if DRY_RUN:
        print("[DRY_RUN] POST", path, json.dumps(body, ensure_ascii=False))
        return {"ok": body.get("code") == "123456"}  # 테스트용: 123456만 통과
    for attempt in range(1, RETRIES + 1):  # attempt = 지금 몇 번째 시도인지 (1, 2, 3)
        try:
            r = requests.post(SERVER + path, json=body, timeout=3)  # r = 서버 응답. 3초 안에 답 없으면 실패
            r.raise_for_status()  # 4xx·5xx 오류 응답도 실패로 처리
            return r.json() if r.content else {}
        except requests.RequestException as e:  # e = 실패 이유 (연결 끊김, 시간 초과 등)
            print(f"전송 실패 {attempt}/{RETRIES}: {e}")
            time.sleep(attempt)  # 1초, 2초, 3초씩 점점 길게 기다렸다 재시도
    return None


def main():
    if not PORT:
        raise SystemExit("우노를 못 찾았어요. USB 연결을 확인하거나 PORT=/dev/ttyACM0 처럼 지정하세요.")
    print("연결:", PORT, "| 서버:", "DRY_RUN" if DRY_RUN else SERVER)
    ser = serial.Serial(PORT, 115200, timeout=1)  # ser = 우노와 연결된 시리얼 통로. 115200은 우노 코드와 같은 속도
    time.sleep(2)  # 우노는 시리얼 연결 시 재부팅되므로 켜질 때까지 기다린다

    while True:
        line = ser.readline().decode(errors="ignore").strip()  # line = 우노가 보낸 한 줄 (글자로 변환, 깨진 글자는 무시)
        if not line.startswith("{"):  # JSON이 아닌 줄(빈 줄 등)은 건너뛴다
            continue
        try:
            event = json.loads(line)  # event = 한 줄을 딕셔너리로 바꾼 것 (eventId, roomId, sensorType ...)
        except json.JSONDecodeError:
            print("JSON 아님:", line)
            continue

        # 장치에는 시계가 없어서 받은 시각(UTC)을 게이트웨이가 붙인다
        event["receivedAt"] = datetime.now(timezone.utc).isoformat()

        if event.get("sensorType") == "keypad":
            res = post("/api/access/verify", event)  # res = 서버의 판단 결과. 예: {"ok": true}
            ok = bool(res and res.get("ok"))  # ok = 번호가 맞았는지. 서버 응답이 없으면 안전하게 거부(False)
            ser.write(b"OK\n" if ok else b"NO\n")  # 우노에 결과를 돌려줘서 LED를 켜게 한다
            print("출입", event["code"], "->", "열림" if ok else "거부")
        else:
            post("/api/sensor-events", event)
            if not DRY_RUN:
                print(event["sensorType"], event["value"])


if __name__ == "__main__":
    main()
