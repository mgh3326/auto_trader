"""hk 6503: screener wrapper behaviour with the store replaced (no database)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from app.mcp_server.tooling.screening import trading_flag_filter as tff
from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.store import FlagRecord

pytestmark = pytest.mark.unit


@asynccontextmanager
async def _fake_session():
    yield object()


def _response(*codes: str) -> dict[str, Any]:
    return {
        "results": [{"code": c, "name": c} for c in codes],
        "total_count": len(codes),
        "returned_count": len(codes),
        "meta": {},
    }


def _record(category: str, state: str) -> FlagRecord:
    return FlagRecord(
        category=category,
        state=state,
        raw_code=None,
        reason_code="6045" if state == "designated" else None,
        effective_date=None,
        source=v.SOURCE_NAVER_INTRADAY,
        observed_at=datetime(2026, 11, 25, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_non_kr_market_is_returned_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: Any, **_k: Any):  # pragma: no cover - must not be reached
        raise AssertionError("no DB read for non-KR markets")

    monkeypatch.setattr(tff, "AsyncSessionLocal", _boom)
    response = _response("AAPL")
    assert await tff.exclude_trading_flag_rows(response, market="us") is response


@pytest.mark.asyncio
async def test_management_row_is_excluded_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _load(_db, symbols):
        assert list(symbols) == ["224060", "005930"]
        return {
            "224060": {
                v.CATEGORY_MANAGEMENT: _record(v.CATEGORY_MANAGEMENT, "designated")
            }
        }

    monkeypatch.setattr(tff, "AsyncSessionLocal", _fake_session)
    monkeypatch.setattr(tff, "load_flag_records", _load)

    out = await tff.exclude_trading_flag_rows(
        _response("224060", "005930"), market="kr"
    )
    assert [r["code"] for r in out["results"]] == ["005930"]
    assert out["total_count"] == 1
    assert out["returned_count"] == 1
    (excluded,) = out["meta"]["trading_flag_excluded"]
    assert excluded["symbol"] == "224060"
    assert excluded["reasons"][0]["reason"] == "management_designated"


@pytest.mark.asyncio
async def test_store_failure_fails_open_and_keeps_every_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _broken():
        raise ConnectionError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(tff, "AsyncSessionLocal", _broken)
    response = _response("224060", "005930")
    out = await tff.exclude_trading_flag_rows(response, market="kr")
    assert [r["code"] for r in out["results"]] == ["224060", "005930"]
    assert out["meta"]["trading_flags_check_skipped"] == "ConnectionError"
    assert "trading_flag_excluded" not in out["meta"]
    assert "warnings" not in out


@pytest.mark.asyncio
async def test_legacy_isin_row_is_looked_up_by_short_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def _load(_db, symbols):
        seen.extend(symbols)
        return {
            "005930": {
                v.CATEGORY_MANAGEMENT: _record(v.CATEGORY_MANAGEMENT, "designated")
            }
        }

    monkeypatch.setattr(tff, "AsyncSessionLocal", _fake_session)
    monkeypatch.setattr(tff, "load_flag_records", _load)
    response = {
        "results": [{"code": "KR7005930003", "short_code": "005930"}],
        "total_count": 1,
        "returned_count": 1,
        "meta": {},
    }
    out = await tff.exclude_trading_flag_rows(response, market="kr")
    assert seen == ["005930"]
    assert out["results"] == []
    assert out["meta"]["trading_flag_excluded"][0]["symbol"] == "005930"
