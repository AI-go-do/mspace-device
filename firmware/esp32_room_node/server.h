// core 서버로 POST 하기 (재실 전송 sender가 쓴다)
// - 시작할 때 설정을 한 번 확인한다: 토큰, https 인증서, 원격 http 막기
// - 통과하지 못하면 BLOCKED. 이때는 아무것도 보내지 않는다 (토큰 보호)
#pragma once
#include <Arduino.h>

namespace server {
enum Mode { MODE_DRY_RUN, MODE_HTTPS, MODE_HTTP_LOCAL, MODE_BLOCKED };

void begin();   // 설정 확인. 문제가 있으면 이유를 시리얼에 찍는다
Mode mode();
bool canSend(); // HTTPS나 로컬 http로 보낼 수 있는 상태인가 (DRY_RUN·BLOCKED면 false)

// path(예: "/api/sensor-events")로 JSON을 보낸다. 응답 코드를 돌려주고, 음수면 연결 실패.
// response가 있으면 응답 본문을 담는다
int post(const char* path, const char* json, String* response = nullptr);
}
