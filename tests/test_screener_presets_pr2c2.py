# tests/test_screener_presets_pr2c2.py
from __future__ import annotations

from app.services.invest_view_model.fundamentals_screener import (
    FUNDAMENTALS_PRESET_SPECS,
)
from app.services.invest_view_model.screener_presets import (
    _KR_ONLY_PRESET_IDS,
    preset_definitions,
)


def test_undervalued_breakout_preset_full_toss_parity_kr_only():
    presets = {p.id: p for p in preset_definitions(market="kr")}
    assert "undervalued_breakout" in presets
    p = presets["undervalued_breakout"]
    assert p.name == "저평가 탈출"
    assert p.presetOrigin == "toss_parity"
    assert p.parityStatus == "full"
    assert "undervalued_breakout" in _KR_ONLY_PRESET_IDS
    # valuation-only: NOT a fundamentals-registry preset
    assert "undervalued_breakout" not in FUNDAMENTALS_PRESET_SPECS
    chip_labels = {c.label for c in p.filterChips}
    assert {"PER", "PBR", "신고가"} <= chip_labels
