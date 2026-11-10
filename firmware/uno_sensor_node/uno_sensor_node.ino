// M-SPACE 방 장치 (우노용) - 재실 센서 (FR-DEV-101) + 입실 결과 LED
// PIR·조도 값은 바뀔 때와 10초마다 JSON 한 줄로 USB 시리얼에 출력한다. PIR은 마지막 움직임 뒤 2분간 "있음"을 유지한다.
// 중계(노트북·젯슨)가 문 앞 QR 입실 결과를 OK/NO 한 줄로 보내면 LED를 켠다 (판단은 서버만 한다).
// 키패드 체크인은 11/10에 뺐다 (QR 입실만 씀). 그래서 Keypad 라이브러리도 필요 없다.
//
// 배선
//   LED 초록    : D12 -> 220Ω -> LED -> GND
//   LED 빨강    : D13 -> 220Ω -> LED -> GND
//   PIR(SR501)  : VCC -> 5V, GND -> GND, OUT -> D2
//   조도(CDS)   : 5V -> CDS -> A0 -> 10kΩ -> GND  (CDS와 10kΩ 사이를 A0에)
// (레이더는 ESP32 쪽에 단다: firmware/esp32_room_node)

#include <EEPROM.h>

// ---------- 설정값 (방·배선이 바뀌면 여기만 고친다) ----------
const int ROOM_ID = 1;        // 이 장치가 달린 방 번호. 서버의 방 id와 같아야 한다. 방마다 복제할 때 이것만 바꾼다
const int PIN_LED_OK = 12;    // 초록 LED 핀. 입실이 열리면 켠다
const int PIN_LED_NO = 13;    // 빨강 LED 핀. 거부되면 켠다
const unsigned long LED_MS = 1500;  // LED를 켜 두는 시간 (ms)
const int PIN_PIR = 2;        // PIR OUT 핀. 움직임이 있으면 1(HIGH), 없으면 0
const int PIN_CDS = A0;       // 조도 센서 핀. 0~1023, 밝을수록 큼 (위 배선 기준)
const unsigned long SENSOR_BEAT_MS = 10000; // 값이 그대로여도 10초마다 보낸다. core가 "센서가 살아 있다"를 알 수 있게 (1분 안 오면 끊김)
const int LIGHT_DELTA = 50;   // 조도가 50 이상 바뀌면 바로 보낸다. 작은 흔들림마다 보내지 않으려고
const unsigned long PIR_HOLD_MS = 120000; // 마지막 움직임 뒤 2분은 "있음"으로 본다. PIR은 가만히 있는 사람을 못 봐서 (FR-DEV-103 "헷갈리면 있음")

// ---------- 실행 중에 바뀌는 값 ----------
unsigned long bootId;      // 부팅 횟수. 켤 때마다 1씩 늘려 EEPROM에 저장한다. 재부팅으로 seq가 0부터 다시 시작해도 eventId가 겹치지 않게
unsigned long seq = 0;     // 이벤트 순서 번호. 보낼 때마다 1씩 늘어난다
int ledPin = -1;           // 지금 켜져 있는 LED 핀. 꺼져 있으면 -1
unsigned long ledOnAt = 0; // LED를 켠 시각 (millis 기준)
int lastPir = -1;          // 마지막으로 보낸 PIR 값. 처음엔 -1이라 첫 값은 무조건 보낸다
int lastLight = -1000;     // 마지막으로 보낸 조도 값. 처음엔 차이가 커서 무조건 보낸다
unsigned long lastSensorAt = 0; // 마지막으로 센서 값을 보낸 시각 (millis 기준)
unsigned long lastMotionAt = 0; // PIR이 마지막으로 움직임(HIGH)을 본 시각 (millis 기준)
bool motionSeen = false;        // 켜진 뒤 움직임을 한 번이라도 봤는지 (처음부터 2분간 "있음"이 되지 않게)

// 재실 센서 한 줄 출력
// 예: {"eventId":"1-48213-19","roomId":1,"sensorType":"occupancy","pir":1,"light":512,"ms":13000}
//   게이트웨이가 core 형식(kind OCCUPANCY, occupied, sources, observedAt)으로 바꾼다
void sendSensors(int pir, int light) {
  seq++;
  Serial.print("{\"eventId\":\"");
  Serial.print(ROOM_ID); Serial.print('-'); Serial.print(bootId); Serial.print('-'); Serial.print(seq);
  Serial.print("\",\"roomId\":"); Serial.print(ROOM_ID);
  Serial.print(",\"sensorType\":\"occupancy\",\"pir\":"); Serial.print(pir);
  Serial.print(",\"light\":"); Serial.print(light);
  Serial.print(",\"ms\":"); Serial.print(millis());
  Serial.println("}");
}

// loop()마다 불러서 센서를 읽는다. PIR이 바뀌었거나, 조도가 크게 바뀌었거나, 10초가 지났으면 보낸다
// pir 값은 "PIR이 지금 HIGH이거나, 마지막 움직임 뒤 2분이 안 지났으면 1"이다
void updateSensors() {
  if (digitalRead(PIN_PIR) == HIGH) {
    lastMotionAt = millis();
    motionSeen = true;
  }
  if (motionSeen && millis() - lastMotionAt >= PIR_HOLD_MS) motionSeen = false;  // 유지 시간이 끝나면 잊는다 (49일 뒤 millis 되감김 대비)
  int pir = motionSeen ? 1 : 0;
  int light = analogRead(PIN_CDS);
  bool beat = lastPir < 0 || millis() - lastSensorAt >= SENSOR_BEAT_MS;
  if (pir != lastPir || abs(light - lastLight) >= LIGHT_DELTA || beat) {
    sendSensors(pir, light);
    lastPir = pir;
    lastLight = light;
    lastSensorAt = millis();
  }
}

// LED를 켜고 켠 시각만 기억한다. delay()로 멈추지 않아서 그동안에도 센서를 계속 읽는다
void ledOn(int pin) {
  if (ledPin >= 0) digitalWrite(ledPin, LOW);  // 다른 LED가 켜져 있으면 끄고 새로 켠다
  digitalWrite(pin, HIGH);
  ledPin = pin;
  ledOnAt = millis();
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
  Serial.setTimeout(50);        // 응답 한 줄을 기다리는 시간. 길면 그동안 센서를 못 읽는다 (기본 1초)
  pinMode(PIN_LED_OK, OUTPUT);  // LED 핀은 쓰기
  pinMode(PIN_LED_NO, OUTPUT);
  pinMode(PIN_PIR, INPUT);      // 센서 핀은 읽기
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
  updateSensors();  // PIR·조도가 바뀌었거나 10초 지났으면 보낸다

  // 중계 응답 (문 앞 QR 입실 결과): "OK" 또는 "NO" 한 줄
  if (Serial.available()) {
    String reply = Serial.readStringUntil('\n');  // 중계가 보낸 한 줄
    reply.trim();                                 // 앞뒤 공백·줄바꿈 제거
    if (reply == "OK") ledOn(PIN_LED_OK);
    else if (reply == "NO") ledOn(PIN_LED_NO);
  }
}
