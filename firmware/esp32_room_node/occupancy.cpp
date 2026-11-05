#include "occupancy.h"
#include "config.h"

void OccupancySensor::begin() {
#if USE_RADAR
  pinMode(PIN_RADAR, INPUT);
#endif
#if USE_PIR
  pinMode(PIN_PIR, INPUT);
#endif
}

OccupancyReading OccupancySensor::read() {
  OccupancyReading r = {false, -1, -1};
#if USE_RADAR
  r.radar = digitalRead(PIN_RADAR) == HIGH ? 1 : 0;
  if (r.radar == 1) r.occupied = true;
#endif
#if USE_PIR
  r.pir = digitalRead(PIN_PIR) == HIGH ? 1 : 0;
  if (r.pir == 1) r.occupied = true;
#endif
  return r;
}

bool OccupancySensor::poll(unsigned long nowMs, OccupancyReading& out) {
  if (lastOccupied_ >= 0 && nowMs - lastReadMs_ < SENSOR_READ_MS) return false;  // 너무 자주 읽지 않는다
  lastReadMs_ = nowMs;

  OccupancyReading r = read();
  bool changed = (int)r.occupied != lastOccupied_;
  bool beat = nowMs - lastSentMs_ >= OCCUPANCY_BEAT_MS;  // 빼서 비교하므로 millis()가 0으로 돌아가도 맞다
  if (!changed && !beat) return false;

  lastOccupied_ = r.occupied;
  lastSentMs_ = nowMs;
  out = r;
  return true;
}
