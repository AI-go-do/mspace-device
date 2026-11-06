#include "config.h"
#if USE_KEYPAD
#include "keypad_door.h"
#include <Keypad.h>
#include <Ticker.h>
#include "access.h"

namespace door {

static const byte ROWS = 4, COLS = 4;
static char keys[ROWS][COLS] = {
  {'1', '2', '3', 'A'},
  {'4', '5', '6', 'B'},
  {'7', '8', '9', 'C'},
  {'*', '0', '#', 'D'},
};
static byte rowPins[ROWS] = KEYPAD_ROW_PINS;
static byte colPins[COLS] = KEYPAD_COL_PINS;
static Keypad keypad(makeKeymap(keys), rowPins, colPins, ROWS, COLS);

static String code = "";           // 지금까지 누른 숫자
static int ledPin = -1;            // 켜져 있는 결과 LED. 꺼져 있으면 -1
static unsigned long ledOnAt = 0;  // 결과 LED를 켠 시각 (millis)
static Ticker blinker;             // "확인 중" 깜빡임. 서버 답을 기다리는 동안에도 뒤에서 돈다

static void toggleOk() { digitalWrite(PIN_LED_OK, !digitalRead(PIN_LED_OK)); }

static void ledOn(int pin) {
  if (ledPin >= 0) digitalWrite(ledPin, LOW);  // 다른 LED가 켜져 있으면 끄고 새로 켠다
  digitalWrite(pin, HIGH);
  ledPin = pin;
  ledOnAt = millis();
}

void begin() {
  pinMode(PIN_LED_OK, OUTPUT);
  pinMode(PIN_LED_NO, OUTPUT);
  digitalWrite(PIN_LED_OK, LOW);
  digitalWrite(PIN_LED_NO, LOW);
  Serial.printf("[키패드] 준비: %d번 방, 6자리 + #\n", ROOM_ID);
}

// 서버에 번호를 확인하고 결과 LED를 켠다
static void check(const String& c) {
  if (ledPin >= 0) { digitalWrite(ledPin, LOW); ledPin = -1; }
  blinker.attach_ms(BLINK_MS, toggleOk);  // 깜빡임 시작
  access::Result r = access::verify(c);   // 답이 올 때까지 기다린다 (그동안 깜빡임은 계속)
  blinker.detach();
  digitalWrite(PIN_LED_OK, LOW);
  if (r.opened) {
    Serial.println("[키패드] 열림");
    ledOn(PIN_LED_OK);
  } else {
    Serial.printf("[키패드] 거부 (%s)\n", r.reason.c_str());
    ledOn(PIN_LED_NO);
  }
}

void update(unsigned long nowMs) {
  // 결과 LED는 1.5초 뒤 끈다. 빼서 비교하므로 49일 뒤 millis()가 0으로 돌아가도 맞다
  if (ledPin >= 0 && nowMs - ledOnAt >= LED_MS) {
    digitalWrite(ledPin, LOW);
    ledPin = -1;
  }
  char k = keypad.getKey();  // 방금 눌린 키. 안 눌렸으면 0
  if (!k) return;
  if (k == '#') {
    if (code.length() == 6) check(code);  // 6자리일 때만 서버에 확인
    else ledOn(PIN_LED_NO);               // 6자리가 아니면 빨강
    code = "";
  } else if (k == '*') {
    code = "";                                             // 처음부터 다시
  } else if (k >= '0' && k <= '9' && code.length() < 6) {
    code += k;                                             // 숫자만, 최대 6자리 (A~D는 무시)
  }
}

}  // namespace door
#endif  // USE_KEYPAD
