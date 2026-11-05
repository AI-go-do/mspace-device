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
// http 시험을 허용할 주소인가: 사설망 IP(10.x, 172.16~31.x, 192.168.x)나 .local 이름만.
// "http://192.168.evil.com"처럼 앞부분만 같은 이름은 막도록 호스트 부분을 통째로 확인한다
static bool isLocalHost(const String& url) {
  String host = url.substring(7);  // "http://" 뒤
  if (host.indexOf('@') >= 0) return false;  // user@host 꼴은 받지 않는다 ("10.1.2.3:80@evil.com"도 막게 자르기 전에 본다)
  int end = host.length();
  const char stops[] = {'/', ':', '?', '#'};
  for (char c : stops) {
    int i = host.indexOf(c);
    if (i >= 0 && i < end) end = i;
  }
  host = host.substring(0, end);
  if (host.endsWith(".local") && host.length() > 6) return true;
  int a, b, c, d;
  char extra;
  if (sscanf(host.c_str(), "%d.%d.%d.%d%c", &a, &b, &c, &d, &extra) != 4) return false;
  if (a < 0 || a > 255 || b < 0 || b > 255 || c < 0 || c > 255 || d < 0 || d > 255) return false;
  return a == 10 || (a == 172 && b >= 16 && b <= 31) || (a == 192 && b == 168);
}

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
    if (ALLOW_HTTP_FOR_LOCAL_TEST && !isLocalHost(url)) {
      Serial.println("[전송] 막힘: http 시험은 같은 와이파이의 사설 주소(192.168.x.x 등)나 .local만 돼요");
    } else if (ALLOW_HTTP_FOR_LOCAL_TEST) {
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
// 버리는 건 "이 이벤트 자체가 잘못됐다"는 답(400·409·413·422)뿐이다.
// 401·403(토큰·방 설정), 404(서버에 아직 경로 없음), 408·429(바쁨), 3xx(주소 설정)는
// 장치나 서버 설정 문제라서 이벤트는 그대로 두고 간격을 늘려 다시 보낸다 (재실 기록이 사라지지 않게)
static Result classify(int status) {
  if (status >= 200 && status < 300) return SENT;  // STORED 또는 DUPLICATE. 둘 다 성공
  if (status == 400 || status == 409 || status == 413 || status == 422) {
    Serial.printf("[전송] 서버가 이 이벤트를 거부함(%d). 형식 문제라 이것만 버려요\n", status);
    return DROP;
  }
  if (status >= 300 && status < 400) {             // 리디렉션은 따라가지 않는다 (토큰이 다른 곳으로 가지 않게)
    Serial.printf("[전송] 리디렉션(%d)은 따라가지 않아요. SERVER_URL을 확인하세요. 큐는 그대로 둬요\n", status);
  } else if (status == 401 || status == 403) {
    Serial.printf("[전송] 막힘(%d): 토큰·ROOM_ID·DEVICE_ID를 확인하세요. 큐는 그대로 둬요\n", status);
  } else if (status == 404) {
    Serial.println("[전송] 막힘(404): 서버에 경로가 아직 없어요 (EVENT_PATH 확인). 큐는 그대로 둬요");
  } else {
    Serial.printf("[전송] 실패(%d). 같은 eventId로 다시 보내요\n", status);  // 408·429·5xx·연결 실패(음수)
  }
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
    secure.setHandshakeTimeout(5);    // https 인사(TLS)가 5초 넘게 멈추면 포기 (기본 120초 동안 센서를 못 읽음)
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
  if (!eventToJson(e, net::epochNow(), nowMs, json, sizeof(json))) {  // 시계가 맞춰질 때까지 기다림
    static unsigned long lastClockLogMs = 0;
    if (lastClockLogMs == 0 || nowMs - lastClockLogMs >= 60000UL) {  // 1분에 한 번만 찍는다
      lastClockLogMs = nowMs ? nowMs : 1;
      Serial.printf("[전송] 인터넷 시계(NTP)를 기다리는 중이라 못 보내요 (%u개 대기). 와이파이가 UDP 123을 막는지 확인하세요\n", (unsigned)count);
    }
    return;
  }

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
