from __future__ import annotations

import pytest

from app.services.us_sector_korean_map import korean_sector_label


@pytest.mark.unit
def test_maps_known_industry_to_korean():
    assert korean_sector_label("Semiconductors") == "반도체"
    assert korean_sector_label("Banks - Regional") == "지방은행"


@pytest.mark.unit
def test_maps_known_sector_to_korean():
    assert korean_sector_label("Technology") == "기술"
    assert korean_sector_label("Healthcare") == "헬스케어"


@pytest.mark.unit
def test_unknown_returns_none_not_fake():
    # 매핑 미스는 None — 호출자가 영문 원문을 표시(fake 한글 금지)
    assert korean_sector_label("Quantum Flux Capacitors") is None
    assert korean_sector_label("") is None
    assert korean_sector_label(None) is None
