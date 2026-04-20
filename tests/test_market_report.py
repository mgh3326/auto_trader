# tests/test_market_report.py
"""Tests for market_reports pipeline — model, service, MCP tools."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.market_report import MarketReport


class TestMarketReportModel:
    """MarketReport 모델 기본 테스트."""

    def test_create_minimal_report(self) -> None:
        report = MarketReport(
            report_type="daily_brief",
            report_date=date(2026, 4, 15),
            market="all",
            content={"success": True, "brief_text": "hello"},
            created_at=datetime(2026, 4, 15, 9, 0),
        )
        assert report.report_type == "daily_brief"
        assert report.report_date == date(2026, 4, 15)
        assert report.market == "all"
        assert report.content == {"success": True, "brief_text": "hello"}
        assert report.title is None
        assert report.summary is None
        assert report.metadata_ is None

    def test_create_full_report(self) -> None:
        report = MarketReport(
            report_type="kr_morning",
            report_date=date(2026, 4, 15),
            market="kr",
            content={"holdings": [], "screening": {}},
            title="KR Morning Report — 04/15 (화)",
            summary="오늘의 한국 주식 시장 요약",
            metadata_={"source": "n8n", "version": 2},
            user_id=1,
            created_at=datetime(2026, 4, 15, 8, 0),
            updated_at=datetime(2026, 4, 15, 8, 30),
        )
        assert report.title == "KR Morning Report — 04/15 (화)"
        assert report.summary == "오늘의 한국 주식 시장 요약"
        assert report.metadata_ == {"source": "n8n", "version": 2}
        assert report.user_id == 1
        assert report.updated_at is not None

    def test_table_args(self) -> None:
        assert MarketReport.__tablename__ == "market_reports"

    def test_repr(self) -> None:
        report = MarketReport(
            id=1,
            report_type="crypto_scan",
            report_date=date(2026, 4, 15),
            market="crypto",
            content={},
            created_at=datetime(2026, 4, 15, 9, 0),
        )
        assert "crypto_scan" in repr(report)
        assert "crypto" in repr(report)


class TestReportToDict:
    """_report_to_dict 직렬화 테스트."""

    def test_serializes_all_fields(self) -> None:
        from app.services.market_report_service import _report_to_dict

        report = MarketReport(
            id=5,
            report_type="daily_brief",
            report_date=date(2026, 4, 15),
            market="all",
            content={"brief_text": "hello"},
            title="Daily Brief — 04/15",
            summary="Summary text",
            metadata_={"source": "test"},
            created_at=datetime(2026, 4, 15, 9, 0),
            updated_at=datetime(2026, 4, 15, 9, 30),
        )

        d = _report_to_dict(report)
        assert d["id"] == 5
        assert d["report_type"] == "daily_brief"
        assert d["report_date"] == "2026-04-15"
        assert d["market"] == "all"
        assert d["content"] == {"brief_text": "hello"}
        assert d["title"] == "Daily Brief — 04/15"
        assert d["summary"] == "Summary text"
        assert d["metadata"] == {"source": "test"}
        assert d["created_at"] == "2026-04-15T09:00:00"
        assert d["updated_at"] == "2026-04-15T09:30:00"

    def test_serializes_none_timestamps(self) -> None:
        from app.services.market_report_service import _report_to_dict

        report = MarketReport(
            id=6,
            report_type="crypto_scan",
            report_date=date(2026, 4, 15),
            market="crypto",
            content={"coins": []},
            created_at=datetime(2026, 4, 15, 9, 0),
            updated_at=None,
        )

        d = _report_to_dict(report)
        assert d["updated_at"] is None
        assert d["metadata"] is None


class TestSerializeResult:
    """_serialize_result 재귀 변환 테스트."""

    def test_plain_dict_passthrough(self) -> None:
        from app.services.market_report_service import _serialize_result

        inp = {"key": "value", "num": 42}
        out = _serialize_result(inp)
        assert out == {"key": "value", "num": 42}

    def test_errors_field_stringified(self) -> None:
        from app.services.market_report_service import _serialize_result

        inp = {"errors": [{"code": 500, "msg": "fail"}, "raw error"]}
        out = _serialize_result(inp)
        assert out["errors"][0] == {"code": "500", "msg": "fail"}
        assert out["errors"][1] == "raw error"

    def test_pydantic_model_dump(self) -> None:
        from app.services.market_report_service import _serialize_result

        mock_model = MagicMock()
        mock_model.model_dump.return_value = {"field": "val"}
        inp = {"data": mock_model}
        out = _serialize_result(inp)
        assert out["data"] == {"field": "val"}

    def test_nested_dict_recursion(self) -> None:
        from app.services.market_report_service import _serialize_result

        inp = {"outer": {"inner": "value"}}
        out = _serialize_result(inp)
        assert out["outer"]["inner"] == "value"

    def test_list_with_pydantic(self) -> None:
        from app.services.market_report_service import _serialize_result

        mock_model = MagicMock()
        mock_model.model_dump.return_value = {"a": 1}
        inp = {"items": [mock_model, "plain"]}
        out = _serialize_result(inp)
        assert out["items"] == [{"a": 1}, "plain"]


class TestSaveDailyBriefReport:
    """save_daily_brief_report DB 저장 테스트."""

    @pytest.mark.asyncio
    async def test_saves_with_valid_as_of(self) -> None:
        from app.services.market_report_service import save_daily_brief_report

        with patch(
            "app.services.market_report_service.upsert_market_report",
            new_callable=AsyncMock,
            return_value=1,
        ) as mock_upsert:
            await save_daily_brief_report(
                {
                    "as_of": "2026-04-15T09:00:00+09:00",
                    "date_fmt": "04/15 (화)",
                    "brief_text": "Daily summary",
                    "success": True,
                }
            )

        mock_upsert.assert_called_once()
        kwargs = mock_upsert.call_args.kwargs
        assert kwargs["report_type"] == "daily_brief"
        assert kwargs["report_date"] == date(2026, 4, 15)
        assert kwargs["market"] == "all"
        assert kwargs["title"] == "Daily Brief — 04/15 (화)"
        assert kwargs["summary"] == "Daily summary"

    @pytest.mark.asyncio
    async def test_fallback_date_on_invalid_as_of(self) -> None:
        from app.services.market_report_service import save_daily_brief_report

        with patch(
            "app.services.market_report_service.upsert_market_report",
            new_callable=AsyncMock,
            return_value=1,
        ) as mock_upsert:
            await save_daily_brief_report({"as_of": "INVALID", "brief_text": "test"})

        kwargs = mock_upsert.call_args.kwargs
        assert isinstance(kwargs["report_date"], date)

    @pytest.mark.asyncio
    async def test_exception_does_not_propagate(self) -> None:
        from app.services.market_report_service import save_daily_brief_report

        with patch(
            "app.services.market_report_service.upsert_market_report",
            new_callable=AsyncMock,
            side_effect=Exception("DB error"),
        ):
            await save_daily_brief_report({"as_of": "2026-04-15T09:00:00+09:00"})


class TestSaveKrMorningReport:
    """save_kr_morning_report DB 저장 테스트."""

    @pytest.mark.asyncio
    async def test_saves_kr_morning(self) -> None:
        from app.services.market_report_service import save_kr_morning_report

        with patch(
            "app.services.market_report_service.upsert_market_report",
            new_callable=AsyncMock,
            return_value=2,
        ) as mock_upsert:
            await save_kr_morning_report(
                {
                    "as_of": "2026-04-15T08:30:00+09:00",
                    "date_fmt": "04/15 (화)",
                    "brief_text": "KR morning summary",
                }
            )

        kwargs = mock_upsert.call_args.kwargs
        assert kwargs["report_type"] == "kr_morning"
        assert kwargs["market"] == "kr"
        assert kwargs["title"] == "KR Morning Report — 04/15 (화)"
        assert kwargs["summary"] == "KR morning summary"


class TestSaveCryptoScanReport:
    """save_crypto_scan_report DB 저장 테스트."""

    @pytest.mark.asyncio
    async def test_saves_crypto_scan(self) -> None:
        from app.services.market_report_service import save_crypto_scan_report

        with patch(
            "app.services.market_report_service.upsert_market_report",
            new_callable=AsyncMock,
            return_value=3,
        ) as mock_upsert:
            await save_crypto_scan_report({"coins": [], "summary": "no signals"})

        kwargs = mock_upsert.call_args.kwargs
        assert kwargs["report_type"] == "crypto_scan"
        assert kwargs["market"] == "crypto"
        assert kwargs["summary"] is None


class TestGetMarketReports:
    """get_market_reports 조회 테스트."""

    @pytest.mark.asyncio
    async def test_returns_filtered_reports(self) -> None:
        from app.services.market_report_service import get_market_reports

        report = MarketReport(
            id=1,
            report_type="daily_brief",
            report_date=date(2026, 4, 15),
            market="all",
            content={"brief_text": "hello"},
            title="Daily Brief",
            summary="summary",
            created_at=datetime(2026, 4, 15, 9, 0),
            updated_at=None,
        )

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [report]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = mock_session
        session_cm.__aexit__.return_value = None

        with patch(
            "app.services.market_report_service.AsyncSessionLocal",
            return_value=session_cm,
        ):
            reports = await get_market_reports(report_type="daily_brief", days=7)

        assert len(reports) == 1
        assert reports[0]["report_type"] == "daily_brief"
        assert reports[0]["market"] == "all"

    @pytest.mark.asyncio
    async def test_returns_empty_list(self) -> None:
        from app.services.market_report_service import get_market_reports

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = mock_session
        session_cm.__aexit__.return_value = None

        with patch(
            "app.services.market_report_service.AsyncSessionLocal",
            return_value=session_cm,
        ):
            reports = await get_market_reports(report_type="kr_morning", market="kr")

        assert reports == []


class TestGetLatestMarketBrief:
    """get_latest_market_brief 조회 테스트."""

    @pytest.mark.asyncio
    async def test_returns_latest_brief(self) -> None:
        from app.services.market_report_service import get_latest_market_brief

        report = MarketReport(
            id=10,
            report_type="daily_brief",
            report_date=date(2026, 4, 15),
            market="all",
            content={"brief_text": "latest"},
            created_at=datetime(2026, 4, 15, 9, 0),
            updated_at=None,
        )

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = report
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = mock_session
        session_cm.__aexit__.return_value = None

        with patch(
            "app.services.market_report_service.AsyncSessionLocal",
            return_value=session_cm,
        ):
            result = await get_latest_market_brief(market="all")

        assert result is not None
        assert result["report_type"] == "daily_brief"
        assert result["content"] == {"brief_text": "latest"}

    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self) -> None:
        from app.services.market_report_service import get_latest_market_brief

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = mock_session
        session_cm.__aexit__.return_value = None

        with patch(
            "app.services.market_report_service.AsyncSessionLocal",
            return_value=session_cm,
        ):
            result = await get_latest_market_brief(market="us")

        assert result is None


class TestMCPGetMarketReports:
    """MCP get_market_reports 도구 테스트."""

    @pytest.mark.asyncio
    async def test_returns_count_and_reports(self) -> None:
        from app.mcp_server.tooling.market_report_handlers import (
            _get_market_reports_impl,
        )

        mock_reports = [
            {"id": 1, "report_type": "daily_brief", "market": "all"},
            {"id": 2, "report_type": "kr_morning", "market": "kr"},
        ]

        with patch(
            "app.mcp_server.tooling.market_report_handlers.get_market_reports",
            new_callable=AsyncMock,
            return_value=mock_reports,
        ):
            result = await _get_market_reports_impl(report_type="daily_brief", days=7)

        assert result["count"] == 2
        assert result["reports"] == mock_reports

    @pytest.mark.asyncio
    async def test_defaults_days_and_limit(self) -> None:
        from app.mcp_server.tooling.market_report_handlers import (
            _get_market_reports_impl,
        )

        with patch(
            "app.mcp_server.tooling.market_report_handlers.get_market_reports",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_get:
            await _get_market_reports_impl(days=None, limit=None)

        kwargs = mock_get.call_args.kwargs
        assert kwargs["days"] == 7
        assert kwargs["limit"] == 10

    @pytest.mark.asyncio
    async def test_empty_results(self) -> None:
        from app.mcp_server.tooling.market_report_handlers import (
            _get_market_reports_impl,
        )

        with patch(
            "app.mcp_server.tooling.market_report_handlers.get_market_reports",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await _get_market_reports_impl()

        assert result["count"] == 0
        assert result["reports"] == []


class TestMCPGetLatestMarketBrief:
    """MCP get_latest_market_brief 도구 테스트."""

    @pytest.mark.asyncio
    async def test_found_report(self) -> None:
        from app.mcp_server.tooling.market_report_handlers import (
            _get_latest_market_brief_impl,
        )

        mock_report = {"id": 1, "report_type": "daily_brief", "market": "all"}

        with patch(
            "app.mcp_server.tooling.market_report_handlers.get_latest_market_brief",
            new_callable=AsyncMock,
            return_value=mock_report,
        ):
            result = await _get_latest_market_brief_impl(market="all")

        assert result["found"] is True
        assert result["report"] == mock_report

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        from app.mcp_server.tooling.market_report_handlers import (
            _get_latest_market_brief_impl,
        )

        with patch(
            "app.mcp_server.tooling.market_report_handlers.get_latest_market_brief",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await _get_latest_market_brief_impl(market="us")

        assert result["found"] is False
        assert result["report"] is None

    @pytest.mark.asyncio
    async def test_defaults_market_to_all(self) -> None:
        from app.mcp_server.tooling.market_report_handlers import (
            _get_latest_market_brief_impl,
        )

        with patch(
            "app.mcp_server.tooling.market_report_handlers.get_latest_market_brief",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_get:
            await _get_latest_market_brief_impl(market=None)

        kwargs = mock_get.call_args.kwargs
        assert kwargs["market"] == "all"


class TestMCPToolRegistration:
    """MCP 도구 등록 테스트."""

    def test_market_report_tools_registered(self) -> None:
        from tests._mcp_tooling_support import build_tools

        tools = build_tools()
        assert "get_market_reports" in tools
        assert "get_latest_market_brief" in tools

    def test_tool_names_constant(self) -> None:
        from app.mcp_server.tooling.market_report_handlers import (
            MARKET_REPORT_TOOL_NAMES,
        )

        assert "get_market_reports" in MARKET_REPORT_TOOL_NAMES
        assert "get_latest_market_brief" in MARKET_REPORT_TOOL_NAMES


class TestN8nEndpointDbSave:
    """n8n 엔드포인트에서 DB 저장 호출 확인."""

    @pytest.mark.integration
    def test_daily_brief_triggers_save(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.schemas.n8n.common import N8nMarketOverview

        app = FastAPI()
        from app.routers.n8n import router

        app.include_router(router)
        client = TestClient(app)

        result = {
            "success": True,
            "as_of": "2026-04-15T09:00:00+09:00",
            "date_fmt": "04/15 (화)",
            "market_overview": N8nMarketOverview(
                fear_greed=None,
                btc_dominance=56.64,
                total_market_cap_change_24h=3.86,
                economic_events_today=[],
            ),
            "pending_orders": {"crypto": None, "kr": None, "us": None},
            "portfolio_summary": {"crypto": None, "kr": None, "us": None},
            "yesterday_fills": {"total": 0, "fills": []},
            "brief_text": "Daily Brief text",
            "errors": [],
        }

        with (
            patch(
                "app.routers.n8n.fetch_daily_brief",
                new_callable=AsyncMock,
                return_value=result,
            ),
            patch(
                "app.routers.n8n.save_daily_brief_report",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            resp = client.get("/api/n8n/daily-brief")

        assert resp.status_code == 200
        mock_save.assert_called_once()

    @pytest.mark.integration
    def test_kr_morning_report_triggers_save(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        from app.routers.n8n import router

        app.include_router(router)
        client = TestClient(app)

        result = {
            "success": True,
            "as_of": "2026-04-15T08:30:00+09:00",
            "date_fmt": "04/15 (화)",
            "holdings": {},
            "cash_balance": {},
            "screening": {},
            "pending_orders": {},
            "brief_text": "KR Morning text",
            "errors": [],
        }

        with (
            patch(
                "app.routers.n8n.fetch_kr_morning_report",
                new_callable=AsyncMock,
                return_value=result,
            ),
            patch(
                "app.routers.n8n.save_kr_morning_report",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            resp = client.get("/api/n8n/kr-morning-report")

        assert resp.status_code == 200
        mock_save.assert_called_once()

    @pytest.mark.integration
    def test_crypto_scan_triggers_save(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        from app.routers.n8n import router

        app.include_router(router)
        client = TestClient(app)

        result = {
            "success": True,
            "as_of": "2026-04-15T09:00:00+09:00",
            "scan_params": {},
            "btc_context": None,
            "fear_greed": None,
            "coins": [],
            "summary": {"total_scanned": 30, "top_n_count": 20, "holdings_added": 5},
            "errors": [],
        }

        with (
            patch(
                "app.routers.n8n.fetch_crypto_scan",
                new_callable=AsyncMock,
                return_value=result,
            ),
            patch(
                "app.routers.n8n.save_crypto_scan_report",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            resp = client.get("/api/n8n/crypto-scan")

        assert resp.status_code == 200
        mock_save.assert_called_once()
