// M-SPACE 방 센서 노드 (우노용)
// 센서 값을 JSON 한 줄로 USB 시리얼에 출력하고, 중계(노트북·젠슨)에서 오는 OK/NO 응답으로 LED를 켠다.
// 필요 라이브러리: Keypad (Arduino IDE > 라이브러리 관리 > "Keypad" by Mark Stanley)
//
// 배선
//   PIR(SR501)  : VCC->5V, GND->GND, OUT->D2
//   레이더 OUT  : (도착 후) LD2410C OUT->D3, VCC->5V, GND->GND
//   CDS         : 5V -> CDS -> A0 -> 10kΩ -> GND  (CDS와 10kΩ 사이를 A0에)
//   4x4 키패드  : 왼쪽부터 행1~4 -> D11,D10,D9,D8 / 열1~4 -> D7,D6,D5,D4
//   LED 초록    : D12 -> 220Ω -> LED -> GND
//   LED 빨강    : D13 -> 220Ω -> LED -> GND

#include <Keypad.h>

const int ROOM_ID = 1;
const int PIN_PIR = 2;
const int PIN_RADAR = 3;
const int PIN_CDS = A0;
const int PIN_LED_OK = 12;
const int PIN_LED_NO = 13;
const bool USE_RADAR = false;          // 레이더 연결하면 true

const unsigned long HEARTBEAT_MS = 10000;  // 변화가 없어도 10초마다 현재 값 전송
const int LIGHT_DELTA = 50;                // 조도 값이 이만큼 바뀌면 바로 전송

const byte ROWS = 4, COLS = 4;
char keys[ROWS][COLS] = {
  {'1','2','3','A'},
  {'4','5','6','B'},
  {'7','8','9','C'},
  {'*','0','#','D'}
};
byte rowPins[ROWS] = {11, 10, 9, 8};
byte colPins[COLS] = {7, 6, 5, 4};
Keypad keypad = Keypad(makeKeymap(keys), rowPins, colPins, ROWS, COLS);

unsigned long bootId;      // 재부팅마다 바뀌는 값. eventId가 재부팅 후에도 겹치지 않게
unsigned long seq = 0;     // 이벤트 번호
int lastPir = -1, lastRadar = -1, lastLight = -1000;
unsigned long lastBeat = 0;
String code = "";

void sendEvent(const char* sensorType, long value) {
  seq++;
  // 예: {"eventId":"1-48213-17","roomId":1,"sensorType":"pir","value":1,"ms":12345}
  Serial.print("{\"eventId\":\"");
  Serial.print(ROOM_ID); Serial.print('-'); Serial.print(bootId); Serial.print('-'); Serial.print(seq);
  Serial.print("\",\"roomId\":"); Serial.print(ROOM_ID);
  Serial.print(",\"sensorType\":\""); Serial.print(sensorType);
  Serial.print("\",\"value\":"); Serial.print(value);
  Serial.print(",\"ms\":"); Serial.print(millis());
  Serial.println("}");
}

void sendCode(const String& c) {
  seq++;
  Serial.print("{\"eventId\":\"");
  Serial.print(ROOM_ID); Serial.print('-'); Serial.print(bootId); Serial.print('-'); Serial.print(seq);
  Serial.print("\",\"roomId\":"); Serial.print(ROOM_ID);
  Serial.print(",\"sensorType\":\"keypad\",\"code\":\""); Serial.print(c);
  Serial.print("\",\"ms\":"); Serial.print(millis());
  Serial.println("}");
}

void blink(int pin) {
  digitalWrite(pin, HIGH);
  delay(1500);
  digitalWrite(pin, LOW);
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_RADAR, INPUT);
  pinMode(PIN_LED_OK, OUTPUT);
  pinMode(PIN_LED_NO, OUTPUT);
  randomSeed(analogRead(A5));  // 연결 안 된 핀의 잡음으로 난수 시드
  bootId = random(10000, 99999);
}

void loop() {
  unsigned long now = millis();
  bool beat = now - lastBeat >= HEARTBEAT_MS;

  int pir = digitalRead(PIN_PIR);
  if (pir != lastPir || beat) { sendEvent("pir", pir); lastPir = pir; }

  if (USE_RADAR) {
    int radar = digitalRead(PIN_RADAR);
    if (radar != lastRadar || beat) { sendEvent("radar", radar); lastRadar = radar; }
  }

  int light = analogRead(PIN_CDS);  // 0~1023, 밝을수록 큼 (배선 기준)
  if (abs(light - lastLight) >= LIGHT_DELTA || beat) { sendEvent("light", light); lastLight = light; }

  if (beat) lastBeat = now;

  // 키패드: 숫자 입력 후 #으로 전송, *로 지우기
  char k = keypad.getKey();
  if (k) {
    if (k == '#') { if (code.length() == 6) sendCode(code); code = ""; }  // 번호는 6자리 (FR-ACC-001)
    else if (k == '*') { code = ""; }
    else if (k >= '0' && k <= '9' && code.length() < 6) { code += k; }
  }

  // 중계 응답: "OK" 또는 "NO" 한 줄
  if (Serial.available()) {
    String reply = Serial.readStringUntil('\n');
    reply.trim();
    if (reply == "OK") blink(PIN_LED_OK);
    else if (reply == "NO") blink(PIN_LED_NO);
  }

  delay(50);
}
