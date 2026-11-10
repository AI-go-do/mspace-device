"""센서 어댑터의 공통 모양 (FR-DEV-101, 10차 회의 "센서도 공통 인터페이스로").

센서마다 들어오는 모양이 다르다 (우노 시리얼 한 줄, 교육장·가짜 MQTT JSON, 방 카메라 YOLO 결과).
어댑터는 그걸 **core 센서 이벤트 하나의 모양**으로 바꿔 `push(body)` 한 곳으로만 내보낸다.
뒤쪽(SensorSender → POST /api/sensor-events)은 어떤 센서였는지 모른다. 그래서

- 가짜 센서 ↔ 실습실 센서 ↔ 실제 방 센서를 설정만 바꿔 끼운다 (core 코드는 그대로)
- 새 센서는 어댑터 하나만 더 만들고 ADAPTERS 표에 한 줄 적는다. 나가는 모양은 check_event로 맞춘다

이 파일은 동작을 바꾸지 않는다. 지금 있는 어댑터가 지켜야 할 모양을 이름으로 적어 두고,
시험(tests/test_sensor_adapters.py)이 세 어댑터가 정말 이 모양으로 내보내는지 확인한다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal, TypedDict, Union

Push = Callable[[dict], None]
"""어댑터가 이벤트를 내보내는 한 곳. 실제로는 SensorSender.push, DRY_RUN이면 화면에 찍는 함수."""


class OccupancyEvent(TypedDict):
    """재실 이벤트 (docs api 2-1). 우노 PIR·방 카메라가 보낸다."""
    eventId: str
    roomId: int
    deviceId: str
    kind: Literal["OCCUPANCY"]
    occupied: bool
    sources: dict          # 무엇으로 판단했나 (pir·light·camera·people). core는 기록만 한다
    observedAt: str


class _Co2Optional(TypedDict, total=False):
    temperatureC: float    # 온도·습도는 센서가 안 보냈으면 빠진다
    humidityPct: float


class Co2Event(_Co2Optional):
    """공기 이벤트 (docs api 6-1). 교육장·가짜 MQTT 센서가 보낸다."""
    eventId: str
    roomId: int
    deviceId: str
    kind: Literal["CO2"]
    ppm: int
    source: str
    observedAt: str


SensorEvent = Union[OccupancyEvent, Co2Event]  # 젯슨 파이썬이 3.10 아래여도 되게 Union

REQUIRED = {
    "OCCUPANCY": {"eventId": str, "roomId": int, "deviceId": str, "occupied": bool, "sources": dict,
                  "observedAt": str},
    "CO2": {"eventId": str, "roomId": int, "deviceId": str, "ppm": int, "source": str, "observedAt": str},
}
OPTIONAL = {"CO2": {"temperatureC": (int, float), "humidityPct": (int, float)}}


def check_event(body):
    """어댑터가 내보낸 이벤트가 공통 모양인지 본다. 문제를 글로 모아 돌려준다 (비면 통과)."""
    if not isinstance(body, dict):
        return ["dict가 아님"]
    kind = body.get("kind")
    if kind not in REQUIRED:
        return [f"모르는 kind: {kind!r}"]
    problems = []
    for key, typ in REQUIRED[kind].items():
        value = body.get(key)
        # bool은 int의 한 종류라서 roomId·ppm에 True가 들어오지 않게 따로 막는다
        if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
            problems.append(f"{key}: {typ.__name__} 아님 ({value!r})")
    for key, typ in OPTIONAL.get(kind, {}).items():
        if key in body and (not isinstance(body[key], typ) or isinstance(body[key], bool)):
            problems.append(f"{key}: 숫자 아님 ({body[key]!r})")
    extra = set(body) - set(REQUIRED[kind]) - set(OPTIONAL.get(kind, {})) - {"kind"}
    if extra:
        problems.append("모르는 키: " + ", ".join(sorted(extra)))
    if isinstance(body.get("eventId"), str):
        try:
            uuid.UUID(body["eventId"])
        except ValueError:
            problems.append("eventId: UUID 아님")
    if isinstance(body.get("observedAt"), str):
        try:
            if datetime.fromisoformat(body["observedAt"]).tzinfo is None:
                problems.append("observedAt: 시간대 없음")
        except ValueError:
            problems.append("observedAt: ISO 시각 아님")
    return problems


@dataclass(frozen=True)
class AdapterInfo:
    """어댑터 하나: 무엇을 받아(source) 어디서(module.name) 어떤 이벤트(kind)로 바꾸나."""
    source: str
    module: str
    name: str
    kind: str


# 지금 있는 센서 어댑터. 새 센서를 붙이면 여기에 한 줄 추가한다 (시험이 이름이 진짜 있는지 확인)
ADAPTERS = (
    AdapterInfo("우노 PIR·조도 (USB 시리얼 한 줄)", "gateway", "occupancy_body", "OCCUPANCY"),
    AdapterInfo("방 안 카메라 인원 (YOLO)", "room_camera", "RoomWatcher.event", "OCCUPANCY"),
    AdapterInfo("교육장·가짜 MQTT 센서 (ChirpStack 업링크)", "room_env", "RoomEnv.tick", "CO2"),
)
