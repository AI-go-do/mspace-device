// core로 보내는 이벤트 모양 (docs/sensor-events-api.md 2장)
// 센서 값을 "공통 모양"으로 바꾸고, 보낼 때 JSON 글자로 만든다.
#pragma once
#include <Arduino.h>
#include "occupancy.h"

enum EventKind : uint8_t {
  KIND_OCCUPANCY = 0,  // 재실
  KIND_CO2 = 1,        // CO2 (FR-DEV-104). 센서가 붙으면 makeCo2Event로 만든다
};

// 보낼 이벤트 한 건. 큐에 담겨 있다가 서버로 간다
struct Event {
  char eventId[37];      // UUID. 신호마다 새로 만들고, 재시도 때는 같은 값 그대로
  EventKind kind;
  OccupancyReading occ;  // 재실일 때
  uint16_t ppm;          // CO2일 때
  unsigned long madeMs;  // 만든 시각 (millis). 시계가 늦게 맞춰져도 이걸로 실제 시각을 거꾸로 계산한다
};

// 새 UUID(버전 4). 이벤트마다 하나
void makeUuid(char* out, size_t n);

Event makeOccupancyEvent(const OccupancyReading& r, unsigned long nowMs);
Event makeCo2Event(uint16_t ppm, unsigned long nowMs);

// 이벤트를 JSON 한 줄로 만든다. 시계를 아직 못 맞췄으면 false (보내지 말고 기다린다)
// epochNow: 지금 시각(초, UTC 기준), nowMs: 지금 millis
// allowNoTime: true면 시계가 없어도 observedAt을 null로 채워 만든다 (DRY_RUN 화면용)
bool eventToJson(const Event& e, time_t epochNow, unsigned long nowMs, char* out, size_t n, bool allowNoTime = false);
