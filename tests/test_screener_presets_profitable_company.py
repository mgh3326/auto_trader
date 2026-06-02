# tests/test_screener_presets_profitable_company.py
from __future__ import annotations

from app.services.invest_view_model.screener_presets import (
    _KR_ONLY_PRESET_IDS,
    preset_definitions,
)


def test_profitable_company_preset_present_and_full_parity():
    presets = {p.id: p for p in preset_definitions(market="kr")}
    assert "profitable_company" in presets
    p = presets["profitable_company"]
    assert p.name == "돈 잘버는 회사"
    assert p.presetOrigin == "toss_parity"
    assert p.parityStatus == "full"
    chip_labels = {c.label for c in p.filterChips}
    assert {"매출총이익률", "ROE"} <= chip_labels
    assert "profitable_company" in _KR_ONLY_PRESET_IDS
