// M-SPACE 방 장치 (우노용) - 키패드 체크인 (FR-ACC-001)
// 키패드로 받은 6자리 번호를 JSON 한 줄로 USB 시리얼에 출력하고,
// 중계(노트북·젯슨)에서 오는 OK/NO 응답으로 LED를 켠다.
// 번호가 맞는지는 서버가 판단한다. 장치는 번호를 저장하지 않는다.
// 필요 라이브러리: Keypad (Arduino IDE > 라이브러리 관리 > "Keypad" by Mark Stanley)
//
// 배선
//   4x4 키패드  : 왼쪽부터 행1~4 -> D11,D10,D9,D8 / 열1~4 -> D7,D6,D5,D4
//   LED 초록    : D12 -> 220Ω -> LED -> GND
//   LED 빨강    : D13 -> 220Ω -> LED -> GND
// (재실 센서 PIR·조도·레이더는 다음 단계에서 추가: FR-DEV-101)

#include <Keypad.h>

// ---------- 설정값 (방·배선이 바뀌면 여기만 고친다) ----------
const int ROOM_ID = 1;        // 이 장치가 달린 방 번호. 서버의 방 id와 같아야 한다. 방마다 복제할 때 이것만 바꾼다
const int PIN_LED_OK = 12;    // 초록 LED 핀. 번호가 맞으면 켠다
const int PIN_LED_NO = 13;    // 빨강 LED 핀. 번호가 틀리면 켠다

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
String code = "";          // 지금까지 누른 숫자. 6자리 + #이면 서버로 보내고 비운다

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

// LED를 1.5초 켰다 끈다
void blink(int pin) {
  digitalWrite(pin, HIGH);
  delay(1500);
  digitalWrite(pin, LOW);
}

void setup() {
  Serial.begin(115200);         // 중계와 통신 속도. gateway.py의 115200과 같아야 한다
  pinMode(PIN_LED_OK, OUTPUT);  // LED 핀은 쓰기
  pinMode(PIN_LED_NO, OUTPUT);
  randomSeed(analogRead(A5));   // 아무것도 안 꽂힌 A5의 잡음 값으로 난수 시작점을 정한다 (매번 다른 bootId)
  bootId = random(10000, 99999);
}

void loop() {
  // 키패드: 숫자 6자리 입력 후 #으로 전송, *로 지우기
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
}
