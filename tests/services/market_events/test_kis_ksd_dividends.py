from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.market_events.kis_ksd_dividends import (
    fetch_ksd_dividend_window,
    normalize_ksd_dividend_row,
)

pytestmark = pytest.mark.unit


def test_ksd_dividend_normalizes_exact_decimal_and_dates():
    received_at = datetime(2026, 11, 25, 3, 0, tzinfo=UTC)
    event, values = normalize_ksd_dividend_row(
        {
            "record_date": "20261120",
            "sht_cd": "005930",
            "isin_name": "삼성전자",
            "per_sto_divi_amt": "361.2500",
            "divi_pay_dt": "20261220",
            "divi_kind": "현금배당",
        },
        received_at=received_at,
    )

    assert event["event_date"] == date(2026, 11, 20)
    assert event["raw_payload_json"]["payment_date"] == "2026-12-20"
    assert event["source"] == "kis_ksd"
    assert event["category"] == "dividend"
    assert event["market"] == "kr"
    assert event["first_received_at"] == received_at
    assert values[0]["actual"] == Decimal("361.2500")
    assert isinstance(values[0]["actual"], Decimal)


@pytest.mark.asyncio
async def test_100_row_response_splits_date_window():
    calls: list[tuple[str, str]] = []

    async def fetch(start: str, end: str) -> list[dict[str, str]]:
        calls.append((start, end))
        if start == "20261101" and end == "20261104":
            return [{"record_date": "20261101"}] * 100
        return [{"record_date": start}]

    result = await fetch_ksd_dividend_window(
        fetch, date(2026, 11, 1), date(2026, 11, 4)
    )

    assert calls == [
        ("20261101", "20261104"),
        ("20261101", "20261102"),
        ("20261103", "20261104"),
    ]
    assert len(result.rows) == 2
    assert result.truncated_windows == ()


@pytest.mark.asyncio
async def test_single_capped_day_is_reported_as_truncated():
    result = await fetch_ksd_dividend_window(
        lambda _start, _end: _hundred_rows(), date(2026, 11, 1), date(2026, 11, 1)
    )
    assert len(result.rows) == 100
    assert result.truncated_windows == ((date(2026, 11, 1), date(2026, 11, 1)),)


@pytest.mark.asyncio
async def test_ksd_dividend_client_rejects_mock_before_request():
    from app.services.brokers.kis.market_data import MarketDataClient

    client = MarketDataClient(SimpleNamespace(_is_mock_client=True))
    request = AsyncMock()
    client._request_with_token_retry = request

    with pytest.raises(RuntimeError, match="real accounts only"):
        await client.ksd_dividends("20261101", "20261130")

    request.assert_not_awaited()


async def _hundred_rows() -> list[dict[str, str]]:
    return [{"record_date": "20261101"}] * 100
