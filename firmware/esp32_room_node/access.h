// 키패드 번호를 core에 물어본다 (FR-ACC-001, docs/access-api.md 3장)
// 판단은 core만 한다. 장치는 번호를 저장하지 않고, 맞는지 모른다.
// 애매하면 거부한다: 서버 답이 없거나, 설정이 막혔거나, 이상한 답이면 "거부"
#pragma once
#include <Arduino.h>

namespace access {
struct Result {
  bool opened;    // true면 열림 (core가 OPENED라고 답함)
  String reason;  // 거부 이유 (WRONG_CODE, NOT_YET, LOCKED, 서버 응답 없음 …). 시리얼 로그용
};

// 6자리 번호를 확인한다. 답이 올 때까지 기다린다 (보통 1초 안, 서버가 안 되면 최악 약 15초)
// DRY_RUN이면 서버 없이 123456만 연다 (게이트웨이 DRY_RUN과 같음)
Result verify(const String& code);
}
