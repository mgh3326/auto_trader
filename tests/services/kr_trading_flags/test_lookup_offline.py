"""hk 6503: confirmed-halt lookup for the halted_suspect path (no database)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.kr_trading_flags import lookup
from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.store import FlagRecord

pytestmark = pytest.mark.unit


@asynccontextmanager
async def _session():
    yield object()


def _halt(state: str) -> FlagRecord:
    return FlagRecord(
        category=v.CATEGORY_TRADING_HALT,
        state=state,
        raw_code="Y",
        reason_code="625",
        effective_date=None,
        source=v.SOURCE_NAVER_INTRADAY,
        observed_at=datetime(2026, 11, 25, 10, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_stored_halted_flag_is_returned_with_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _load(_db, _symbols: Any):
        return {"031860": {v.CATEGORY_TRADING_HALT: _halt("halted")}}

    monkeypatch.setattr(lookup, "AsyncSessionLocal", _session)
    monkeypatch.setattr(lookup, "load_flag_records", _load)
    evidence = await lookup.confirmed_halt_flag("031860")
    assert evidence is not None
    assert evidence["state"] == "halted"
    assert evidence["reason_code"] == "625"
    assert evidence["source"] == v.SOURCE_NAVER_INTRADAY


@pytest.mark.asyncio
async def test_no_halted_row_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _load(_db, _symbols: Any):
        return {"031860": {v.CATEGORY_TRADING_HALT: _halt("none")}}

    monkeypatch.setattr(lookup, "AsyncSessionLocal", _session)
    monkeypatch.setattr(lookup, "load_flag_records", _load)
    assert await lookup.confirmed_halt_flag("031860") is None


@pytest.mark.asyncio
async def test_read_failure_fails_open_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def _broken():
        raise ConnectionError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(lookup, "AsyncSessionLocal", _broken)
    assert await lookup.confirmed_halt_flag("031860") is None
