from app.schemas.n8n import N8nKrMorningReportResponse


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


import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from app.core.timezone import KST


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
            return_value={"total_scanned": 0, "top_n": 20, "strategy": None, "results": [], "summary": {}},
        ),
    ):
        from app.services.n8n_kr_morning_report_service import fetch_kr_morning_report

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
            return_value={"total_scanned": 0, "top_n": 20, "strategy": None, "results": [], "summary": {}},
        ),
    ):
        from app.services.n8n_kr_morning_report_service import fetch_kr_morning_report

        result = await fetch_kr_morning_report(as_of=as_of)

    assert result["holdings"]["combined"]["total_count"] == 0
    assert result["holdings"]["combined"]["total_eval_fmt"] == "0"
