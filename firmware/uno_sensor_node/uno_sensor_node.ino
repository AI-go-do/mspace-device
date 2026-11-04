// M-SPACE 방 장치 (우노용) - 키패드 체크인 (FR-ACC-001)
// 키패드로 받은 6자리 번호를 JSON 한 줄로 USB 시리얼에 출력하고,
// 중계(노트북·젯슨)에서 오는 OK/NO 응답으로 LED를 켠다.
// 번호가 맞는지는 서버가 판단한다. 장치는 번호를 저장하지 않는다.
// 필요 라이브러리: Keypad (Arduino IDE > 라이브러리 관리 > "Keypad" by Mark Stanley)
//
// 배선
//   4x4 키패드  : 행 R1~R4 -> D11,D10,D9,D8 / 열 C1~C4 -> D7,D6,D5,D4
//                 (선에 R·C 라벨이 있으면 라벨 기준. 순서가 C4~C1, R1~R4인 키패드도 있다)
//   LED 초록    : D12 -> 220Ω -> LED -> GND
//   LED 빨강    : D13 -> 220Ω -> LED -> GND
// (재실 센서 PIR·조도·레이더는 다음 단계에서 추가: FR-DEV-101)

#include <Keypad.h>
#include <EEPROM.h>

// ---------- 설정값 (방·배선이 바뀌면 여기만 고친다) ----------
const int ROOM_ID = 1;        // 이 장치가 달린 방 번호. 서버의 방 id와 같아야 한다. 방마다 복제할 때 이것만 바꾼다
const int PIN_LED_OK = 12;    // 초록 LED 핀. 번호가 맞으면 켠다
const int PIN_LED_NO = 13;    // 빨강 LED 핀. 번호가 틀리면 켠다
const unsigned long LED_MS = 1500;  // LED를 켜 두는 시간 (ms)
const unsigned long BLINK_MS = 200;     // "확인 중" 깜빡임 간격 (ms). 서버 답을 기다리는 동안 초록 LED를 깜빡인다
const unsigned long WAIT_MAX_MS = 7000; // 답을 기다리는 최대 시간 (ms). 게이트웨이는 최대 약 5초 안에 답하므로 넉넉히 7초

// ---------- 키패드 ----------
const byte ROWS = 4, COLS = 4;  // 키패드 크기: 4행 4열
char keys[ROWS][COLS] = {       // 키패드에 실제로 적힌 글자 배치 (눌린 위치 -> 글자)
  {'1','2','3','A'},
  {'4','5','6','B'},
  {'7','8','9','C'},
  {'*','0','#','D'}
};
byte rowPins[ROWS] = {11, 10, 9, 8};  // 키패드 행1~4가 꽂힌 우노 핀. 숫자가 다르게 찍히면 이 순서만 바꾼다
byte colPins[COLS] = {7, 6, 5, 4};    // 키패드 열1~4가 꽂힌 우노 핀
Keypad keypad = Keypad(makeKeymap(keys), rowPins, colPins, ROWS, COLS);  // 위 설정으로 만든 키패드 객체

// ---------- 실행 중에 바뀌는 값 ----------
unsigned long bootId;      // 부팅 횟수. 켤 때마다 1씩 늘려 EEPROM에 저장한다. 재부팅으로 seq가 0부터 다시 시작해도 eventId가 겹치지 않게
unsigned long seq = 0;     // 이벤트 순서 번호. 보낼 때마다 1씩 늘어난다
String code = "";          // 지금까지 누른 숫자. 6자리 + #이면 서버로 보내고 비운다
int ledPin = -1;           // 지금 켜져 있는 LED 핀. 꺼져 있으면 -1
unsigned long ledOnAt = 0; // LED를 켠 시각 (millis 기준)
bool waiting = false;      // 서버 답을 기다리는 중인지. 기다리는 동안 초록 LED를 깜빡인다
unsigned long waitStart = 0; // 기다리기 시작한 시각 (millis 기준)

// 키패드 번호 한 줄 출력
// 예: {"eventId":"1-48213-18","roomId":1,"sensorType":"keypad","code":"123456","ms":12890}
//   eventId = 방번호-bootId-seq  (게이트웨이가 UUID로 바꿔 서버에 보내고, 서버가 이 값으로 중복을 거른다)
//   ms      = 우노가 켜진 뒤 지난 시간. 우노엔 시계가 없다
void sendCode(const String& c) {
  seq++;
  Serial.print("{\"eventId\":\"");
  Serial.print(ROOM_ID); Serial.print('-'); Serial.print(bootId); Serial.print('-'); Serial.print(seq);
  Serial.print("\",\"roomId\":"); Serial.print(ROOM_ID);
  Serial.print(",\"sensorType\":\"keypad\",\"code\":\""); Serial.print(c);
  Serial.print("\",\"ms\":"); Serial.print(millis());
  Serial.println("}");
}

// LED를 켜고 켠 시각만 기억한다. delay()로 멈추지 않아서 그동안에도 키 입력을 계속 받는다
void ledOn(int pin) {
  if (ledPin >= 0) digitalWrite(ledPin, LOW);  // 다른 LED가 켜져 있으면 끄고 새로 켠다
  digitalWrite(pin, HIGH);
  ledPin = pin;
  ledOnAt = millis();
}

// 번호를 보낸 직후 부른다. 켜져 있던 결과 LED를 끄고 "확인 중" 깜빡임을 시작한다
void startWaiting() {
  if (ledPin >= 0) digitalWrite(ledPin, LOW);
  ledPin = -1;
  waiting = true;
  waitStart = millis();
}

// 답이 오면(또는 너무 오래 걸리면) 깜빡임을 멈춘다
void stopWaiting() {
  waiting = false;
  digitalWrite(PIN_LED_OK, LOW);
}

// loop()마다 불러서 기다리는 중이면 초록 LED를 BLINK_MS마다 켰다 껐다 한다
// WAIT_MAX_MS가 지나도 답이 없으면(USB 빠짐, 게이트웨이 꺼짐) 빨강 LED로 거부를 알린다
void updateWaiting() {
  if (!waiting) return;
  unsigned long waited = millis() - waitStart;  // 빼서 비교하므로 millis()가 0으로 돌아가도 맞다
  if (waited >= WAIT_MAX_MS) {
    stopWaiting();
    ledOn(PIN_LED_NO);
    return;
  }
  digitalWrite(PIN_LED_OK, (waited / BLINK_MS) % 2 == 0 ? HIGH : LOW);  // 짝수 칸이면 켜고 홀수 칸이면 끈다
}

// loop()마다 불러서 LED_MS가 지났으면 끈다
// millis() - ledOnAt 으로 빼서 비교하면 약 49일 뒤 millis()가 0으로 돌아가도 맞게 계산된다
void updateLed() {
  if (ledPin >= 0 && millis() - ledOnAt >= LED_MS) {
    digitalWrite(ledPin, LOW);
    ledPin = -1;
  }
}

void setup() {
  Serial.begin(115200);         // 중계와 통신 속도. gateway.py의 115200과 같아야 한다
  Serial.setTimeout(50);        // 응답 한 줄을 기다리는 시간. 길면 그동안 키패드를 못 읽는다 (기본 1초)
  pinMode(PIN_LED_OK, OUTPUT);  // LED 핀은 쓰기
  pinMode(PIN_LED_NO, OUTPUT);
  EEPROM.get(0, bootId);        // 전원을 꺼도 남는 메모리(EEPROM)에서 지난 부팅 횟수를 읽는다
  if (bootId == 0xFFFFFFFF) {   // 처음 쓰는 보드는 EEPROM이 FF로 차 있다
    // 보드를 새로 바꿔도 예전 보드의 번호와 겹치지 않게 랜덤한 곳에서 시작한다
    randomSeed(analogRead(A5) ^ ((unsigned long)analogRead(A4) << 10) ^ micros());  // 안 꽂힌 핀의 잡음으로 시작점을 섞는다
    bootId = random(1, 1000000) * 1000UL;
  }
  bootId++;
  EEPROM.put(0, bootId);
}

void loop() {
  updateLed();      // 켜 둔 LED가 1.5초 지났으면 끈다 (기다리지 않고 확인만 한다)
  updateWaiting();  // 서버 답을 기다리는 중이면 초록 LED를 깜빡인다

  // 키패드: 숫자 6자리 입력 후 #으로 전송, *로 지우기
  char k = keypad.getKey();  // 방금 눌린 키. 안 눌렸으면 0
  if (k) {
    if (k == '#') {
      if (code.length() == 6) { sendCode(code); startWaiting(); }  // 6자리일 때만 서버에 확인하고, 답 올 때까지 깜빡인다
      else ledOn(PIN_LED_NO);                  // 6자리가 아니면 빨강 LED로 잘못 눌렀다고 알린다
      code = "";
    }
    else if (k == '*') { code = ""; }                                     // 처음부터 다시 입력
    else if (k >= '0' && k <= '9' && code.length() < 6) { code += k; }   // 숫자만, 최대 6자리까지 모은다 (A~D는 무시)
  }

  // 중계 응답: "OK" 또는 "NO" 한 줄
  if (Serial.available()) {
    String reply = Serial.readStringUntil('\n');  // 중계가 보낸 한 줄
    reply.trim();                                 // 앞뒤 공백·줄바꿈 제거
    if (reply == "OK") { stopWaiting(); ledOn(PIN_LED_OK); }
    else if (reply == "NO") { stopWaiting(); ledOn(PIN_LED_NO); }
  }
}
