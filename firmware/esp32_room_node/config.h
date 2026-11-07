// 방 장치 설정값. 방·배선·주기가 바뀌면 이 파일만 고친다.
// 비밀 값(와이파이 비밀번호, 장치 토큰, 서버 주소)은 여기가 아니라 secrets.h에 둔다 (GitHub에 안 올라감).
#pragma once

// ---------- 이 장치가 맡은 방 ----------
// 방마다 장치를 복제할 때 이 두 줄만 바꾼다.
// 지금 core는 장치 토큰이 하나뿐이라 방·장치를 토큰으로 확인하지 않는다.
// 장치마다 토큰을 묶는 방식(다르면 403)은 지영님·재웅님과 정하는 중 (PR #24 리뷰)
#define ROOM_ID    2
#define DEVICE_ID  "sensor-2"   // 규칙: sensor-방번호

// ---------- 어떤 센서를 달았나 ----------
// 달린 센서만 1로 둔다. 재실(occupied)은 켜진 센서 중 하나라도 "있음"이면 있음 (FR-DEV-103)
#define USE_RADAR  1   // LD2410C 레이더. 가만히 있는 사람도 감지
#define USE_PIR    0   // PIR(SR501). 움직임만 감지. 레이더와 같이 달면 정확도 비교(이슈 #13)에 쓴다
#if !USE_RADAR && !USE_PIR
#error "USE_RADAR 또는 USE_PIR 중 하나는 1이어야 해요 (둘 다 0이면 늘 \"없음\"을 보내서 자동 퇴실될 수 있음)"
#endif
// 키패드 체크인 (FR-ACC-001). 4x4 키패드 + LED 2개. 번호 확인은 core가 한다.
// 2번 방에서 "두 방이 동시에 돌아간다"를 시험할 때만 1. 0이면 키패드 코드가 통째로 빠진다 (Keypad 라이브러리도 필요 없음)
#define USE_KEYPAD 1
// 나중에 추가할 것: CO2(SCD40, FR-DEV-104). 자리는 README "ESP32 확장"에 적어 둠

// ---------- 핀 ----------
#define PIN_RADAR  27  // 레이더 OUT. 3.3V 신호라 바로 꽂는다. 사람이 있으면 HIGH
#define PIN_PIR    26  // PIR OUT (USE_PIR 1일 때). SR501 OUT도 3.3V 신호
// 키패드 (USE_KEYPAD 1일 때). 행은 입력(내부 풀업)이라 입력 전용 핀(34~39)은 못 쓴다.
// 부팅에 쓰이는 핀(0·2·12·15)과 CO2 센서 자리(I2C 21·22)는 피했다
#define KEYPAD_ROW_PINS  {32, 33, 25, 14}  // 키패드 R1~R4. 숫자가 다르게 찍히면 선 대신 이 순서만 바꾼다
#define KEYPAD_COL_PINS  {13, 23, 19, 18}  // 키패드 C1~C4
#define PIN_LED_OK  16  // 초록 LED (열림). 16 -> 220Ω -> LED -> GND
#define PIN_LED_NO  17  // 빨강 LED (거부). 17 -> 220Ω -> LED -> GND

// ---------- 주기 (docs/sensor-events-api.md 2장과 맞춤) ----------
#define OCCUPANCY_BEAT_MS  10000UL  // 바뀌지 않아도 10초마다 보낸다. core는 1분(6번) 안 오면 끊김으로 보고 "있음" 처리
#define SENSOR_READ_MS     50UL     // 센서 확인 간격
#define PIR_HOLD_MS        120000UL // PIR은 마지막 움직임 뒤 2분간 "있음" (가만히 있는 사람을 못 봐서). 레이더는 그대로

// ---------- 전송 ----------
#define DRY_RUN            1        // 1이면 서버에 안 보내고 시리얼 모니터에 보낼 내용만 찍는다. 서버가 준비되면 0
#define EVENT_PATH         "/api/sensor-events"  // core 주소 뒤에 붙는 경로. 지영님과 정하면 여기만 바꾼다
#define QUEUE_SIZE         32       // 와이파이가 끊긴 동안 모아 둘 이벤트 수. 넘치면 가장 오래된 것부터 버린다
#define CONNECT_TIMEOUT_MS 1000     // 서버 연결 기다리는 시간
#define RESPONSE_TIMEOUT_MS 2000    // 연결 뒤 답 기다리는 시간
#define RETRY_MIN_MS       1000UL   // 실패하면 1초 뒤 다시. 실패할 때마다 2배씩 늘림
#define RETRY_MAX_MS       30000UL  // 최대 30초 간격

// ---------- 키패드 (우노와 같은 동작) ----------
#define ACCESS_PATH     "/api/access/verify"  // 번호 확인 주소 (docs/access-api.md 3장)
#define ACCESS_TRIES    2        // 서버 오류·연결 실패면 같은 eventId로 한 번 더 (게이트웨이와 같음)
#define ACCESS_RETRY_MS 1000UL   // 다시 보내기 전에 1초 쉰다
#define LED_MS          1500UL   // 결과 LED를 켜 두는 시간
#define BLINK_MS        200UL    // 서버 답을 기다리는 동안 초록 LED 깜빡임 간격

// ---------- 시계 ----------
#define KST_OFFSET_SEC  (9 * 3600)  // 한국 시각 = UTC + 9시간
#define NTP_SERVER_1    "pool.ntp.org"
#define NTP_SERVER_2    "time.google.com"
