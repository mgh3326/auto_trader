# tests/test_screener_presets_pr2b.py
from __future__ import annotations

from app.services.invest_view_model.screener_presets import (
    _KR_ONLY_PRESET_IDS,
    preset_definitions,
)


def test_three_pr2b_presets_present_full_parity_kr_only():
    presets = {p.id: p for p in preset_definitions(market="kr")}
    for pid, name in [
        ("undervalued_growth", "저평가 성장주"),
        ("stable_growth", "안정 성장주"),
        ("future_dividend_king", "미래의 배당왕"),
    ]:
        assert pid in presets, pid
        assert presets[pid].name == name
        assert presets[pid].presetOrigin == "toss_parity"
        assert presets[pid].parityStatus == "full"
        assert pid in _KR_ONLY_PRESET_IDS
