from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.core.timezone import KST
from app.schemas.n8n import N8nKrMorningReportResponse
from app.services.n8n_kr_morning_report_service import (
    _build_brief_text,
    fetch_kr_morning_report,
)


def test_kr_morning_report_schema_accepts_manual_toss_cash():
    payload = N8nKrMorningReportResponse(
        success=True,
        as_of="2026-03-19T08:50:00+09:00",
        date_fmt="03/19 (목)",
        cash_balance={
            "kis_krw": 45000,
            "kis_krw_fmt": "4.5만",
            "toss_krw": None,
            "toss_krw_fmt": "수동 관리",
            "total_krw": 45000,
            "total_krw_fmt": "4.5만",
        },
    )

    assert payload.cash_balance.toss_krw is None
    assert payload.cash_balance.toss_krw_fmt == "수동 관리"


@pytest.mark.asyncio
async def test_fetch_kr_morning_report_groups_kis_and_toss_kr_holdings():
    as_of = datetime(2026, 3, 19, 8, 50, tzinfo=KST)
    overview = {
        "positions": [
            {
                "market_type": "KR",
                "symbol": "005930",
                "name": "삼성전자",
                "quantity": 10,
                "avg_price": 70000,
                "current_price": 68000,
                "evaluation": 680000,
                "profit_rate": -0.0286,
                "broker": "kis",
            },
            {
                "market_type": "KR",
                "symbol": "000660",
                "name": "SK하이닉스",
                "quantity": 3,
                "avg_price": 200000,
                "current_price": 210000,
                "evaluation": 630000,
                "profit_rate": 0.05,
                "broker": "toss",
            },
            {
                "market_type": "US",
                "symbol": "AAPL",
                "name": "Apple",
                "quantity": 1,
                "avg_price": 200,
                "current_price": 205,
                "evaluation": 205,
                "profit_rate": 0.025,
                "broker": "kis",
            },
        ]
    }

    with (
        patch(
            "app.services.n8n_kr_morning_report_service._get_portfolio_overview",
            new_callable=AsyncMock,
            return_value=overview,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_kis_cash_balance",
            new_callable=AsyncMock,
            return_value=45000.0,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value={"total": 0, "buy_count": 0, "sell_count": 0, "orders": []},
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_screening",
            new_callable=AsyncMock,
            return_value={
                "total_scanned": 0,
                "top_n": 20,
                "strategy": None,
                "results": [],
                "summary": {},
            },
        ),
    ):
        result = await fetch_kr_morning_report(as_of=as_of)

    assert result["holdings"]["kis"]["total_count"] == 1
    assert result["holdings"]["toss"]["total_count"] == 1
    assert result["holdings"]["combined"]["total_count"] == 2
    assert result["cash_balance"]["kis_krw"] == 45000.0
    assert result["cash_balance"]["toss_krw"] is None
    assert result["cash_balance"]["toss_krw_fmt"] == "수동 관리"


@pytest.mark.asyncio
async def test_fetch_kr_morning_report_returns_zeroed_holdings_when_no_kr_positions():
    as_of = datetime(2026, 3, 19, 8, 50, tzinfo=KST)
    overview = {
        "positions": [
            {
                "market_type": "US",
                "symbol": "AAPL",
                "name": "Apple",
                "quantity": 1,
                "avg_price": 200,
                "current_price": 205,
                "evaluation": 205,
                "profit_rate": 0.025,
                "broker": "kis",
            },
        ]
    }

    with (
        patch(
            "app.services.n8n_kr_morning_report_service._get_portfolio_overview",
            new_callable=AsyncMock,
            return_value=overview,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_kis_cash_balance",
            new_callable=AsyncMock,
            return_value=0.0,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value={"total": 0, "buy_count": 0, "sell_count": 0, "orders": []},
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_screening",
            new_callable=AsyncMock,
            return_value={
                "total_scanned": 0,
                "top_n": 20,
                "strategy": None,
                "results": [],
                "summary": {},
            },
        ),
    ):
        result = await fetch_kr_morning_report(as_of=as_of)

    assert result["holdings"]["combined"]["total_count"] == 0
    assert result["holdings"]["combined"]["total_eval_fmt"] == "0"


@pytest.mark.asyncio
async def test_fetch_kr_morning_report_skips_screening_when_disabled():
    with (
        patch(
            "app.services.n8n_kr_morning_report_service._get_portfolio_overview",
            new_callable=AsyncMock,
            return_value={"positions": []},
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_kis_cash_balance",
            new_callable=AsyncMock,
            return_value=45000.0,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value={"total": 0, "buy_count": 0, "sell_count": 0, "orders": []},
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_screening",
            new_callable=AsyncMock,
        ) as screen_mock,
    ):
        result = await fetch_kr_morning_report(include_screen=False)

    screen_mock.assert_not_called()
    assert result["screening"]["results"] == []


@pytest.mark.asyncio
async def test_fetch_kr_morning_report_sorts_screening_by_lowest_rsi_and_trims_top_n():
    raw_results = {
        "results": [
            {"symbol": "A", "name": "A", "current_price": 1000, "rsi": 40},
            {"symbol": "B", "name": "B", "current_price": 1000, "rsi": 22},
            {"symbol": "C", "name": "C", "current_price": 1000, "rsi": 31},
        ],
        "total_count": 100,
    }
    with (
        patch(
            "app.services.n8n_kr_morning_report_service._get_portfolio_overview",
            new_callable=AsyncMock,
            return_value={"positions": []},
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_kis_cash_balance",
            new_callable=AsyncMock,
            return_value=45000.0,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value={"total": 0, "buy_count": 0, "sell_count": 0, "orders": []},
        ),
        patch(
            "app.services.n8n_kr_morning_report_service.screen_stocks_impl",
            new_callable=AsyncMock,
            return_value=raw_results,
        ),
    ):
        result = await fetch_kr_morning_report(top_n=2)

    assert [row["symbol"] for row in result["screening"]["results"]] == ["B", "C"]
    assert len(result["screening"]["results"]) == 2


def test_build_brief_text_formats_manual_toss_cash_label():
    text = _build_brief_text(
        date_fmt="03/19 (목)",
        holdings={
            "kis": {
                "total_eval_fmt": "100만",
                "total_pnl_fmt": "+1.0%",
                "total_count": 1,
            },
            "toss": {
                "total_eval_fmt": "50만",
                "total_pnl_fmt": "-2.0%",
                "total_count": 2,
            },
            "combined": {"total_eval_fmt": "150만", "total_pnl_fmt": "+0.5%"},
        },
        cash_balance={
            "kis_krw_fmt": "4.5만",
            "toss_krw_fmt": "수동 관리",
            "total_krw_fmt": "4.5만",
        },
        screening={"results": []},
        pending_orders={"total": 0},
        include_screen=True,
        include_pending=True,
    )

    assert "토스: 수동 관리" in text
    assert text.startswith("📊 KR 모닝 리포트 — 03/19 (목)")


@pytest.mark.asyncio
async def test_fetch_kr_morning_report_groups_holdings_from_components_payload():
    as_of = datetime(2026, 3, 19, 8, 50, tzinfo=KST)
    overview = {
        "positions": [
            {
                "market_type": "KR",
                "symbol": "005930",
                "name": "삼성전자",
                "quantity": 10,
                "avg_price": 70000,
                "current_price": 68000,
                "evaluation": 680000,
                "profit_loss": -20000,
                "profit_rate": -0.0286,
                "components": [
                    {
                        "account_key": "live:kis",
                        "broker": "kis",
                        "account_name": "KIS 실계좌",
                        "source": "live",
                        "quantity": 10,
                        "avg_price": 70000,
                        "current_price": 68000,
                        "evaluation": 680000,
                        "profit_loss": -20000,
                        "profit_rate": -0.0286,
                    }
                ],
            },
            {
                "market_type": "KR",
                "symbol": "000660",
                "name": "SK하이닉스",
                "quantity": 3,
                "avg_price": 200000,
                "current_price": 210000,
                "evaluation": 630000,
                "profit_loss": 30000,
                "profit_rate": 0.05,
                "components": [
                    {
                        "account_key": "manual:12",
                        "broker": "toss",
                        "account_name": "토스",
                        "source": "manual",
                        "quantity": 3,
                        "avg_price": 200000,
                        "current_price": 210000,
                        "evaluation": 630000,
                        "profit_loss": 30000,
                        "profit_rate": 0.05,
                    }
                ],
            },
        ]
    }

    with (
        patch(
            "app.services.n8n_kr_morning_report_service._get_portfolio_overview",
            new_callable=AsyncMock,
            return_value=overview,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_kis_cash_balance",
            new_callable=AsyncMock,
            return_value=45000.0,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value={"total": 0, "buy_count": 0, "sell_count": 0, "orders": []},
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_screening",
            new_callable=AsyncMock,
            return_value={
                "total_scanned": 0,
                "top_n": 20,
                "strategy": None,
                "results": [],
                "summary": {},
            },
        ),
    ):
        result = await fetch_kr_morning_report(as_of=as_of)

    assert result["holdings"]["kis"]["total_count"] == 1
    assert result["holdings"]["toss"]["total_count"] == 1


@pytest.mark.asyncio
async def test_fetch_kr_morning_report_reads_pending_totals_from_summary():
    as_of = datetime(2026, 3, 19, 8, 50, tzinfo=KST)
    pending_payload = {
        "success": True,
        "market": "kr",
        "orders": [
            {
                "symbol": "005930",
                "summary_line": "삼성전자(005930) sell @7.00만 ...",
            }
        ],
        "summary": {
            "total": 1,
            "buy_count": 0,
            "sell_count": 1,
            "total_buy_fmt": "0",
            "total_sell_fmt": "70.0만",
        },
        "errors": [],
    }

    with (
        patch(
            "app.services.n8n_kr_morning_report_service._get_portfolio_overview",
            new_callable=AsyncMock,
            return_value={"positions": []},
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_kis_cash_balance",
            new_callable=AsyncMock,
            return_value=45000.0,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value=pending_payload,
        ),
        patch(
            "app.services.n8n_kr_morning_report_service._fetch_screening",
            new_callable=AsyncMock,
            return_value={
                "total_scanned": 0,
                "top_n": 20,
                "strategy": None,
                "results": [],
                "summary": {},
            },
        ),
    ):
        result = await fetch_kr_morning_report(as_of=as_of)

    assert result["pending_orders"]["total"] == 1
    assert result["pending_orders"]["sell_count"] == 1
