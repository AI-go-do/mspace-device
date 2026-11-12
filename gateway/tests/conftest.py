import pytest

import room_camera


@pytest.fixture(autouse=True)
def person_seen_at_once(monkeypatch):
    """대부분의 테스트는 사람이 한 장만 보여도 들어온 것으로 본다 (ENTRY_PERSON_SEC 전의 규칙).
    잘못 본 사람을 거르는 테스트는 room_camera.ENTRY_PERSON_SEC를 다시 정한다."""
    monkeypatch.setattr(room_camera, "ENTRY_PERSON_SEC", 0)
