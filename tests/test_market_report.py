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
