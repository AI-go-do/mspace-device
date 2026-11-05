// 방 장치 설정값. 방·배선·주기가 바뀌면 이 파일만 고친다.
// 비밀 값(와이파이 비밀번호, 장치 토큰, 서버 주소)은 여기가 아니라 secrets.h에 둔다 (GitHub에 안 올라감).
#pragma once

// ---------- 이 장치가 맡은 방 ----------
// 방마다 장치를 복제할 때 이 두 줄만 바꾼다. 장치 토큰에 등록된 방·장치와 같아야 한다 (다르면 core가 403)
#define ROOM_ID    2
#define DEVICE_ID  "sensor-2"   // 규칙: sensor-방번호

// ---------- 어떤 센서를 달았나 ----------
// 달린 센서만 1로 둔다. 재실(occupied)은 켜진 센서 중 하나라도 "있음"이면 있음 (FR-DEV-103)
#define USE_RADAR  1   // LD2410C 레이더. 가만히 있는 사람도 감지
#define USE_PIR    0   // PIR(SR501). 움직임만 감지. 레이더와 같이 달면 정확도 비교(이슈 #13)에 쓴다
// 나중에 추가할 것: CO2(SCD40, FR-DEV-104), 키패드 체크인(FR-ACC-001). 자리는 README "ESP32 확장"에 적어 둠

// ---------- 핀 ----------
#define PIN_RADAR  27  // 레이더 OUT. 3.3V 신호라 바로 꽂는다. 사람이 있으면 HIGH
#define PIN_PIR    26  // PIR OUT (USE_PIR 1일 때). SR501 OUT도 3.3V 신호

// ---------- 주기 (docs/sensor-events-api.md 2장과 맞춤) ----------
#define OCCUPANCY_BEAT_MS  10000UL  // 바뀌지 않아도 10초마다 보낸다. core는 1분(6번) 안 오면 끊김으로 보고 "있음" 처리
#define SENSOR_READ_MS     50UL     // 센서 확인 간격

// ---------- 전송 ----------
#define DRY_RUN            1        // 1이면 서버에 안 보내고 시리얼 모니터에 보낼 내용만 찍는다. 서버가 준비되면 0
#define EVENT_PATH         "/api/sensor-events"  // core 주소 뒤에 붙는 경로. 지영님과 정하면 여기만 바꾼다
#define QUEUE_SIZE         32       // 와이파이가 끊긴 동안 모아 둘 이벤트 수. 넘치면 가장 오래된 것부터 버린다
#define CONNECT_TIMEOUT_MS 1000     // 서버 연결 기다리는 시간
#define RESPONSE_TIMEOUT_MS 2000    // 연결 뒤 답 기다리는 시간
#define RETRY_MIN_MS       1000UL   // 실패하면 1초 뒤 다시. 실패할 때마다 2배씩 늘림
#define RETRY_MAX_MS       30000UL  // 최대 30초 간격

// ---------- 시계 ----------
#define KST_OFFSET_SEC  (9 * 3600)  // 한국 시각 = UTC + 9시간
#define NTP_SERVER_1    "pool.ntp.org"
#define NTP_SERVER_2    "time.google.com"
