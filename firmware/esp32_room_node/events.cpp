#include "events.h"
#include "config.h"
#include <time.h>

// 새 UUID(버전 4)를 만든다. ESP32의 random()은 하드웨어 난수를 쓴다
static void makeUuid(char* out, size_t n) {
  uint8_t b[16];
  for (int i = 0; i < 16; i++) b[i] = (uint8_t)random(256);
  b[6] = (b[6] & 0x0F) | 0x40;  // 버전 4 표시
  b[8] = (b[8] & 0x3F) | 0x80;  // UUID 규격 표시
  snprintf(out, n, "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
           b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]);
}

Event makeOccupancyEvent(const OccupancyReading& r, unsigned long nowMs) {
  Event e = {};
  makeUuid(e.eventId, sizeof(e.eventId));
  e.kind = KIND_OCCUPANCY;
  e.occ = r;
  e.madeMs = nowMs;
  return e;
}

Event makeCo2Event(uint16_t ppm, unsigned long nowMs) {
  Event e = {};
  makeUuid(e.eventId, sizeof(e.eventId));
  e.kind = KIND_CO2;
  e.ppm = ppm;
  e.madeMs = nowMs;
  return e;
}

// 센서가 본 시각을 "2026-11-05T10:00:00+09:00" 모양으로. 만든 뒤 지난 시간만큼 지금 시각에서 뺀다
static void observedAtIso(const Event& e, time_t epochNow, unsigned long nowMs, char* out, size_t n) {
  time_t seen = epochNow - (time_t)((nowMs - e.madeMs) / 1000UL) + KST_OFFSET_SEC;
  struct tm t;
  gmtime_r(&seen, &t);  // +9시간 더한 값을 그대로 쓰므로 한국 시각
  strftime(out, n, "%Y-%m-%dT%H:%M:%S+09:00", &t);
}

// 센서별 값 {"radar":true,"pir":false}. 안 단 센서는 빼고, 하나도 없으면 빈 글자
static void sourcesJson(const OccupancyReading& r, char* out, size_t n) {
  out[0] = '\0';
  if (r.radar < 0 && r.pir < 0) return;
  size_t len = snprintf(out, n, ",\"sources\":{");
  bool first = true;
  if (r.radar >= 0) { len += snprintf(out + len, n - len, "\"radar\":%s", r.radar ? "true" : "false"); first = false; }
  if (r.pir >= 0)   { len += snprintf(out + len, n - len, "%s\"pir\":%s", first ? "" : ",", r.pir ? "true" : "false"); }
  snprintf(out + len, n - len, "}");
}

bool eventToJson(const Event& e, time_t epochNow, unsigned long nowMs, char* out, size_t n, bool allowNoTime) {
  char observedAt[36];
  if (epochNow >= 1700000000) {  // 2023년 이후면 시계가 맞춰진 것
    observedAt[0] = '"';
    observedAtIso(e, epochNow, nowMs, observedAt + 1, sizeof(observedAt) - 2);
    strcat(observedAt, "\"");
  } else if (allowNoTime) {
    strcpy(observedAt, "null");  // DRY_RUN 화면용. 진짜 전송에서는 쓰지 않는다
  } else {
    return false;  // 시계를 아직 못 맞춤 -> 보내지 말고 기다린다
  }

  if (e.kind == KIND_OCCUPANCY) {
    char sources[48];
    sourcesJson(e.occ, sources, sizeof(sources));
    snprintf(out, n,
             "{\"eventId\":\"%s\",\"roomId\":%d,\"deviceId\":\"%s\",\"kind\":\"OCCUPANCY\","
             "\"occupied\":%s%s,\"observedAt\":%s}",
             e.eventId, ROOM_ID, DEVICE_ID, e.occ.occupied ? "true" : "false", sources, observedAt);
  } else {
    snprintf(out, n,
             "{\"eventId\":\"%s\",\"roomId\":%d,\"deviceId\":\"%s\",\"kind\":\"CO2\","
             "\"ppm\":%u,\"observedAt\":%s}",
             e.eventId, ROOM_ID, DEVICE_ID, (unsigned)e.ppm, observedAt);
  }
  return true;
}
