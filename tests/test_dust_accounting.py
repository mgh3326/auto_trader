from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_list_holdings_marks_crypto_dust_and_keeps_non_crypto_false(monkeypatch):
    from app.mcp_server.tooling import portfolio_holdings

    positions = [
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "quantity": 0.00005,
            "avg_buy_price": 70_000_000.0,
            "current_price": 70_000_000.0,
            "evaluation_amount": 3500.0,
            "profit_loss": 0.0,
            "profit_rate": 0.0,
        },
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-ETH",
            "name": "이더리움",
            "quantity": 0.02,
            "avg_buy_price": 4_000_000.0,
            "current_price": 4_000_000.0,
            "evaluation_amount": 80_000.0,
            "profit_loss": 0.0,
            "profit_rate": 0.0,
        },
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "source": "kis_api",
            "instrument_type": "equity_us",
            "market": "us",
            "symbol": "AAPL",
            "name": "Apple",
            "quantity": 1.0,
            "avg_buy_price": 50.0,
            "current_price": 50.0,
            "evaluation_amount": 50.0,
            "profit_loss": 0.0,
            "profit_rate": 0.0,
        },
    ]

    monkeypatch.setattr(
        portfolio_holdings,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(positions, [], None, None)),
    )
    monkeypatch.setattr(
        portfolio_holdings,
        "_build_holdings_summary",
        lambda _positions, _include_current_price: {
            "total_buy_amount": 0.0,
            "total_evaluation": 0.0,
            "total_profit_loss": 0.0,
            "total_profit_rate": 0.0,
            "position_count": len(_positions),
            "weights": [],
        },
    )
    monkeypatch.setattr(
        portfolio_holdings,
        "_min_order_krw",
        lambda _symbol: 5_000.0,
    )

    result = await portfolio_holdings._get_holdings_impl(minimum_value=0)

    by_symbol: dict[str, dict[str, object]] = {}
    for account in result["accounts"]:
        for row in account["positions"]:
            by_symbol[row["symbol"]] = row

    assert by_symbol["KRW-BTC"]["dust"] is True
    assert by_symbol["KRW-ETH"]["dust"] is False
    assert by_symbol["AAPL"]["dust"] is False


def test_min_order_krw_returns_fixed_crypto_minimum():
    from app.mcp_server.tooling import shared

    assert shared.min_order_krw("KRW-BTC") == shared.DEFAULT_MINIMUM_VALUES["crypto"]
    assert shared.min_order_krw("KRW-ETH") == shared.DEFAULT_MINIMUM_VALUES["crypto"]


def test_position_to_output_includes_dust_with_false_default():
    from app.mcp_server.tooling.shared import position_to_output

    base = {
        "symbol": "KRW-BTC",
        "name": "비트코인",
        "market": "crypto",
        "quantity": 1.0,
        "avg_buy_price": 10.0,
        "current_price": 10.0,
        "evaluation_amount": 10.0,
        "profit_loss": 0.0,
        "profit_rate": 0.0,
    }
    with_dust = position_to_output({**base, "dust": True})
    without_dust = position_to_output(base)

    assert with_dust["dust"] is True
    assert without_dust["dust"] is False


def test_portfolio_overview_aggregate_sets_dust_for_crypto_only():
    from app.services.portfolio_overview_service import PortfolioOverviewService

    service = PortfolioOverviewService(AsyncMock())
    components = [
        {
            "account_key": "live:upbit",
            "broker": "upbit",
            "account_name": "업비트",
            "source": "live",
            "market_type": "CRYPTO",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "quantity": 0.00005,
            "avg_price": 70_000_000.0,
            "current_price": 70_000_000.0,
            "evaluation": 3500.0,
            "profit_loss": 0.0,
            "profit_rate": 0.0,
        },
        {
            "account_key": "live:upbit",
            "broker": "upbit",
            "account_name": "업비트",
            "source": "live",
            "market_type": "CRYPTO",
            "symbol": "KRW-ETH",
            "name": "이더리움",
            "quantity": 0.02,
            "avg_price": 4_000_000.0,
            "current_price": 4_000_000.0,
            "evaluation": 80_000.0,
            "profit_loss": 0.0,
            "profit_rate": 0.0,
        },
        {
            "account_key": "live:kis",
            "broker": "kis",
            "account_name": "KIS",
            "source": "live",
            "market_type": "KR",
            "symbol": "005930",
            "name": "삼성전자",
            "quantity": 1.0,
            "avg_price": 3000.0,
            "current_price": 3000.0,
            "evaluation": 3000.0,
            "profit_loss": 0.0,
            "profit_rate": 0.0,
        },
        {
            "account_key": "live:kis-us",
            "broker": "kis",
            "account_name": "KIS US",
            "source": "live",
            "market_type": "US",
            "symbol": "AAPL",
            "name": "Apple",
            "quantity": 1.0,
            "avg_price": 50.0,
            "current_price": 50.0,
            "evaluation": 50.0,
            "profit_loss": 0.0,
            "profit_rate": 0.0,
        },
    ]

    rows = service._aggregate_positions(components, usd_krw=1300.0)
    by_symbol = {row["symbol"]: row for row in rows}

    assert by_symbol["KRW-BTC"]["dust"] is True
    assert by_symbol["KRW-ETH"]["dust"] is False
    assert by_symbol["005930"]["dust"] is False
    assert by_symbol["AAPL"]["dust"] is False


def test_build_brief_text_renders_dust_footnote_phrase_and_omits_when_none():
    from app.schemas.n8n.common import N8nMarketOverview
    from app.services.n8n_daily_brief_service import _build_brief_text

    with_dust = _build_brief_text(
        date_fmt="04/17 (금)",
        market_overview=N8nMarketOverview(
            fear_greed=None,
            btc_dominance=None,
            total_market_cap_change_24h=None,
            economic_events_today=[],
        ),
        pending_by_market={},
        portfolio_by_market={
            "crypto": {
                "total_value_fmt": "100만",
                "pnl_fmt": "+1.0%",
                "dust_positions": [
                    {
                        "symbol": "KRW-BTC",
                        "quantity": 0.00001,
                        "current_krw_value": 350.0,
                    }
                ],
            }
        },
        yesterday_fills={"total": 0, "fills": []},
    )
    without_dust = _build_brief_text(
        date_fmt="04/17 (금)",
        market_overview=N8nMarketOverview(
            fear_greed=None,
            btc_dominance=None,
            total_market_cap_change_24h=None,
            economic_events_today=[],
        ),
        pending_by_market={},
        portfolio_by_market={
            "crypto": {
                "total_value_fmt": "100만",
                "pnl_fmt": "+1.0%",
                "dust_positions": [],
            }
        },
        yesterday_fills={"total": 0, "fills": []},
    )

    assert "🧹 Dust" in with_dust
    assert "execution-actionable 제외, journal 유지" in with_dust
    assert "BTC 1e-05 (~350 KRW)" in with_dust
    assert "🧹 Dust" not in without_dust


def test_build_portfolio_summary_excludes_dust_from_top_gainers_and_losers():
    from app.services.n8n_daily_brief_service import _build_portfolio_summary

    summary = _build_portfolio_summary(
        {
            "positions": [
                {
                    "market_type": "CRYPTO",
                    "symbol": "KRW-BTC",
                    "name": "비트코인",
                    "quantity": 0.00001,
                    "avg_price": 35_000_000.0,
                    "evaluation": 350.0,
                    "profit_rate": 0.10,
                    "profit_loss": 35.0,
                    "dust": True,
                },
                {
                    "market_type": "CRYPTO",
                    "symbol": "KRW-ETH",
                    "name": "이더리움",
                    "quantity": 0.1,
                    "avg_price": 4_000_000.0,
                    "evaluation": 450_000.0,
                    "profit_rate": 0.05,
                    "profit_loss": 22_500.0,
                    "dust": False,
                },
                {
                    "market_type": "CRYPTO",
                    "symbol": "KRW-XRP",
                    "name": "리플",
                    "quantity": 100.0,
                    "avg_price": 1000.0,
                    "evaluation": 90_000.0,
                    "profit_rate": -0.10,
                    "profit_loss": -10_000.0,
                    "dust": False,
                },
            ]
        }
    )

    gainers = [row["symbol"] for row in summary["crypto"]["top_gainers"]]
    losers = [row["symbol"] for row in summary["crypto"]["top_losers"]]
    assert "KRW-BTC" not in gainers
    assert "KRW-BTC" not in losers
