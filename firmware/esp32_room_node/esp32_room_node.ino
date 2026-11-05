// M-SPACE 방 장치 (ESP32용) - 실제 운영에 쓰는 메인 장치
// 방마다 ESP32 하나. 센서를 읽어서 와이파이로 core에 바로 보낸다 (젯슨 없이).
//
// 지금 하는 일: 재실 감지 (FR-DEV-101, FR-DEV-103)
//   - 레이더(LD2410C)·PIR 중 달린 센서를 읽어 "있음/없음" 하나로 합친다
//   - 바뀔 때 바로, 그대로여도 10초마다 보낸다 (core는 1분 안 오면 끊김으로 보고 "있음" 처리)
// 나중에 붙일 일 (자리만 정해 둠, README "ESP32 확장"):
//   - CO2 (FR-DEV-104): sensor 파일 하나 추가 + makeCo2Event()로 큐에 넣기
//   - 키패드 체크인 (FR-ACC-001): 우노 코드의 키패드·LED 부분을 옮겨 오기
//
// 파일 (역할별로 나눠서 하나만 바꿔 끼울 수 있게)
//   config.h        설정값 (방 번호, 핀, 주기, DRY_RUN)
//   secrets.h       와이파이·서버 주소·장치 토큰 (GitHub에 안 올라감, secrets.h.example 복사)
//   occupancy.*     재실 센서 읽기 + 합치기
//   events.*        core 형식 이벤트 만들기 (eventId, observedAt)
//   net.*           와이파이 + 인터넷 시계
//   sender.*        큐 + 서버 전송 (재시도, https 확인) / DRY_RUN 출력
//
// 배선: docs/wiring-esp32.png (레이더 VCC->VIN, GND->GND, OUT->D27)
// 보드: Arduino IDE > 보드 매니저 > esp32 (Espressif) 설치 후 "ESP32 Dev Module"

#include "config.h"
#include "occupancy.h"
#include "events.h"
#include "net.h"
#include "sender.h"

OccupancySensor occupancy;

void setup() {
  Serial.begin(115200);
  delay(300);  // 시리얼 모니터가 붙을 시간
  Serial.printf("M-SPACE ESP32 장치 시작: %d번 방 (%s)\n", ROOM_ID, DEVICE_ID);
  occupancy.begin();
  net::begin();
  sender::begin();
}

void loop() {
  unsigned long now = millis();
  net::update(now);  // 와이파이가 끊겼으면 뒤에서 다시 붙는다 (멈추지 않음)

  OccupancyReading r;
  if (occupancy.poll(now, r)) {  // 바뀌었거나 10초가 지났으면
    sender::push(makeOccupancyEvent(r, now));
    Serial.printf("재실 %d번 방: %s", ROOM_ID, r.occupied ? "있음" : "없음");
    if (r.radar >= 0) Serial.printf(" (레이더=%d", r.radar);
    if (r.pir >= 0) Serial.printf("%sPIR=%d", r.radar >= 0 ? ", " : " (", r.pir);
    if (r.radar >= 0 || r.pir >= 0) Serial.print(")");
    Serial.println();
  }

  sender::update(now);  // 큐 맨 앞 하나를 보낸다 (보낼 때가 됐으면)
}
