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
