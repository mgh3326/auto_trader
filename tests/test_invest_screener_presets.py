"""ROB-147 — static preset catalog tests."""

from __future__ import annotations

import pytest

from app.services.invest_view_model.screener_presets import (
    DEFAULT_PRESET_ID,
    SCREENER_PRESETS,
    get_preset,
    preset_definitions,
    screening_filters_for,
)


@pytest.mark.unit
def test_catalog_has_at_least_six_presets() -> None:
    # Linear acceptance: 최소 5개 이상 — we ship 6.
    assert len(SCREENER_PRESETS) >= 6


@pytest.mark.unit
def test_default_preset_is_in_catalog() -> None:
    ids = {p.id for p in preset_definitions()}
    assert DEFAULT_PRESET_ID in ids


@pytest.mark.unit
def test_all_presets_have_metric_label_and_kr_market() -> None:
    for p in preset_definitions():
        assert p.metricLabel
        assert p.market == "kr"


@pytest.mark.unit
def test_inki_badge_appears_at_least_once() -> None:
    assert any("인기" in p.badges for p in preset_definitions())


@pytest.mark.unit
def test_get_preset_returns_none_for_unknown_id() -> None:
    assert get_preset("does_not_exist") is None


@pytest.mark.unit
def test_get_preset_returns_match() -> None:
    preset = get_preset(DEFAULT_PRESET_ID)
    assert preset is not None
    assert preset.id == DEFAULT_PRESET_ID


@pytest.mark.unit
def test_screening_filters_known_for_each_preset() -> None:
    # Every catalog preset must have a deterministic filter mapping.
    for p in preset_definitions():
        filters = screening_filters_for(p.id)
        assert isinstance(filters, dict)
        # Every preset must specify market and limit so the screening service
        # has bounded inputs.
        assert filters.get("market") == "kr"
        assert isinstance(filters.get("limit"), int)


@pytest.mark.unit
def test_consecutive_gainers_preset_requests_streak_filter() -> None:
    filters = screening_filters_for("consecutive_gainers", market="kr")
    assert filters.get("min_consecutive_up_days") == 5


@pytest.mark.unit
def test_consecutive_gainers_preset_matches_toss_rank_and_limit() -> None:
    filters = screening_filters_for("consecutive_gainers", market="kr")

    assert filters.get("min_consecutive_up_days") == 5
    assert filters.get("min_week_change_rate") == 0.0
    assert filters.get("sort_by") == "week_change_rate"
    assert filters.get("sort_order") == "desc"
    assert filters.get("limit") == 80


@pytest.mark.unit
def test_consecutive_gainers_chip_says_5_days() -> None:
    preset = get_preset("consecutive_gainers", market="kr")
    assert preset is not None
    chip_details = [c.detail for c in preset.filterChips if c.detail]
    assert any("5일 연속 상승" in d for d in chip_details)


@pytest.mark.unit
def test_crypto_preset_definitions_are_crypto_scoped() -> None:
    presets = preset_definitions("crypto")

    assert [p.id for p in presets] == [
        "crypto_high_volume",
        "crypto_oversold",
        "crypto_momentum",
    ]
    assert all(p.market == "crypto" for p in presets)
    assert all(p.filterChips[0].label == "가상자산" for p in presets)


@pytest.mark.unit
def test_every_kr_preset_declares_origin() -> None:
    # ROB-359 Scope B: the catalog must separate Toss-parity presets from
    # auto_trader-original ones; no KR preset may leave presetOrigin unset.
    for p in preset_definitions("kr"):
        assert p.presetOrigin in {"toss_parity", "auto_trader_original"}, p.id


@pytest.mark.unit
def test_auto_trader_original_presets_are_flagged() -> None:
    by_id = {p.id: p for p in preset_definitions("kr")}
    for pid in ("kr_high_volume_surge", "investor_flow_momentum"):
        assert by_id[pid].presetOrigin == "auto_trader_original", pid
        # Parity status is meaningless for auto_trader-original presets.
        assert by_id[pid].parityStatus is None, pid


@pytest.mark.unit
def test_toss_parity_presets_have_a_parity_status() -> None:
    for p in preset_definitions("kr"):
        if p.presetOrigin == "toss_parity":
            assert p.parityStatus in {"full", "partial", "mismatch"}, p.id


@pytest.mark.unit
def test_already_implemented_presets_marked_full() -> None:
    by_id = {p.id: p for p in preset_definitions("kr")}
    # ROB-170 / ROB-276 shipped real Toss parity for these.
    assert by_id["consecutive_gainers"].parityStatus == "full"
    assert by_id["double_buy"].parityStatus == "full"


@pytest.mark.unit
def test_partial_and_mismatch_presets_explain_the_gap() -> None:
    by_id = {p.id: p for p in preset_definitions("kr")}
    assert by_id["cheap_value"].parityStatus == "partial"
    assert by_id["steady_dividend"].parityStatus == "partial"
    assert by_id["oversold_recovery"].parityStatus == "mismatch"
    assert by_id["growth_expectation"].parityStatus == "mismatch"
    # Honest divergence must be explained, not silently approximated.
    for pid in (
        "cheap_value",
        "steady_dividend",
        "oversold_recovery",
        "growth_expectation",
    ):
        assert by_id[pid].parityNote, pid
    # partial presets specifically flag the un-implementable conditions.
    for pid in ("cheap_value", "steady_dividend"):
        assert "확인 불가" in (by_id[pid].parityNote or ""), pid


@pytest.mark.unit
def test_high_yield_value_preset_is_full_toss_parity_kr_only() -> None:
    # ROB-359 PR4: 고수익 저평가 (ROE≥15 + PER 0~10) implemented from
    # market_valuation_snapshots.
    by_id = {p.id: p for p in preset_definitions("kr")}
    assert "high_yield_value" in by_id
    preset = by_id["high_yield_value"]
    assert preset.name == "고수익 저평가"
    assert preset.presetOrigin == "toss_parity"
    assert preset.parityStatus == "full"
    assert preset.metricLabel == "ROE"
    # KR-only: must not surface in the US catalog.
    assert "high_yield_value" not in {p.id for p in preset_definitions("us")}


@pytest.mark.unit
def test_high_yield_value_filter_mapping_is_bounded() -> None:
    filters = screening_filters_for("high_yield_value", market="kr")
    assert filters.get("market") == "kr"
    assert filters.get("min_roe") == 15.0
    assert filters.get("max_per") == 10.0
    assert isinstance(filters.get("limit"), int)


@pytest.mark.unit
def test_crypto_presets_are_auto_trader_original() -> None:
    for p in preset_definitions("crypto"):
        assert p.presetOrigin == "auto_trader_original", p.id
        assert p.parityStatus is None, p.id


@pytest.mark.unit
def test_crypto_screening_filters_are_read_only_market_filters() -> None:
    high_volume = screening_filters_for("crypto_high_volume", "crypto")
    oversold = screening_filters_for("crypto_oversold", "crypto")
    momentum = screening_filters_for("crypto_momentum", "crypto")

    assert high_volume == {
        "market": "crypto",
        "sort_by": "trade_amount",
        "sort_order": "desc",
        "limit": 20,
    }
    assert oversold["market"] == "crypto"
    assert oversold["sort_by"] == "rsi"
    assert oversold["max_rsi"] == pytest.approx(35.0)
    assert momentum["market"] == "crypto"
    assert momentum["sort_by"] == "change_rate"
    assert screening_filters_for("consecutive_gainers", "crypto") == {}
    assert screening_filters_for("crypto_high_volume", "kr") == {}
