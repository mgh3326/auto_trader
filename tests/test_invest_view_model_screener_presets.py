"""ROB-276 — preset registry consistency tests for double_buy / kr_high_volume_surge rename."""

from __future__ import annotations

from app.services.invest_view_model.screener_presets import (
    _KR_ONLY_PRESET_IDS,
    SCREENER_PRESETS,
    screening_filters_for,
)


def test_high_volume_momentum_removed_and_volume_preset_renamed():
    ids = {p.id for p in SCREENER_PRESETS}
    assert "high_volume_momentum" not in ids
    assert "kr_high_volume_surge" in ids
    surge = next(p for p in SCREENER_PRESETS if p.id == "kr_high_volume_surge")
    assert surge.name == "거래량 급증"
    assert surge.market == "kr"


def test_double_buy_preset_present_and_kr_only():
    ids = {p.id for p in SCREENER_PRESETS}
    assert "double_buy" in ids
    db = next(p for p in SCREENER_PRESETS if p.id == "double_buy")
    assert db.name == "쌍끌이 매수"
    assert db.market == "kr"
    assert "double_buy" in _KR_ONLY_PRESET_IDS
    chip_labels = {c.label for c in db.filterChips}
    assert "국내" in chip_labels
    assert "외국인" in chip_labels   # independent
    assert "기관" in chip_labels     # independent


def test_investor_flow_momentum_copy_no_double_buy_wording():
    ifm = next(p for p in SCREENER_PRESETS if p.id == "investor_flow_momentum")
    assert "쌍끌이" not in ifm.description
    for chip in ifm.filterChips:
        detail = chip.detail or ""
        assert "쌍끌이" not in detail


def test_double_buy_screening_filters_lookup_is_kr_only_snapshot():
    filters = screening_filters_for("double_buy", "kr")
    assert filters["market"] == "kr"
    assert filters["sort_by"] == "change_rate"
    assert filters["sort_order"] == "desc"
    assert filters["min_change_rate"] == 0.0
    assert filters["include_double_buy"] is True
    assert filters["limit"] == 50


def test_every_preset_id_is_in_metric_field_map():
    from app.services.invest_view_model.screener_service import _METRIC_FIELD
    preset_ids = {p.id for p in SCREENER_PRESETS}
    metric_ids = set(_METRIC_FIELD.keys())
    missing = preset_ids - metric_ids
    assert not missing, f"presets missing from _METRIC_FIELD: {missing}"
