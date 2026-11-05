// secrets.h가 있으면 불러오고, 없으면 빈 값으로 채운다.
// 그래서 secrets.h 없이도 컴파일된다 (처음 받아서 DRY_RUN으로 확인할 때, CI에서).
#pragma once

#if __has_include("secrets.h")
#include "secrets.h"
#endif

#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASS
#define WIFI_PASS ""
#endif
#ifndef SERVER_URL
#define SERVER_URL ""
#endif
#ifndef DEVICE_TOKEN
#define DEVICE_TOKEN ""
#endif
#ifndef SERVER_CA_PEM
#define SERVER_CA_PEM ""
#endif
#ifndef ALLOW_HTTP_FOR_LOCAL_TEST
#define ALLOW_HTTP_FOR_LOCAL_TEST 0
#endif
