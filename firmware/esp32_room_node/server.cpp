#include "server.h"
#include <HTTPClient.h>
#include <WiFiClient.h>
#include <WiFiClientSecure.h>
#include "config.h"
#include "secrets_or_default.h"

namespace server {

static Mode current = MODE_DRY_RUN;

Mode mode() { return current; }
bool canSend() { return current == MODE_HTTPS || current == MODE_HTTP_LOCAL; }

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

void begin() {
  if (DRY_RUN) {
    Serial.println("[서버] DRY_RUN: 서버에 안 보내고 화면에만 찍어요 (키패드는 123456만 열림)");
    current = MODE_DRY_RUN;
    return;
  }
  String url = SERVER_URL;
  current = MODE_BLOCKED;  // 아래 조건을 통과해야 보낸다
  if (strlen(DEVICE_TOKEN) == 0) {
    Serial.println("[서버] 막힘: secrets.h에 DEVICE_TOKEN이 없어요");
  } else if (url.startsWith("https://")) {
    if (strlen(SERVER_CA_PEM) == 0) {
      Serial.println("[서버] 막힘: https 인증서(SERVER_CA_PEM)가 없어요. 확인 없이 보내면 가짜 서버에 토큰이 갈 수 있어요");
    } else {
      current = MODE_HTTPS;
    }
  } else if (url.startsWith("http://")) {
    if (ALLOW_HTTP_FOR_LOCAL_TEST && !isLocalHost(url)) {
      Serial.println("[서버] 막힘: http 시험은 같은 와이파이의 사설 주소(192.168.x.x 등)나 .local만 돼요");
    } else if (ALLOW_HTTP_FOR_LOCAL_TEST) {
      Serial.println("[서버] 주의: http로 보내요 (맥 로컬 시험용). 시험이 끝나면 ALLOW_HTTP_FOR_LOCAL_TEST 0으로");
      current = MODE_HTTP_LOCAL;
    } else {
      Serial.println("[서버] 막힘: 원격 서버는 https://로만 보내요 (장치 토큰 보호)");
    }
  } else {
    Serial.println("[서버] 막힘: SERVER_URL이 비었거나 http(s)://로 시작하지 않아요");
  }
  if (current == MODE_BLOCKED) {
    Serial.println("[서버] 재실 이벤트는 큐에 모으기만 하고, 키패드는 모두 거부해요. secrets.h를 고치고 다시 올려 주세요");
  }
}

int post(const char* path, const char* json, String* response) {
  if (!canSend()) return -1;
  String url = String(SERVER_URL) + path;
  HTTPClient http;
  WiFiClientSecure secure;
  WiFiClient plain;
  bool ok;
  if (current == MODE_HTTPS) {
    secure.setCACert(SERVER_CA_PEM);  // 서버 인증서를 이 루트 인증서로 확인한다
    secure.setHandshakeTimeout(5);    // https 인사(TLS)가 5초 넘게 멈추면 포기 (기본 120초 동안 센서를 못 읽음)
    ok = http.begin(secure, url);
  } else {
    ok = http.begin(plain, url);
  }
  if (!ok) return -1;
  http.setConnectTimeout(CONNECT_TIMEOUT_MS);
  http.setTimeout(RESPONSE_TIMEOUT_MS);
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);  // 리디렉션은 따라가지 않는다 (토큰이 다른 곳으로 가지 않게)
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);
  int status = http.POST((uint8_t*)json, strlen(json));
  if (response && status > 0) *response = http.getString();
  http.end();
  return status;  // 음수면 연결 실패
}

}  // namespace server
