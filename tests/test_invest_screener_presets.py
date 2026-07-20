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
    from app.services.invest_view_model.fundamentals_screener import (
        FUNDAMENTALS_PRESET_SPECS,
    )

    # Every catalog preset must have a deterministic filter mapping.
    for p in preset_definitions():
        if p.id in FUNDAMENTALS_PRESET_SPECS:
            continue
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
        "crypto_funding_squeeze",
        "crypto_funding_overheated",
        "crypto_oi_surge",
        "crypto_long_short_skew",
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
    # ROB-170 / ROB-276 / ROB-359 / ROB-422 shipped real Toss parity for these.
    for pid in (
        "consecutive_gainers",
        "double_buy",
        "high_yield_value",
        "profitable_company",
        "undervalued_growth",
        "stable_growth",
        "future_dividend_king",
        "cheap_value",
        "steady_dividend",
    ):
        assert by_id[pid].parityStatus == "full"


@pytest.mark.unit
def test_partial_and_mismatch_presets_explain_the_gap() -> None:
    by_id = {p.id: p for p in preset_definitions("kr")}
    # All baseline mismatch or partial presets are now either upgraded to full parity
    # or reclassified as auto_trader_original (extra) presets, leaving 0 partial/mismatch KR presets.
    partial_or_mismatch = [
        p for p in by_id.values() if p.parityStatus in ("partial", "mismatch")
    ]
    assert len(partial_or_mismatch) == 0


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
    # ROB-427 PR3: exposed AND active for US (Yahoo valuation backs ROE+PER).
    us_by_id = {p.id: p for p in preset_definitions("us")}
    assert "high_yield_value" in us_by_id
    assert us_by_id["high_yield_value"].availability == "active"
    assert us_by_id["high_yield_value"].availabilityReason is None


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


@pytest.mark.unit
def test_support_proximity_preset_is_kr_only_and_data_pending_for_us() -> None:
    kr_preset = get_preset("support_proximity", market="kr")
    assert kr_preset is not None
    assert kr_preset.availability == "active"
    assert kr_preset.metricLabel == "지지선까지 거리"
    assert kr_preset.presetOrigin == "auto_trader_original"

    us_preset = get_preset("support_proximity", market="us")
    assert us_preset is not None
    # ROB-441 PR5 retired data_pending; a KR-first-rollout preset is classified
    # unsupported-with-reason (same bucket as the flow presets) until US lands.
    assert us_preset.availability == "unsupported"
    assert us_preset.availabilityReason


@pytest.mark.unit
def test_support_proximity_screening_filters_have_quality_floors() -> None:
    filters = screening_filters_for("support_proximity", market="kr")
    assert filters.get("min_market_cap") == pytest.approx(300_000_000_000.0)
    assert filters.get("min_turnover") == pytest.approx(1_000_000_000.0)
    assert filters.get("sort_by") == "dist_to_support_pct"
    assert filters.get("sort_order") == "asc"
