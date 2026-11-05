#include "net.h"
#include <WiFi.h>
#include "config.h"
#include "secrets_or_default.h"

namespace net {

static const unsigned long RECONNECT_MS = 10000;  // 끊겼을 때 10초마다 다시 붙어 본다
static unsigned long lastTryMs = 0;
static bool timeStarted = false;  // NTP 시계 맞추기를 시작했나
static bool wasOnline = false;

bool hasWifiConfig() { return strlen(WIFI_SSID) > 0; }

void begin() {
  if (!hasWifiConfig()) {
    Serial.println("[와이파이] secrets.h에 이름이 없어서 와이파이 없이 돌아요 (DRY_RUN 화면 확인만)");
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  Serial.print("[와이파이] 연결 시작: "); Serial.println(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  lastTryMs = millis();
}

void update(unsigned long nowMs) {
  if (!hasWifiConfig()) return;
  bool on = WiFi.status() == WL_CONNECTED;
  if (on && !wasOnline) {
    Serial.print("[와이파이] 연결됨, IP "); Serial.println(WiFi.localIP());
    if (!timeStarted) {
      configTime(0, 0, NTP_SERVER_1, NTP_SERVER_2);  // UTC로 맞추고, 한국 시각(+9)은 이벤트 만들 때 더한다
      timeStarted = true;
    }
  } else if (!on && wasOnline) {
    Serial.println("[와이파이] 끊김. 이벤트는 모아 뒀다가 다시 붙으면 보내요");
  }
  wasOnline = on;
  if (!on && nowMs - lastTryMs >= RECONNECT_MS) {  // 자동 재연결이 안 되면 직접 다시
    lastTryMs = nowMs;
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
}

bool online() { return hasWifiConfig() && WiFi.status() == WL_CONNECTED; }

time_t epochNow() { return time(nullptr); }

}  // namespace net
