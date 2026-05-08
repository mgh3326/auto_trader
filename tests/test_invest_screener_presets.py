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
