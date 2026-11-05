// 와이파이 연결과 인터넷 시계(NTP)
// 끊기면 멈추지 않고 뒤에서 다시 붙는다. 그동안 센서는 계속 읽고 이벤트는 큐에 모아 둔다.
#pragma once
#include <Arduino.h>
#include <time.h>

namespace net {
void begin();                 // 와이파이 연결 시작 (기다리지 않음)
void update(unsigned long nowMs);  // loop()마다. 끊겼으면 일정 간격으로 다시 붙는다
bool online();                // 와이파이 연결됨
time_t epochNow();            // 지금 시각(초, UTC). 시계를 아직 못 맞췄으면 작은 값(1970년대)
bool hasWifiConfig();         // secrets.h에 와이파이 이름이 있나
}
