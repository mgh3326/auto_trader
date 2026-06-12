from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.services.brokers.toss.dto import TossWarningInfo
from app.services.brokers.toss.warnings_guard import check_warnings_guard


class DummyClient:
    def __init__(self, warnings_result=None, should_delay=False, should_fail=False):
        self.warnings_result = warnings_result or []
        self.should_delay = should_delay
        self.should_fail = should_fail
        self.calls = []

    async def warnings(self, symbol: str) -> list[TossWarningInfo]:
        self.calls.append(symbol)
        if self.should_fail:
            raise RuntimeError("API failure")
        if self.should_delay:
            await asyncio.sleep(5.0)
        return self.warnings_result


@pytest.mark.asyncio
async def test_warnings_guard_skips_non_kr() -> None:
    # US stock symbol
    client = DummyClient()
    result = await check_warnings_guard(client, "AAPL")
    assert result.ok is True
    assert result.warnings == []
    assert len(client.calls) == 0

    # Explicitly US market
    result = await check_warnings_guard(client, "005930", market="us")
    assert result.ok is True
    assert result.warnings == []
    assert len(client.calls) == 0


@pytest.mark.asyncio
async def test_warnings_guard_kr_no_warnings() -> None:
    client = DummyClient(warnings_result=[])
    result = await check_warnings_guard(client, "005930")
    assert result.ok is True
    assert result.warnings == []
    assert client.calls == ["005930"]


@pytest.mark.asyncio
async def test_warnings_guard_kr_non_blocking_warnings() -> None:
    warnings_list = [
        TossWarningInfo(
            warning_type="OVERHEATED",
            exchange="KRX",
            start_date="2026-06-12",
            end_date=None,
        ),
        TossWarningInfo(
            warning_type="VI_STATIC",
            exchange="KRX",
            start_date="2026-06-12",
            end_date=None,
        ),
    ]
    client = DummyClient(warnings_result=warnings_list)
    result = await check_warnings_guard(client, "005930")
    assert result.ok is True
    assert result.warnings == warnings_list
    assert result.error_message is None


@pytest.mark.asyncio
async def test_warnings_guard_kr_blocking_warning() -> None:
    warnings_list = [
        TossWarningInfo(
            warning_type="LIQUIDATION_TRADING",
            exchange="KRX",
            start_date="2026-06-12",
            end_date=None,
        )
    ]
    client = DummyClient(warnings_result=warnings_list)
    result = await check_warnings_guard(client, "005930")
    assert result.ok is False
    assert result.warnings == warnings_list
    assert "LIQUIDATION_TRADING" in result.error_message


@pytest.mark.asyncio
async def test_warnings_guard_filters_inactive_warnings_before_blocking() -> None:
    warnings_list = [
        TossWarningInfo(
            warning_type="LIQUIDATION_TRADING",
            exchange="KRX",
            start_date="2026-06-01",
            end_date="2026-06-10",
        ),
        TossWarningInfo(
            warning_type="LIQUIDATION_TRADING",
            exchange="KRX",
            start_date="2026-06-20",
            end_date=None,
        ),
        TossWarningInfo(
            warning_type="OVERHEATED",
            exchange="KRX",
            start_date="2026-06-12",
            end_date=None,
        ),
    ]
    client = DummyClient(warnings_result=warnings_list)

    result = await check_warnings_guard(client, "005930", today=date(2026, 6, 12))

    assert result.ok is True
    assert [w.warning_type for w in result.warnings] == ["OVERHEATED"]


@pytest.mark.asyncio
async def test_warnings_guard_fail_open_on_timeout() -> None:
    client = DummyClient(should_delay=True)
    result = await check_warnings_guard(client, "005930", timeout=0.1)
    assert result.ok is True
    assert result.warnings == []
    assert "timed out" in result.error_message


@pytest.mark.asyncio
async def test_warnings_guard_fail_open_on_error() -> None:
    client = DummyClient(should_fail=True)
    result = await check_warnings_guard(client, "005930")
    assert result.ok is True
    assert result.warnings == []
    assert "failed" in result.error_message
