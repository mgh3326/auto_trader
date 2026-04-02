from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import portfolio


class _FakeOverviewService:
    def __init__(self) -> None:
        self.get_overview = AsyncMock(
            return_value={
                "success": True,
                "as_of": "2026-02-20T00:00:00+00:00",
                "filters": {
                    "market": "US",
                    "account_keys": ["live:kis", "manual:1"],
                    "q": "aapl",
                },
                "summary": {
                    "total_positions": 1,
                    "by_market": {"KR": 0, "US": 1, "CRYPTO": 0},
                },
                "facets": {
                    "accounts": [
                        {
                            "account_key": "live:kis",
                            "broker": "kis",
                            "account_name": "KIS 실계좌",
                            "source": "live",
                            "market_types": ["US"],
                        }
                    ]
                },
                "positions": [
                    {
                        "market_type": "US",
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "quantity": 2,
                        "avg_price": 150.0,
                        "current_price": 160.0,
                        "evaluation": 320.0,
                        "profit_loss": 20.0,
                        "profit_rate": 0.0667,
                        "components": [],
                    }
                ],
                "warnings": [],
            }
        )
        self.enrich_manual_positions = AsyncMock(
            return_value={
                "success": True,
                "as_of": "2026-02-20T00:00:00+00:00",
                "positions": [
                    {
                        "market_type": "US",
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "quantity": 2,
                        "avg_price": 150.0,
                        "current_price": 165.0,
                        "evaluation": 330.0,
                        "profit_loss": 30.0,
                        "profit_rate": 0.1,
                        "components": [],
                    }
                ],
                "warnings": [],
            }
        )


def _create_client() -> tuple[TestClient, _FakeOverviewService, _FakeDashboardService]:
    app = FastAPI()
    fake_overview = _FakeOverviewService()
    fake_dashboard = _FakeDashboardService()
    app.include_router(portfolio.router)
    app.dependency_overrides[portfolio.get_authenticated_user] = lambda: (
        SimpleNamespace(id=7)
    )
    app.dependency_overrides[portfolio.get_portfolio_overview_service] = lambda: (
        fake_overview
    )
    app.dependency_overrides[portfolio.get_portfolio_dashboard_service] = lambda: (
        fake_dashboard
    )
    return TestClient(app), fake_overview, fake_dashboard


def test_portfolio_dashboard_page_renders_screener_style_shell() -> None:
    client, _, _ = _create_client()
    response = client.get("/portfolio/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    assert 'id="portfolio-main-page"' in body
    assert 'id="portfolio-table"' in body
    assert 'id="portfolio-cards"' in body
    assert "@media (max-width: 760px)" in body
    assert "function escapeHtml(value)" in body
    assert "escapeHtml(item.name)" in body
    assert "escapeHtml(warning)" in body
    assert "symbol\\s+not\\s+tradable" in body
    assert "const detailLimit = 5;" in body
    assert "원인 요약:" in body
    assert "외 ${omittedCount}건 생략" in body


def test_portfolio_dashboard_page_renders_full_width_results_layout() -> None:
    client, _, _ = _create_client_with_dashboard()
    response = client.get("/portfolio/")

    assert response.status_code == 200
    body = response.text

    assert 'id="portfolio-results-panel"' in body
    assert 'id="portfolio-secondary-grid"' in body
    assert "table-layout: fixed;" in body
    assert "renderOverviewLoadingState" in body


def test_portfolio_overview_api_passes_repeated_account_keys() -> None:
    client, fake_service, fake_dashboard = _create_client()
    response = client.get(
        "/portfolio/api/overview",
        params=[
            ("market", "US"),
            ("account_keys", "live:kis"),
            ("account_keys", "manual:1"),
            ("q", "aapl"),
        ],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["filters"]["market"] == "US"
    assert payload["filters"]["account_keys"] == ["live:kis", "manual:1"]

    fake_service.get_overview.assert_awaited_once_with(
        user_id=7,
        market="US",
        account_keys=["live:kis", "manual:1"],
        q="aapl",
        skip_missing_prices=False,
    )
    fake_dashboard.get_cash_snapshot.assert_not_awaited()


def test_portfolio_overview_api_uses_default_filters() -> None:
    client, fake_service, fake_dashboard = _create_client()
    response = client.get("/portfolio/api/overview")

    assert response.status_code == 200
    fake_service.get_overview.assert_awaited_once_with(
        user_id=7,
        market="ALL",
        account_keys=None,
        q=None,
        skip_missing_prices=False,
    )
    fake_dashboard.get_cash_snapshot.assert_not_awaited()


def test_portfolio_overview_api_forwards_skip_missing_prices_flag() -> None:
    client, fake_service, fake_dashboard = _create_client()
    response = client.get(
        "/portfolio/api/overview",
        params={"skip_missing_prices": "true"},
    )

    assert response.status_code == 200
    fake_service.get_overview.assert_awaited_once_with(
        user_id=7,
        market="ALL",
        account_keys=None,
        q=None,
        skip_missing_prices=True,
    )
    fake_dashboard.get_cash_snapshot.assert_not_awaited()


def test_portfolio_overview_api_merges_batch_journal_snapshots() -> None:
    client, _, fake_dashboard = _create_client()

    response = client.get("/portfolio/api/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["positions"][0]["journal"]["target_price"] == 165.0
    assert payload["positions"][0]["journal"]["target_distance_pct"] == 3.12
    fake_dashboard.get_journals_batch.assert_awaited_once_with(
        ["AAPL"],
        current_prices={"AAPL": 160.0},
    )


def test_portfolio_enrich_api_returns_only_requested_positions() -> None:
    client, fake_service, fake_dashboard = _create_client()

    response = client.post(
        "/portfolio/api/overview/enrich",
        json={
            "symbols": [
                {"symbol": "AAPL", "market_type": "US"},
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["positions"]) == 1
    assert payload["positions"][0]["symbol"] == "AAPL"
    assert payload["positions"][0]["journal"]["target_distance_pct"] == 3.12

    fake_service.enrich_manual_positions.assert_awaited_once_with(
        user_id=7,
        targets=[{"symbol": "AAPL", "market_type": "US"}],
    )
    fake_dashboard.get_journals_batch.assert_awaited_once_with(
        ["AAPL"],
        current_prices={"AAPL": 165.0},
    )


def test_portfolio_overview_api_rejects_invalid_market() -> None:
    client, fake_service, _ = _create_client()
    response = client.get("/portfolio/api/overview", params={"market": "INVALID"})

    assert response.status_code == 422
    fake_service.get_overview.assert_not_awaited()


class _FakeDashboardService:
    def __init__(self) -> None:
        self.get_latest_journal_snapshot = AsyncMock(
            return_value={
                "id": 1,
                "symbol": "AAPL",
                "instrument_type": "equity_us",
                "side": "buy",
                "entry_price": 150.0,
                "quantity": 10.0,
                "thesis": "Test thesis",
                "strategy": "Test strategy",
                "target_price": 165.0,
                "stop_loss": 135.0,
                "target_distance_pct": 10.0,
                "stop_distance_pct": -10.0,
                "status": "active",
                "created_at": "2026-04-01T00:00:00+00:00",
            }
        )
        self.get_cash_snapshot = AsyncMock(
            return_value={
                "accounts": {
                    "kis_krw": {
                        "broker": "kis",
                        "currency": "KRW",
                        "balance": 1000000.0,
                        "orderable": 900000.0,
                    },
                    "kis_usd": {
                        "broker": "kis",
                        "currency": "USD",
                        "balance": 1000.0,
                        "orderable": 900.0,
                    },
                    "upbit_krw": {
                        "broker": "upbit",
                        "currency": "KRW",
                        "balance": 500000.0,
                        "orderable": 500000.0,
                    },
                },
                "manual_cash": {
                    "amount": 1000000.0,
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "stale_warning": False,
                },
                "summary": {
                    "total_available_krw": 3570000.0,
                    "exchange_rate_usd_krw": 1300.0,
                    "as_of": "2026-04-01T00:00:00+00:00",
                },
                "errors": [],
            }
        )
        self.get_journals_batch = AsyncMock(
            return_value={
                "AAPL": {
                    "symbol": "AAPL",
                    "target_price": 165.0,
                    "stop_loss": 135.0,
                    "target_distance_pct": 3.12,
                    "stop_distance_pct": -7.14,
                }
            }
        )


def _create_client_with_dashboard() -> tuple[
    TestClient, _FakeOverviewService, _FakeDashboardService
]:
    app = FastAPI()
    fake_overview = _FakeOverviewService()
    fake_dashboard = _FakeDashboardService()
    app.include_router(portfolio.router)
    app.dependency_overrides[portfolio.get_authenticated_user] = lambda: (
        SimpleNamespace(id=7)
    )
    app.dependency_overrides[portfolio.get_portfolio_overview_service] = lambda: (
        fake_overview
    )
    app.dependency_overrides[portfolio.get_portfolio_dashboard_service] = lambda: (
        fake_dashboard
    )
    return TestClient(app), fake_overview, fake_dashboard


def test_portfolio_journal_api_returns_detail_payload() -> None:
    client, _, fake_dashboard = _create_client_with_dashboard()
    response = client.get(
        "/portfolio/api/journal/AAPL", params={"current_price": 160.0}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "AAPL"
    assert payload["target_price"] == 165.0
    assert payload["stop_loss"] == 135.0
    assert payload["target_distance_pct"] == 10.0
    assert payload["stop_distance_pct"] == -10.0

    fake_dashboard.get_latest_journal_snapshot.assert_awaited_once_with(
        "AAPL", current_price=160.0
    )


def test_portfolio_journal_api_returns_404_when_missing() -> None:
    client, _, fake_dashboard = _create_client_with_dashboard()
    fake_dashboard.get_latest_journal_snapshot.return_value = None

    response = client.get("/portfolio/api/journal/NONEXISTENT")

    assert response.status_code == 404
    assert response.json()["detail"] == "Trade journal not found"


def test_portfolio_cash_api_returns_dashboard_cash_summary() -> None:
    client, _, fake_dashboard = _create_client_with_dashboard()
    response = client.get("/portfolio/api/cash")

    assert response.status_code == 200
    payload = response.json()

    assert "accounts" in payload
    assert "kis_krw" in payload["accounts"]
    assert "kis_usd" in payload["accounts"]
    assert "upbit_krw" in payload["accounts"]
    assert "manual_cash" in payload
    assert "summary" in payload
    assert payload["summary"]["total_available_krw"] == 3570000.0

    fake_dashboard.get_cash_snapshot.assert_awaited_once()


def test_portfolio_dashboard_page_renders_phase1_panels_and_scripts() -> None:
    client, _, _ = _create_client_with_dashboard()
    response = client.get("/portfolio/")

    assert response.status_code == 200
    body = response.text

    assert 'id="portfolio-cash-panel"' in body
    assert 'id="portfolio-allocation-panel"' in body
    assert 'id="allocation-donut-chart"' in body
    assert 'id="top-positions-chart"' in body
    assert 'id="portfolio-detail-panel"' in body
    assert "chart.js" in body.lower() or "cdn.jsdelivr.net/npm/chart.js" in body
    assert "function fetchCashSummary()" in body
    assert "function openPositionDetail(" in body
    assert "function renderCharts(" in body
    assert 'id="sort-select"' in body
    assert 'params.append("skip_missing_prices", "true");' in body
    assert "function mergeEnrichedPositions(" in body
    assert "function buildPortfolioWarnings(" in body
    assert "function calculateSellSimulation(" in body
    assert "/portfolio/api/overview/enrich" in body
    assert "조회중..." in body
    assert "/portfolio/api/simulate-sell" not in body


def test_portfolio_dashboard_page_retains_filter_controls_and_component_detail_hooks() -> (
    None
):
    client, _, _ = _create_client_with_dashboard()
    response = client.get("/portfolio/")

    assert response.status_code == 200
    body = response.text

    assert 'refreshButton.addEventListener("click", () => {' in body
    assert 'marketSelect.addEventListener("change", fetchOverview);' in body
    assert 'queryInput.addEventListener("keydown", (event) => {' in body
    assert 'clearAccountsButton.addEventListener("click", () => {' in body
    assert "fetchCashSummary();" in body
    assert "function findSelectedPosition(" in body
    assert "function renderComponentHoldings(" in body
    assert "selectedPosition.components" in body
    assert "계좌별 보유 내역" in body


@pytest.mark.unit
def test_portfolio_dashboard_offcanvas_contains_detail_page_link_hook() -> None:
    client, _, _ = _create_client_with_dashboard()
    response = client.get("/portfolio/")
    body = response.text

    assert "상세 페이지 열기" in body
    assert "buildPositionDetailUrl(" in body
    assert "/portfolio/positions/" in body


@pytest.mark.unit
def test_portfolio_dashboard_page_includes_detail_url_contract_for_positions() -> None:
    client, _, _ = _create_client_with_dashboard()
    body = client.get("/portfolio/").text

    assert 'data-detail-url="${detailUrl}"' in body
    assert 'role="link"' in body
    assert 'tabindex="0"' in body
    assert 'class="position-detail-link"' in body


@pytest.mark.unit
def test_portfolio_dashboard_page_uses_guarded_delegated_navigation() -> None:
    client, _, _ = _create_client_with_dashboard()
    body = client.get("/portfolio/").text

    assert "function shouldIgnorePositionActivationTarget(target)" in body
    assert 'target.closest("a, button, input, select, textarea, label")' in body
    assert 'event.key === "Enter" || event.key === " "' in body
    assert "window.location.assign(detailUrl)" in body
