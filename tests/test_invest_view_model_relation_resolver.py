"""Unit tests for relation_resolver."""

from __future__ import annotations

import pytest

from app.services.invest_view_model.relation_resolver import RelationResolver


@pytest.mark.unit
def test_relation_held_only() -> None:
    r = RelationResolver(held={("us", "AAPL")})
    assert r.relation("us", "AAPL") == "held"
    assert r.relation("us", "TSLA") == "none"


@pytest.mark.unit
def test_relation_watchlist_only() -> None:
    r = RelationResolver(watch={("kr", "005930")})
    assert r.relation("kr", "005930") == "watchlist"


@pytest.mark.unit
def test_relation_both() -> None:
    r = RelationResolver(held={("us", "BRK.B")}, watch={("us", "BRK.B")})
    assert r.relation("us", "BRK.B") == "both"


@pytest.mark.unit
def test_relation_normalizes_symbol() -> None:
    r = RelationResolver(held={("us", "BRK.B")})
    # KIS slash form, Yahoo dash form must both resolve
    assert r.relation("us", "BRK/B") == "held"
    assert r.relation("us", "BRK-B") == "held"


@pytest.mark.unit
def test_relation_market_case_insensitive() -> None:
    r = RelationResolver(held={("kr", "005930")})
    assert r.relation("KR", "005930") == "held"
