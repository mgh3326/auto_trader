"""Taxonomy constants for market events (ROB-128)."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_categories_cover_required_set():
    from app.services.market_events.taxonomy import CATEGORIES

    required = {
        "earnings",
        "economic",
        "disclosure",
        "crypto_exchange_notice",
        "crypto_protocol",
        "tokenomics",
        "regulatory",
    }
    assert required <= CATEGORIES


@pytest.mark.unit
def test_markets_cover_required_set():
    from app.services.market_events.taxonomy import MARKETS

    assert {"us", "kr", "crypto", "global"} <= MARKETS


@pytest.mark.unit
def test_statuses_cover_required_set():
    from app.services.market_events.taxonomy import STATUSES

    assert {"scheduled", "released", "revised", "cancelled", "tentative"} <= STATUSES


@pytest.mark.unit
def test_time_hints_cover_required_set():
    from app.services.market_events.taxonomy import TIME_HINTS

    assert {"before_open", "after_close", "during_market", "unknown"} <= TIME_HINTS


@pytest.mark.unit
def test_partition_statuses_cover_required_set():
    from app.services.market_events.taxonomy import PARTITION_STATUSES

    assert {
        "pending",
        "running",
        "succeeded",
        "failed",
        "partial",
    } <= PARTITION_STATUSES


@pytest.mark.unit
def test_validate_category_rejects_unknown():
    from app.services.market_events.taxonomy import validate_category

    validate_category("earnings")
    with pytest.raises(ValueError, match="unknown category"):
        validate_category("not_a_category")


@pytest.mark.unit
def test_sources_include_forexfactory():
    from app.services.market_events.taxonomy import SOURCES

    assert "forexfactory" in SOURCES
