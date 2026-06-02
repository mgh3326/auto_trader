# tests/test_screener_presets_pr2c1.py
from __future__ import annotations

from app.services.invest_view_model.fundamentals_screener import (
    FUNDAMENTALS_PRESET_SPECS,
)
from app.services.invest_view_model.screener_presets import (
    _KR_ONLY_PRESET_IDS,
    preset_definitions,
)


def test_cheap_value_and_steady_dividend_now_full_and_kr_only():
    presets = {p.id: p for p in preset_definitions(market="kr")}
    for pid in ("cheap_value", "steady_dividend"):
        assert presets[pid].parityStatus == "full"
        assert presets[pid].parityNote is None
        assert pid in _KR_ONLY_PRESET_IDS
        assert pid in FUNDAMENTALS_PRESET_SPECS  # registry-routed (snapshot-only)


def test_mismatch_presets_reclassified_as_auto_trader_original():
    presets = {p.id: p for p in preset_definitions(market="kr")}
    for pid in ("oversold_recovery", "growth_expectation"):
        assert presets[pid].presetOrigin == "auto_trader_original"
        assert presets[pid].parityStatus is None
        assert pid not in FUNDAMENTALS_PRESET_SPECS  # still generic-provider routed
