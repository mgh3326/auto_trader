"""Tests for the pure /invest deep-link builders (INVEST-WATCH-UI §57차 item ③)."""

from __future__ import annotations

import pytest

from app.core.invest_deep_links import (
    build_funding_advisory_url,
    build_funding_declaration_url,
    build_loss_cut_approval_url,
    build_order_detail_url,
    build_watches_url,
)


@pytest.mark.unit
def test_build_watches_url_bare():
    url = build_watches_url()
    assert url.endswith("/invest/watches")
    assert "?" not in url


@pytest.mark.unit
def test_build_watches_url_with_filters():
    url = build_watches_url(market="kr", status="active", symbol="005930")
    assert url.endswith("/invest/watches?market=kr&status=active&symbol=005930")


@pytest.mark.unit
def test_build_watches_url_uses_public_base_url(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://example.test/")
    url = build_watches_url()
    assert url == "https://example.test/invest/watches"


@pytest.mark.unit
def test_build_loss_cut_approval_url_contains_only_proposal_id(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://example.test/")
    proposal_id = "11111111-2222-4333-8444-555555555555"

    url = build_loss_cut_approval_url(proposal_id=proposal_id)

    assert url == f"https://example.test/invest/approvals/loss-cut/{proposal_id}"
    assert "nonce" not in url
    assert "quantity" not in url


@pytest.mark.unit
def test_build_order_detail_url():
    url = build_order_detail_url(broker="kis", market="kr", ledger_id=42)
    assert url is not None
    assert url.endswith("/invest/orders/kis/kr/42")


# verify-r1 BLOCKER-1 regression: broker="kis" alone is ambiguous between the
# KR domestic ledger and the US live ledger (independent id sequences), so
# the same broker+ledger_id must produce DIFFERENT urls depending on market.
@pytest.mark.unit
def test_build_order_detail_url_market_disambiguates_same_broker_and_ledger_id():
    kr_url = build_order_detail_url(broker="kis", market="kr", ledger_id=42)
    us_url = build_order_detail_url(broker="kis", market="us", ledger_id=42)
    assert kr_url != us_url
    assert kr_url.endswith("/invest/orders/kis/kr/42")
    assert us_url.endswith("/invest/orders/kis/us/42")


@pytest.mark.unit
def test_build_order_detail_url_missing_broker_returns_none():
    assert build_order_detail_url(broker=None, market="kr", ledger_id=42) is None
    assert build_order_detail_url(broker="", market="kr", ledger_id=42) is None


@pytest.mark.unit
def test_build_order_detail_url_missing_market_returns_none():
    assert build_order_detail_url(broker="kis", market=None, ledger_id=42) is None
    assert build_order_detail_url(broker="kis", market="", ledger_id=42) is None


@pytest.mark.unit
def test_build_order_detail_url_missing_ledger_id_returns_none():
    assert build_order_detail_url(broker="kis", market="kr", ledger_id=None) is None


@pytest.mark.unit
def test_build_funding_urls_land_on_read_only_detail_and_exact_form_anchor():
    detail = build_funding_advisory_url("advisory id")
    assert detail is not None
    assert detail.endswith("/invest/funding/advisory%20id")
    assert build_funding_declaration_url().endswith(
        "/invest/funding#external-cash-declaration"
    )


@pytest.mark.unit
def test_build_funding_detail_url_rejects_empty_id():
    assert build_funding_advisory_url("") is None
