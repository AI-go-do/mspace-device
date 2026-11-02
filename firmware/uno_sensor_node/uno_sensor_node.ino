// M-SPACE 방 센서 노드 (우노용)
// 센서 값을 JSON 한 줄로 USB 시리얼에 출력하고, 중계(노트북·젯슨)에서 오는 OK/NO 응답으로 LED를 켠다.
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

// ---------- 설정값 (방·배선이 바뀌면 여기만 고친다) ----------
const int ROOM_ID = 1;        // 이 장치가 달린 방 번호. 서버의 방 id와 같아야 한다. 방마다 복제할 때 이것만 바꾼다
const int PIN_PIR = 2;        // PIR 센서 OUT 핀. 움직임이 있으면 1(HIGH), 없으면 0
const int PIN_RADAR = 3;      // 레이더 OUT 핀. 가만히 있는 사람도 감지하면 1 (부품 도착 후 사용)
const int PIN_CDS = A0;       // 조도 센서(CDS) 핀. 0~1023 숫자로 밝기를 읽는다
const int PIN_LED_OK = 12;    // 초록 LED 핀. 번호가 맞으면 켠다
const int PIN_LED_NO = 13;    // 빨강 LED 핀. 번호가 틀리면 켠다
const bool USE_RADAR = false; // 레이더를 연결했는지. 연결 전에는 false라서 레이더 값을 보내지 않는다

const unsigned long HEARTBEAT_MS = 10000; // 값이 그대로여도 10초(10000ms)마다 현재 값을 보낸다. 서버가 "장치가 살아 있다"를 알 수 있게
const int LIGHT_DELTA = 50;               // 조도 값이 50 이상 바뀌면 바로 보낸다. 작은 흔들림마다 보내지 않으려고

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
unsigned long bootId;      // 켜질 때마다 새로 뽑는 랜덤 번호. 재부팅으로 seq가 0부터 다시 시작해도 eventId가 겹치지 않게
unsigned long seq = 0;     // 이벤트 순서 번호. 보낼 때마다 1씩 늘어난다
int lastPir = -1;          // 마지막으로 보낸 PIR 값. 처음엔 -1이라 첫 값은 무조건 보낸다
int lastRadar = -1;        // 마지막으로 보낸 레이더 값 (위와 같은 이유로 -1에서 시작)
int lastLight = -1000;     // 마지막으로 보낸 조도 값. -1000이라 첫 값은 무조건 차이가 커서 보낸다
unsigned long lastBeat = 0; // 마지막으로 10초 주기 전송을 한 시각 (켜진 뒤 ms)
String code = "";          // 지금까지 누른 숫자. 6자리 + #이면 서버로 보내고 비운다

// 센서 이벤트 한 줄 출력
// 예: {"eventId":"1-48213-17","roomId":1,"sensorType":"pir","value":1,"ms":12345}
//   eventId = 방번호-bootId-seq  (서버가 이 값으로 중복을 거른다)
//   ms      = 우노가 켜진 뒤 지난 시간. 우노엔 시계가 없어서 실제 시각은 중계가 receivedAt으로 붙인다
void sendEvent(const char* sensorType, long value) {
  seq++;
  Serial.print("{\"eventId\":\"");
  Serial.print(ROOM_ID); Serial.print('-'); Serial.print(bootId); Serial.print('-'); Serial.print(seq);
  Serial.print("\",\"roomId\":"); Serial.print(ROOM_ID);
  Serial.print(",\"sensorType\":\""); Serial.print(sensorType);
  Serial.print("\",\"value\":"); Serial.print(value);
  Serial.print(",\"ms\":"); Serial.print(millis());
  Serial.println("}");
}

// 키패드 번호 한 줄 출력. 번호가 맞는지는 우노가 아니라 서버가 판단한다 (장치는 번호를 저장하지 않음)
void sendCode(const String& c) {
  seq++;
  Serial.print("{\"eventId\":\"");
  Serial.print(ROOM_ID); Serial.print('-'); Serial.print(bootId); Serial.print('-'); Serial.print(seq);
  Serial.print("\",\"roomId\":"); Serial.print(ROOM_ID);
  Serial.print(",\"sensorType\":\"keypad\",\"code\":\""); Serial.print(c);
  Serial.print("\",\"ms\":"); Serial.print(millis());
  Serial.println("}");
}

// LED를 1.5초 켰다 끈다
void blink(int pin) {
  digitalWrite(pin, HIGH);
  delay(1500);
  digitalWrite(pin, LOW);
}

void setup() {
  Serial.begin(115200);         // 중계와 통신 속도. gateway.py의 115200과 같아야 한다
  pinMode(PIN_PIR, INPUT);      // 센서 핀은 읽기
  pinMode(PIN_RADAR, INPUT);
  pinMode(PIN_LED_OK, OUTPUT);  // LED 핀은 쓰기
  pinMode(PIN_LED_NO, OUTPUT);
  randomSeed(analogRead(A5));   // 아무것도 안 꽂힌 A5의 잡음 값으로 난수 시작점을 정한다 (매번 다른 bootId)
  bootId = random(10000, 99999);
}

void loop() {
  unsigned long now = millis();               // 켜진 뒤 지난 시간(ms)
  bool beat = now - lastBeat >= HEARTBEAT_MS; // 10초가 지났으면 true -> 이번엔 값이 그대로여도 보낸다

  int pir = digitalRead(PIN_PIR);  // 지금 PIR 값 (0 또는 1)
  if (pir != lastPir || beat) { sendEvent("pir", pir); lastPir = pir; }  // 바뀌었거나 10초 됐을 때만 전송

  if (USE_RADAR) {
    int radar = digitalRead(PIN_RADAR);  // 지금 레이더 값 (0 또는 1)
    if (radar != lastRadar || beat) { sendEvent("radar", radar); lastRadar = radar; }
  }

  int light = analogRead(PIN_CDS);  // 지금 조도 값. 0~1023, 밝을수록 큼 (배선 기준)
  if (abs(light - lastLight) >= LIGHT_DELTA || beat) { sendEvent("light", light); lastLight = light; }

  if (beat) lastBeat = now;

  // 키패드: 숫자 6자리 입력 후 #으로 전송, *로 지우기 (FR-ACC-001)
  char k = keypad.getKey();  // 방금 눌린 키. 안 눌렸으면 0
  if (k) {
    if (k == '#') { if (code.length() == 6) sendCode(code); code = ""; }  // 6자리일 때만 보내고, 아니면 버린다
    else if (k == '*') { code = ""; }                                     // 처음부터 다시 입력
    else if (k >= '0' && k <= '9' && code.length() < 6) { code += k; }   // 숫자만, 최대 6자리까지 모은다 (A~D는 무시)
  }

  // 중계 응답: "OK" 또는 "NO" 한 줄
  if (Serial.available()) {
    String reply = Serial.readStringUntil('\n');  // 중계가 보낸 한 줄
    reply.trim();                                 // 앞뒤 공백·줄바꿈 제거
    if (reply == "OK") blink(PIN_LED_OK);
    else if (reply == "NO") blink(PIN_LED_NO);
  }

  delay(50);  // 0.05초 쉬고 다시 반복 (너무 빠르게 돌지 않게)
}
