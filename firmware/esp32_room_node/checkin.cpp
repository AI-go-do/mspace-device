#include "config.h"
#if USE_KEYPAD
#include "checkin.h"
#include "events.h"
#include "net.h"
#include "server.h"

namespace checkin {

// 응답 JSON에서 "key":"값"의 값을 꺼낸다. 없으면 빈 글자.
// 응답이 {"result":"OPENED"} 처럼 짧고 정해진 모양이라 라이브러리 없이 찾는다
static String jsonString(const String& body, const char* key) {
  String k = String("\"") + key + "\"";
  int i = body.indexOf(k);
  if (i < 0) return "";
  i += k.length();
  while (i < (int)body.length() && (body[i] == ' ' || body[i] == ':')) i++;
  if (i >= (int)body.length() || body[i] != '"') return "";
  int end = body.indexOf('"', i + 1);
  return end < 0 ? "" : body.substring(i + 1, end);
}

static Result denied(const char* reason) { return Result{false, reason}; }

Result verify(const String& code) {
  char eventId[37];
  makeUuid(eventId, sizeof(eventId));  // 재시도 때도 같은 eventId → core가 한 번만 처리 (체크인·잠금 횟수 두 번 안 셈)

  if (server::mode() == server::MODE_DRY_RUN) {  // 연습. 번호는 비밀이라 찍지 않는다
    Serial.printf("[DRY_RUN] POST %s %s\n", ACCESS_PATH, eventId);
    return code == "123456" ? Result{true, ""} : denied("WRONG_CODE");
  }
  if (!server::canSend()) return denied("서버 설정 막힘 (위 [서버] 로그 확인)");
  if (!net::online()) return denied("와이파이 끊김");

  char json[200];
  snprintf(json, sizeof(json),
           "{\"eventId\":\"%s\",\"roomId\":%d,\"deviceId\":\"keypad-%d\",\"code\":\"%s\"}",
           eventId, ROOM_ID, ROOM_ID, code.c_str());

  for (int attempt = 1; attempt <= ACCESS_TRIES; attempt++) {
    String body;
    int status = server::post(ACCESS_PATH, json, &body);
    if (status >= 200 && status < 300) {
      if (jsonString(body, "result") == "OPENED") return Result{true, ""};
      String reason = jsonString(body, "reason");
      return Result{false, reason.length() ? reason : String("이상한 응답")};
    }
    if (status >= 300 && status < 500) {  // 리디렉션·토큰·형식 문제는 다시 보내도 같아서 바로 거부
      Serial.printf("[키패드] 서버가 요청을 거부함 (%d), 다시 보내지 않음\n", status);
      return denied(status == 401 ? "토큰 틀림 (401)" : "요청 거부");
    }
    Serial.printf("[키패드] 전송 실패 %d/%d (%d)\n", attempt, ACCESS_TRIES, status);  // 5xx·연결 실패(음수)
    if (attempt < ACCESS_TRIES) delay(ACCESS_RETRY_MS);
  }
  return denied("서버 응답 없음");
}

}  // namespace checkin
#endif  // USE_KEYPAD
