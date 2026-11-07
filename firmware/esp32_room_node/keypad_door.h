// 방 문 앞 키패드 + LED (FR-ACC-001, USE_KEYPAD 1일 때만). 우노 펌웨어와 같은 사용법:
//   숫자 6자리 + #  → core에 확인 → 초록(열림) / 빨강(거부) LED 1.5초
//   *               → 처음부터 다시
//   6자리가 아닌데 # → 빨강
// 서버 답을 기다리는 동안 초록 LED가 깜빡인다 (Ticker가 뒤에서 깜빡여서, 답을 기다리며 멈춰 있어도 깜빡임)
#pragma once
#include <Arduino.h>

namespace door {
void begin();
void update(unsigned long nowMs);  // loop()마다. 키를 읽고, 6자리 + #이면 서버에 확인한다
}
