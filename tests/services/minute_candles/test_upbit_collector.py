from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.minute_candles.repository import (
    MinuteCandleRow,
    MinuteCandlesRepository,
)
from app.services.minute_candles.upbit_collector import (
    MAX_TOP_N,
    SOURCE,
    _rows_for_current_day,
    collect_upbit_minute_candles,
    select_targets,
)
from app.services.minute_candles.upbit_public import (
    Candle,
    CandlePage,
    Pacer,
    UpbitMinutePublicData,
    UpbitPublicError,
)
from research.crypto_universe.public_http import FetchResponse

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "upbit_minute_candles"
    / "krw_btc_page.json"
)


def test_target_union_is_sorted_and_top_n_is_capped() -> None:
    targets, held, top = select_targets(
        {"KRW-DOGE", "BTC-USDT", "KRW-BTC"},
        [f"KRW-{i:03}" for i in range(MAX_TOP_N + 2)],
        MAX_TOP_N + 100,
    )
    assert held == ("KRW-BTC", "KRW-DOGE")
    assert len(top) == MAX_TOP_N
    assert set(targets) == set(held) | {"KRW-BTC", "KRW-XRP", "KRW-ETH"} | set(top)
    assert targets == tuple(sorted(targets))


def test_parser_uses_documented_response_shape_and_keeps_decimal_values() -> None:
    payload = FIXTURE.read_bytes()
    now = datetime(2026, 11, 26, 0, 2, tzinfo=UTC)
    data = UpbitMinutePublicData(
        lambda _url: FetchResponse(200, payload),
        pacer=Pacer(0, sleep=lambda _: None),
        clock=lambda: now,
    )
    page = data.candles("KRW-BTC", now)
    assert len(page.candles) == 2
    assert str(page.candles[0].quote_volume) == "1234567.89"
    assert page.candles[0].time_utc == datetime(2026, 11, 26, 0, 0, tzinfo=UTC)


class FakeData:
    def __init__(self, pages: dict[str, list[CandlePage] | Exception]) -> None:
        self.pages = pages
        self.calls: dict[str, int] = {}

    def markets(self) -> list[str]:
        return ["KRW-BTC", "KRW-XRP", "KRW-ETH", "KRW-DOGE"]

    def top_by_trade_value(self, markets: list[str], limit: int) -> list[str]:
        return ["KRW-DOGE", "KRW-BTC"][:limit]

    def candles(self, market: str, before: datetime) -> CandlePage:
        i = self.calls.get(market, 0)
        self.calls[market] = i + 1
        outcome = self.pages[market]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome[min(i, len(outcome) - 1)]


def candle(market: str, stamp: datetime) -> Candle:
    return Candle(
        market,
        stamp,
        *[Decimal(value) for value in ("1", "2", "1", "2", "3", "6")],
    )


def test_forming_minute_is_excluded_and_closed_bar_is_returned_once() -> None:
    now = datetime(2026, 11, 26, 15, 2, tzinfo=UTC)
    page = CandlePage(
        (
            candle("KRW-BTC", datetime(2026, 11, 26, 15, 1, tzinfo=UTC)),
            candle("KRW-BTC", datetime(2026, 11, 26, 15, 0, tzinfo=UTC)),
        ),
        now,
    )
    rows = _rows_for_current_day(FakeData({"KRW-BTC": [page]}), "KRW-BTC", now)
    assert [row.time_utc for row, _ in rows] == [
        datetime(2026, 11, 26, 15, 0, tzinfo=UTC)
    ]


@pytest.mark.asyncio
async def test_errors_are_market_rows_and_other_markets_continue() -> None:
    now = datetime(2026, 11, 26, 15, 2, tzinfo=UTC)
    ok = CandlePage((candle("KRW-BTC", now - timedelta(minutes=2)),), now)
    failed = UpbitPublicError("HTTP 429")
    data = FakeData(
        {
            "KRW-BTC": [ok],
            "KRW-XRP": failed,
            "KRW-ETH": [CandlePage((), now)],
            "KRW-DOGE": [CandlePage((), now)],
        }
    )
    result = await collect_upbit_minute_candles(
        data=data, held_markets_loader=lambda: _resolved({"KRW-XRP"}), now=now, top_n=1
    )
    by_market = {row.market: row for row in result.markets}
    assert by_market["KRW-XRP"].status == "error"
    assert by_market["KRW-XRP"].rows is None
    assert by_market["KRW-BTC"].status == "ok"
    assert by_market["KRW-ETH"].rows == 0
    assert result.errors == 1


def test_429_and_5xx_are_retried_with_backoff_then_reported() -> None:
    sleeps: list[float] = []
    calls = 0

    def fetch(_url: str) -> FetchResponse:
        nonlocal calls
        calls += 1
        return FetchResponse(503 if calls % 2 else 429, b"{}")

    data = UpbitMinutePublicData(
        fetch,
        pacer=Pacer(0, sleep=lambda _: None),
        retry_sleep=sleeps.append,
    )
    with pytest.raises(UpbitPublicError) as error:
        data.markets()
    assert error.value.status_code == 429
    assert calls == 4
    assert sleeps == [1.0, 2.0, 4.0]


async def _resolved(value: set[str]) -> set[str]:
    return value


def test_minute_repository_has_insert_only_conflict_policy() -> None:
    sql = str(MinuteCandlesRepository._build_insert_only())
    assert "ON CONFLICT (instrument_id, time) DO NOTHING" in sql
    assert "DO UPDATE" not in sql
    assert SOURCE == "upbit_rest_1m"


@pytest.mark.asyncio
async def test_insert_only_repository_reports_zero_for_replayed_rows() -> None:
    class Session:
        rows: set[tuple[int, datetime]] = set()

        async def execute(self, statement, payload):
            assert "DO NOTHING" in str(statement)
            added = 0
            for row in payload:
                key = (row["instrument_id"], row["time"])
                if key not in self.rows:
                    self.rows.add(key)
                    added += 1
            return type("Result", (), {"rowcount": added})()

    session = Session()
    repo = MinuteCandlesRepository(session=session)
    row = MinuteCandleRow(
        instrument_id=1,
        time_utc=datetime(2026, 11, 26, tzinfo=UTC),
        open=Decimal("90000000.01"),
        high=Decimal("90000000.01"),
        low=Decimal("90000000.01"),
        close=Decimal("90000000.01"),
        base_volume=Decimal("0.00000001"),
        quote_volume=Decimal("0.9000000001"),
        source=SOURCE,
    )
    assert await repo.insert_rows(rows=[row]) == 1
    assert await repo.insert_rows(rows=[row]) == 0


def test_fixture_is_a_candle_list() -> None:
    assert isinstance(json.loads(FIXTURE.read_text()), list)
