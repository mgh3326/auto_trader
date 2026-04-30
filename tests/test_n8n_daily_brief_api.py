from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_brief_result() -> dict:
    from app.schemas.n8n.common import N8nMarketOverview

    return {
        "success": True,
        "as_of": "2026-03-17T08:30:00+09:00",
        "date_fmt": "03/17 (화)",
        "market_overview": N8nMarketOverview(
            fear_greed=None,
            btc_dominance=56.64,
            total_market_cap_change_24h=3.86,
            economic_events_today=[],
        ),
        "pending_orders": {"crypto": None, "kr": None, "us": None},
        "portfolio_summary": {"crypto": None, "kr": None, "us": None},
        "yesterday_fills": {"total": 0, "fills": []},
        "brief_text": "📋 Daily Trading Brief — 03/17 (화)\n...",
        "errors": [],
    }


@pytest.mark.integration
class TestDailyBriefEndpoint:
    def _get_client(self) -> TestClient:
        app = FastAPI()
        from app.routers.n8n import router

        app.include_router(router)
        return TestClient(app)

    def test_daily_brief_default_params(self):
        client = self._get_client()
        with patch(
            "app.routers.n8n.fetch_daily_brief",
            new_callable=AsyncMock,
            return_value=_make_brief_result(),
        ):
            resp = client.get("/api/n8n/daily-brief")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "brief_text" in body
        assert "market_overview" in body

    def test_daily_brief_preserves_daily_burn_error_payload(self):
        client = self._get_client()
        result = _make_brief_result()
        result["daily_burn"] = {
            "daily_burn_krw": 0.0,
            "active_count": 0,
            "error": "db unavailable",
        }
        result["errors"] = [{"source": "daily_burn", "error": "db unavailable"}]
        result["brief_text"] = "daily_burn: unavailable (active DCA 재산출 실패)"

        with patch(
            "app.routers.n8n.fetch_daily_brief",
            new_callable=AsyncMock,
            return_value=result,
        ):
            resp = client.get("/api/n8n/daily-brief")

        assert resp.status_code == 200
        body = resp.json()
        assert body["daily_burn"]["error"] == "db unavailable"
        assert {"source": "daily_burn", "error": "db unavailable"} in body["errors"]
        assert "daily_burn: unavailable" in body["brief_text"]

    def test_daily_brief_custom_markets(self):
        client = self._get_client()
        with patch(
            "app.routers.n8n.fetch_daily_brief",
            new_callable=AsyncMock,
            return_value=_make_brief_result(),
        ) as mock_fetch:
            resp = client.get("/api/n8n/daily-brief?markets=crypto,kr")
        assert resp.status_code == 200
        call_kwargs = mock_fetch.call_args.kwargs
        assert call_kwargs["markets"] == ["crypto", "kr"]

    def test_daily_brief_error_returns_500(self):
        client = self._get_client()
        with patch(
            "app.routers.n8n.fetch_daily_brief",
            new_callable=AsyncMock,
            side_effect=Exception("total failure"),
        ):
            resp = client.get("/api/n8n/daily-brief")
        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
