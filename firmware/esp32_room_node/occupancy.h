// 재실 센서 읽기 (FR-DEV-101, FR-DEV-103)
// 달린 센서(레이더, PIR)를 읽어서 "있음/없음" 하나로 합친다. 센서 종류는 이 파일 안에서만 안다.
// core는 PIR인지 레이더인지 몰라도 된다 (docs/sensor-events-api.md 3장).
#pragma once
#include <Arduino.h>

// 한 번 읽은 결과
struct OccupancyReading {
  bool occupied;  // 최종 판단: 켜진 센서 중 하나라도 있음이면 true
  int8_t radar;   // 레이더 값 1/0. 레이더를 안 달았으면 -1 (보내지 않음)
  int8_t pir;     // PIR 값 1/0 (마지막 움직임 뒤 PIR_HOLD_MS 동안은 1). PIR을 안 달았으면 -1
};

class OccupancySensor {
 public:
  void begin();
  // 센서를 읽고, 보내야 하면(바뀜 또는 10초) out에 담고 true를 돌려준다
  bool poll(unsigned long nowMs, OccupancyReading& out);

 private:
  OccupancyReading read(unsigned long nowMs);
  unsigned long lastPirMotionMs_ = 0;  // PIR이 마지막으로 움직임을 본 시각 (millis)
  bool pirMotionSeen_ = false;         // 켜진 뒤 PIR이 움직임을 한 번이라도 봤는지
  int lastOccupied_ = -1;       // 마지막으로 보낸 최종 판단. 처음엔 -1이라 첫 값은 무조건 보낸다
  unsigned long lastSentMs_ = 0;  // 마지막으로 보낸 시각 (millis)
  unsigned long lastReadMs_ = 0;  // 마지막으로 읽은 시각 (millis)
};
