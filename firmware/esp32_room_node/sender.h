// 이벤트를 core로 보내기 (docs/sensor-events-api.md 4·5장)
// - 큐: 이벤트를 차례로 담아 두고 앞에서부터 보낸다. 와이파이가 끊겨도 모아 뒀다가 순서대로 보낸다
// - 재시도: 서버 오류(5xx)·연결 실패면 "같은 eventId"로 다시 보낸다. 간격은 1초, 2초, 4초 … 최대 30초
// - 다시 안 보냄: 4xx(토큰·방·형식 오류)는 다시 보내도 같아서 로그만 남기고 버린다
// - DRY_RUN: 서버에 안 보내고 시리얼 모니터에 보낼 JSON만 찍는다
#pragma once
#include <Arduino.h>
#include "events.h"

namespace sender {
void begin();                       // 설정 확인 (https 인증서, 토큰). 문제가 있으면 이유를 찍는다
void push(const Event& e);          // 큐에 넣는다. 꽉 차면 가장 오래된 것을 버린다
void update(unsigned long nowMs);   // loop()마다. 보낼 때가 됐으면 맨 앞 하나를 보낸다
size_t pending();                   // 아직 못 보낸 개수
}
