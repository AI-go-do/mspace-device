#include "sender.h"
#include <HTTPClient.h>
#include <WiFiClient.h>
#include <WiFiClientSecure.h>
#include "config.h"
#include "net.h"
#include "secrets_or_default.h"

namespace sender {

// ---------- 큐 (고리 모양 배열) ----------
static Event queue[QUEUE_SIZE];
static size_t head = 0;   // 맨 앞(다음에 보낼 것) 위치
static size_t count = 0;  // 들어 있는 개수

size_t pending() { return count; }

void push(const Event& e) {
  if (count == QUEUE_SIZE) {  // 꽉 참: 가장 오래된 것(맨 앞)을 버린다. 최신 상태가 더 중요해서
    Serial.println("[전송] 큐가 꽉 차서 가장 오래된 이벤트를 버려요");
    head = (head + 1) % QUEUE_SIZE;
    count--;
  }
  queue[(head + count) % QUEUE_SIZE] = e;
  count++;
}

static void popFront() {
  head = (head + 1) % QUEUE_SIZE;
  count--;
}

// ---------- 서버 설정 확인 ----------
enum Mode { MODE_DRY_RUN, MODE_HTTPS, MODE_HTTP_LOCAL, MODE_BLOCKED };
static Mode mode = MODE_DRY_RUN;

void begin() {
  if (DRY_RUN) {
    Serial.println("[전송] DRY_RUN: 서버에 안 보내고 화면에만 찍어요");
    mode = MODE_DRY_RUN;
    return;
  }
  String url = SERVER_URL;
  mode = MODE_BLOCKED;  // 아래 조건을 통과해야 보낸다
  if (strlen(DEVICE_TOKEN) == 0) {
    Serial.println("[전송] 막힘: secrets.h에 DEVICE_TOKEN이 없어요");
  } else if (url.startsWith("https://")) {
    if (strlen(SERVER_CA_PEM) == 0) {
      Serial.println("[전송] 막힘: https 인증서(SERVER_CA_PEM)가 없어요. 확인 없이 보내면 가짜 서버에 토큰이 갈 수 있어요");
    } else {
      mode = MODE_HTTPS;
    }
  } else if (url.startsWith("http://")) {
    if (ALLOW_HTTP_FOR_LOCAL_TEST) {
      Serial.println("[전송] 주의: http로 보내요 (맥 로컬 시험용). 시험이 끝나면 ALLOW_HTTP_FOR_LOCAL_TEST 0으로");
      mode = MODE_HTTP_LOCAL;
    } else {
      Serial.println("[전송] 막힘: 원격 서버는 https://로만 보내요 (장치 토큰 보호)");
    }
  } else {
    Serial.println("[전송] 막힘: SERVER_URL이 비었거나 http(s)://로 시작하지 않아요");
  }
  if (mode == MODE_BLOCKED) Serial.println("[전송] 이벤트는 큐에 모으기만 해요. secrets.h를 고치고 다시 올려 주세요");
}

// ---------- 한 건 보내기 ----------
enum Result { SENT, DROP, RETRY };

// 서버 응답 코드로 다음 행동을 정한다
static Result classify(int status) {
  if (status >= 200 && status < 300) return SENT;  // STORED 또는 DUPLICATE. 둘 다 성공
  if (status >= 300 && status < 400) {             // 리디렉션은 따라가지 않는다 (토큰이 다른 곳으로 가지 않게)
    Serial.printf("[전송] 리디렉션(%d)은 따라가지 않아요. SERVER_URL을 확인하세요\n", status);
    return DROP;
  }
  if (status >= 400 && status < 500) {             // 401 토큰, 403 방·장치 불일치, 400 형식 -> 다시 보내도 같다
    Serial.printf("[전송] 서버가 거부함(%d). 다시 보내지 않아요 (토큰·ROOM_ID·DEVICE_ID 확인)\n", status);
    return DROP;
  }
  Serial.printf("[전송] 실패(%d). 같은 eventId로 다시 보내요\n", status);  // 5xx 또는 연결 실패(음수)
  return RETRY;
}

static int httpPost(const char* json) {
  String url = String(SERVER_URL) + EVENT_PATH;
  HTTPClient http;
  WiFiClientSecure secure;
  WiFiClient plain;
  bool ok;
  if (mode == MODE_HTTPS) {
    secure.setCACert(SERVER_CA_PEM);  // 서버 인증서를 이 루트 인증서로 확인한다
    ok = http.begin(secure, url);
  } else {
    ok = http.begin(plain, url);
  }
  if (!ok) return -1;
  http.setConnectTimeout(CONNECT_TIMEOUT_MS);
  http.setTimeout(RESPONSE_TIMEOUT_MS);
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);
  int status = http.POST((uint8_t*)json, strlen(json));
  http.end();
  return status;  // 음수면 연결 실패
}

// ---------- 큐 처리 ----------
static unsigned long nextTryMs = 0;   // 다음에 보내 볼 시각 (millis)
static unsigned long backoffMs = RETRY_MIN_MS;

void update(unsigned long nowMs) {
  if (count == 0) return;
  const Event& e = queue[head];
  char json[320];

  if (mode == MODE_DRY_RUN) {  // 연습: 화면에 찍고 바로 성공 처리
    eventToJson(e, net::epochNow(), nowMs, json, sizeof(json), true);
    Serial.print("[DRY_RUN] POST "); Serial.print(EVENT_PATH); Serial.print(' '); Serial.println(json);
    popFront();
    return;
  }
  if (mode == MODE_BLOCKED || !net::online()) return;    // 보낼 수 없으면 모아 두기만
  if ((long)(nowMs - nextTryMs) < 0) return;              // 아직 기다리는 중 (재시도 간격)
  if (!eventToJson(e, net::epochNow(), nowMs, json, sizeof(json))) return;  // 시계가 맞춰질 때까지 기다림

  Result r = classify(httpPost(json));
  if (r == RETRY) {
    nextTryMs = millis() + backoffMs;
    backoffMs = min(backoffMs * 2, RETRY_MAX_MS);
    return;
  }
  popFront();  // 성공이거나, 다시 보내도 소용없는 것
  backoffMs = RETRY_MIN_MS;
  nextTryMs = millis();
}

}  // namespace sender
