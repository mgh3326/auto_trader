import pytest

from app.services.symbol_analysis.authority import (
    AUTHORITY,
    CATEGORIES,
    CORE_CATEGORIES,
    NON_AUTHORITY_SOURCES,
    AuthoritySpec,
)


@pytest.mark.unit
def test_every_category_has_a_primary_source():
    assert set(AUTHORITY) == set(CATEGORIES)
    for cat in CATEGORIES:
        assert isinstance(AUTHORITY[cat], AuthoritySpec)
        assert AUTHORITY[cat].primary, f"{cat} missing primary"


@pytest.mark.unit
def test_core_categories_are_subset_of_categories():
    assert set(CORE_CATEGORIES) <= set(CATEGORIES)
    assert set(CORE_CATEGORIES) == {"price", "consensus", "technicals"}


@pytest.mark.unit
def test_toss_naver_browser_are_never_authority():
    # reference/calibration 만 허용 — primary/fallback 으로 등장 금지.
    for cat, spec in AUTHORITY.items():
        assert spec.primary not in NON_AUTHORITY_SOURCES, cat
        assert spec.fallback not in NON_AUTHORITY_SOURCES, cat
        # naver_finance 는 reference 로만 허용
        for ref in spec.reference:
            assert ref in NON_AUTHORITY_SOURCES or ref == "stock_info"


@pytest.mark.unit
def test_price_authority_matches_invest_data_source_contract():
    # stocks/symbol seam 의 primary 와 정합 (drift-guard).
    from app.services.invest_data_source_contract import INVEST_DATA_SOURCE_CONTRACT

    symbol_entry = next(
        e
        for e in INVEST_DATA_SOURCE_CONTRACT
        if e.surface == "stocks" and e.collector_snapshot_kind == "symbol"
    )
    assert AUTHORITY["price"].primary == symbol_entry.source_name  # kis_live
    assert AUTHORITY["price"].fallback == symbol_entry.fallback_source  # stock_info
