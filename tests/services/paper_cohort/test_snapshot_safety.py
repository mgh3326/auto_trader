from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.services.paper_cohort.contracts import PaperCohortError
from app.services.paper_cohort.market_snapshot import CanonicalSnapshotCapture
from tests.services.paper_cohort.test_market_snapshot import (
    CAPTURED_AT,
    FakePublicClient,
    request,
    ticker,
)

pytestmark = pytest.mark.unit


async def rejected(client: FakePublicClient) -> str:
    clocks = iter([CAPTURED_AT, CAPTURED_AT + timedelta(milliseconds=200)])
    with pytest.raises(PaperCohortError) as exc_info:
        await CanonicalSnapshotCapture(client, clock=lambda: next(clocks)).capture(
            request()
        )
    return exc_info.value.reason_code


@pytest.mark.parametrize(
    "mutation",
    [
        "open_candle",
        "short_lookback",
        "gap",
        "duplicate",
        "unsorted",
        "wrong_symbol",
        "wrong_interval",
        "nan",
        "nonpositive",
        "bad_ohlc",
    ],
)
@pytest.mark.asyncio
async def test_candle_fail_close_matrix(mutation: str) -> None:
    client = FakePublicClient()
    rows = client.candles["BTCUSDT"]
    if mutation == "open_candle":
        rows[2] = replace(rows[2], is_closed=False)
    elif mutation == "short_lookback":
        rows.pop()
    elif mutation == "gap":
        rows[1] = replace(
            rows[1],
            open_time=rows[1].open_time + timedelta(minutes=1),
            close_time=rows[1].close_time + timedelta(minutes=1),
        )
    elif mutation == "duplicate":
        rows[1] = rows[0]
    elif mutation == "unsorted":
        rows[0], rows[1] = rows[1], rows[0]
    elif mutation == "wrong_symbol":
        rows[0] = replace(rows[0], symbol="ETHUSDT")
    elif mutation == "wrong_interval":
        rows[0] = replace(rows[0], interval="5m")
    elif mutation == "nan":
        rows[0] = replace(rows[0], close=Decimal("NaN"))
    elif mutation == "nonpositive":
        rows[0] = replace(rows[0], base_volume=Decimal("0"))
    elif mutation == "bad_ohlc":
        rows[0] = replace(rows[0], high=Decimal("100.5"), close=Decimal("101"))

    assert await rejected(client) == "invalid_canonical_snapshot"


@pytest.mark.parametrize(
    "mutation",
    [
        "partial",
        "stale",
        "skew",
        "crossed",
        "nan",
        "nonpositive",
        "naive_time",
    ],
)
@pytest.mark.asyncio
async def test_ticker_fail_close_matrix(mutation: str) -> None:
    client = FakePublicClient()
    if mutation == "partial":
        client.tickers["ETHUSDT"] = ticker("BTCUSDT")
    elif mutation == "stale":
        client.tickers["BTCUSDT"] = ticker(
            "BTCUSDT", CAPTURED_AT - timedelta(seconds=6)
        )
    elif mutation == "skew":
        client.tickers["ETHUSDT"] = ticker(
            "ETHUSDT", CAPTURED_AT + timedelta(seconds=3)
        )
    elif mutation == "crossed":
        client.tickers["BTCUSDT"] = replace(
            client.tickers["BTCUSDT"], bid_price=Decimal("102")
        )
    elif mutation == "nan":
        client.tickers["BTCUSDT"] = replace(
            client.tickers["BTCUSDT"], bid_qty=Decimal("Infinity")
        )
    elif mutation == "nonpositive":
        client.tickers["BTCUSDT"] = replace(
            client.tickers["BTCUSDT"], ask_qty=Decimal("0")
        )
    elif mutation == "naive_time":
        client.tickers["BTCUSDT"] = ticker("BTCUSDT", CAPTURED_AT.replace(tzinfo=None))

    assert await rejected(client) == "invalid_canonical_snapshot"


@pytest.mark.asyncio
async def test_provider_error_is_stable_fail_close() -> None:
    client = FakePublicClient()

    async def fail(_symbol: str):
        raise RuntimeError("provider unavailable")

    client.book_ticker = fail  # type: ignore[method-assign]
    assert await rejected(client) == "canonical_provider_error"
