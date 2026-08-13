"""Tests for the pure /invest deep-link builders (INVEST-WATCH-UI §57차 item ③)."""

from __future__ import annotations

import pytest

from app.core.invest_deep_links import build_order_detail_url, build_watches_url


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
def test_build_order_detail_url():
    url = build_order_detail_url(broker="kis", ledger_id=42)
    assert url is not None
    assert url.endswith("/invest/orders/kis/42")


@pytest.mark.unit
def test_build_order_detail_url_missing_broker_returns_none():
    assert build_order_detail_url(broker=None, ledger_id=42) is None
    assert build_order_detail_url(broker="", ledger_id=42) is None


@pytest.mark.unit
def test_build_order_detail_url_missing_ledger_id_returns_none():
    assert build_order_detail_url(broker="kis", ledger_id=None) is None
