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
    assert "최근 20거래일 내 52주 신고가" in p.description
    assert p.presetOrigin == "toss_parity"
    assert p.parityStatus == "full"
    assert "undervalued_breakout" in _KR_ONLY_PRESET_IDS
    # ROB-428 PR-C: undervalued_breakout's DISPLAY read-path was rerouted onto the
    # tvscreener KR snapshot, so it is now a fundamentals-registry preset (the OLD
    # market_valuation loader is kept only for reports/PIT). Its Toss-parity preset
    # definition (KR-only, full parity, filter chips) is unchanged.
    assert "undervalued_breakout" in FUNDAMENTALS_PRESET_SPECS
    chip_labels = {c.label: c.detail for c in p.filterChips}
    assert {"PER", "PBR", "신고가"} <= set(chip_labels)
    assert chip_labels["신고가"] == "최근 20거래일 이내"
    assert p.metricLabel == "신고가 경과"


def test_undervalued_breakout_metric_displays_new_high_age():
    from app.services.invest_view_model.screener_service import _metric_value_label

    label, warnings = _metric_value_label(
        "undervalued_breakout",
        {
            "new_high_age_trading_days": 8,
            # Proximity is still emitted as informational data, but it is not the
            # KR preset's pass/fail signal after ROB-430/ROB-432.
            "high_52w_proximity": 0.665,
        },
    )

    assert label == "8거래일"
    assert warnings == []
