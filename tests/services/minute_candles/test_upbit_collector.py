from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.minute_candles.repository import (
    MinuteCandleRow,
    MinuteCandlesRepository,
)
from app.services.minute_candles.upbit_collector import (
    MAX_PAGES_PER_MARKET,
    MAX_TOP_N,
    SOURCE,
    _rows_for_current_day,
    _walk_rows,
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
    now = datetime(2025, 7, 1, 12, 2, tzinfo=UTC)
    data = UpbitMinutePublicData(
        lambda _url: FetchResponse(200, payload),
        pacer=Pacer(0, sleep=lambda _: None),
        clock=lambda: now,
    )
    page = data.candles("KRW-BTC", now)
    assert len(page.candles) == 1
    assert str(page.candles[0].quote_volume) == "4022470467.03403"
    assert page.candles[0].time_utc == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)


def test_default_fetcher_allows_exact_collector_paths_without_network(
    monkeypatch,
) -> None:
    requested: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"[]"

    class Opener:
        def open(self, request, timeout):
            requested.append(request.full_url)
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    data = UpbitMinutePublicData(
        pacer=Pacer(0, sleep=lambda _: None), retry_sleep=lambda _: None
    )
    for url in (
        "https://api.upbit.com/v1/market/all?isDetails=false",
        "https://api.upbit.com/v1/ticker?markets=KRW-BTC",
        "https://api.upbit.com/v1/candles/minutes/1?market=KRW-BTC&count=200",
    ):
        assert data._fetch(url).status == 200
    assert len(requested) == 3


def test_target_ranking_uses_24h_quote_value() -> None:
    def fetch(url: str) -> FetchResponse:
        if url.endswith("market/all?isDetails=false"):
            return FetchResponse(
                200,
                b'[{"market":"KRW-AAA"},{"market":"KRW-BBB"},{"market":"KRW-CCC"}]',
            )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        markets = query["markets"][0].split(",")
        records = {
            "KRW-AAA": 10,
            "KRW-BBB": 30,
            "KRW-CCC": 20,
        }
        return FetchResponse(
            200,
            json.dumps(
                [
                    {"market": market, "acc_trade_price_24h": records[market]}
                    for market in markets
                ]
            ).encode(),
        )

    data = UpbitMinutePublicData(fetch, pacer=Pacer(0, sleep=lambda _: None))
    markets = data.markets()
    assert data.top_by_trade_value(markets, 2) == ["KRW-BBB", "KRW-CCC"]


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


def test_existing_latest_bar_limits_repeat_fetch_to_newer_rows() -> None:
    now = datetime(2026, 11, 26, 15, 5, tzinfo=UTC)
    page = CandlePage(
        tuple(
            candle("KRW-BTC", datetime(2026, 11, 26, 15, minute, tzinfo=UTC))
            for minute in (3, 2, 1)
        ),
        now,
    )
    rows = _rows_for_current_day(
        FakeData({"KRW-BTC": [page]}),
        "KRW-BTC",
        now,
        after=datetime(2026, 11, 26, 15, 1, tzinfo=UTC),
    )
    assert [row.time_utc.minute for row, _ in rows] == [2, 3]


def test_midnight_checkpoint_collects_closed_previous_day_tail() -> None:
    now = datetime(2026, 11, 27, 15, 1, tzinfo=UTC)  # 00:01 KST
    page = CandlePage(
        tuple(
            candle("KRW-BTC", now - timedelta(minutes=minute)) for minute in range(1, 6)
        ),
        now,
    )
    rows = _rows_for_current_day(
        FakeData({"KRW-BTC": [page]}),
        "KRW-BTC",
        now,
        after=datetime(2026, 11, 27, 14, 57, tzinfo=UTC),  # 23:57 KST
    )
    assert [row.time_utc for row, _ in rows] == [
        datetime(2026, 11, 27, 14, 58, tzinfo=UTC),
        datetime(2026, 11, 27, 14, 59, tzinfo=UTC),
    ]


def test_full_page_crossing_midnight_without_0000_candle_is_complete() -> None:
    now = datetime(2026, 11, 27, 15, 4, tzinfo=UTC)  # 00:04 KST
    page = CandlePage(
        tuple(
            candle("KRW-BTC", now - timedelta(minutes=minute))
            for minute in (minute for minute in range(1, 202) if minute != 4)
        ),
        now,
    )
    rows = _rows_for_current_day(FakeData({"KRW-BTC": [page]}), "KRW-BTC", now)
    stamps = [row.time_utc for row, _ in rows]
    assert datetime(2026, 11, 27, 14, 59, tzinfo=UTC) in stamps
    assert datetime(2026, 11, 27, 15, 0, tzinfo=UTC) not in stamps


def test_walk_reaching_checkpoint_exactly_is_complete(monkeypatch) -> None:
    now = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)
    monkeypatch.setattr("app.services.minute_candles.upbit_collector.PAGE_SIZE", 1)
    monkeypatch.setattr(
        "app.services.minute_candles.upbit_collector.MAX_PAGES_PER_MARKET", 2
    )
    pages = [
        CandlePage((candle("KRW-BTC", now - timedelta(minutes=1)),), now),
        CandlePage((candle("KRW-BTC", now - timedelta(minutes=2)),), now),
    ]

    walk = _walk_rows(
        FakeData({"KRW-BTC": pages}),
        "KRW-BTC",
        now,
        after=now - timedelta(minutes=2),
    )

    assert walk.pages == 2
    assert walk.reached_boundary is True
    assert walk.pending_gap is None
    assert walk.incomplete is False


@pytest.mark.asyncio
async def test_incomplete_walk_does_not_advance_checkpoint_and_next_run_fills_gap(
    monkeypatch,
) -> None:
    now = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)
    checkpoint = now - timedelta(minutes=5)
    monkeypatch.setattr("app.services.minute_candles.upbit_collector.PAGE_SIZE", 1)
    monkeypatch.setattr(
        "app.services.minute_candles.upbit_collector.MAX_PAGES_PER_MARKET", 2
    )

    class Session:
        def __init__(self) -> None:
            self.rows = {("KRW-BTC", checkpoint)}

        async def execute(self, statement, _payload=None):
            sql = str(statement)
            if "INSERT INTO public.crypto_instruments" in sql:
                return None
            if "crypto_instruments.id" in sql:
                return type("Result", (), {"scalar_one": lambda _self: 1})()
            latest = max(stamp for market, stamp in self.rows if market == "KRW-BTC")
            return type("Result", (), {"scalar_one_or_none": lambda _self: latest})()

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class FakeRepository:
        def __init__(self, session) -> None:
            self.session = session

        async def insert_rows(self, *, rows):
            added = 0
            for row in rows:
                key = ("KRW-BTC", row.time_utc)
                if key not in self.session.rows:
                    self.session.rows.add(key)
                    added += 1
            return added

    monkeypatch.setattr(
        "app.services.minute_candles.upbit_collector.MinuteCandlesRepository",
        FakeRepository,
    )
    session = Session()

    def page(minute: int) -> CandlePage:
        return CandlePage((candle("KRW-BTC", now - timedelta(minutes=minute)),), now)

    first_run = FakeData(
        {
            "KRW-BTC": [page(2), page(3)],
            "KRW-ETH": [CandlePage((), now)],
            "KRW-XRP": [CandlePage((), now)],
        }
    )
    first = await collect_upbit_minute_candles(
        session=session,
        data=first_run,
        held_markets_loader=lambda: _resolved(set()),
        now=now,
        top_n=0,
        dry_run=False,
    )

    assert session.rows == {("KRW-BTC", checkpoint)}
    first_btc = next(row for row in first.markets if row.market == "KRW-BTC")
    assert first_btc.pages == 2
    assert first_btc.reached_boundary is False
    assert first_btc.pending_gap == (
        checkpoint + timedelta(minutes=1),
        now - timedelta(minutes=4),
    )

    monkeypatch.setattr(
        "app.services.minute_candles.upbit_collector.MAX_PAGES_PER_MARKET",
        MAX_PAGES_PER_MARKET,
    )
    normal_run = FakeData(
        {
            "KRW-BTC": [page(2), page(3), page(4), page(5), page(6)],
            "KRW-ETH": [CandlePage((), now)],
            "KRW-XRP": [CandlePage((), now)],
        }
    )
    second = await collect_upbit_minute_candles(
        session=session,
        data=normal_run,
        held_markets_loader=lambda: _resolved(set()),
        now=now,
        top_n=0,
        dry_run=False,
    )

    assert session.rows == {
        ("KRW-BTC", checkpoint),
        ("KRW-BTC", now - timedelta(minutes=2)),
        ("KRW-BTC", now - timedelta(minutes=3)),
        ("KRW-BTC", now - timedelta(minutes=4)),
    }
    second_btc = next(row for row in second.markets if row.market == "KRW-BTC")
    assert second_btc.pages == 4
    assert second_btc.reached_boundary is True
    assert second_btc.pending_gap is None


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
    assert by_market["KRW-XRP"].new is None
    assert by_market["KRW-BTC"].status == "ok"
    assert by_market["KRW-BTC"].rows == 1
    assert by_market["KRW-BTC"].new is None  # dry-run does not query the DB
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


def test_upbit_collector_source_is_stable() -> None:
    assert SOURCE == "upbit_rest_1m"


@pytest.mark.asyncio
async def test_insert_only_repository_reports_zero_for_replayed_rows() -> None:
    class Session:
        rows: set[tuple[int, datetime]] = set()

        async def execute(self, statement, payload):
            assert "DO NOTHING" in str(statement)
            added = 0
            # The insert statement uses indexed bind names and RETURNING.
            keys = [key for key in payload if key.startswith("instrument_id_")]
            for key in keys:
                index = key.removeprefix("instrument_id_")
                row = {
                    name: payload[f"{name}_{index}"]
                    for name in ("instrument_id", "time")
                }
                key = (row["instrument_id"], row["time"])
                if key not in self.rows:
                    self.rows.add(key)
                    added += 1
            values = [None] * added
            return type(
                "Result",
                (),
                {
                    "scalars": lambda _self: type(
                        "Scalars", (), {"all": lambda _self: values}
                    )()
                },
            )()

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


@pytest.mark.asyncio
async def test_insert_only_repository_counts_multirow_insert_without_rowcount():
    class Session:
        async def execute(self, statement, payload):
            if isinstance(payload, list):
                # SQLAlchemy asyncpg executemany leaves rowcount at -1.
                return type("Result", (), {"rowcount": -1})()
            assert "RETURNING time" in str(statement)
            assert isinstance(payload, dict)
            values = [1, 2]
            return type(
                "Result",
                (),
                {
                    "scalars": lambda _self: type(
                        "Scalars", (), {"all": lambda _self: values}
                    )()
                },
            )()

    rows = [
        MinuteCandleRow(
            instrument_id=1,
            time_utc=datetime(2026, 11, 26, 0, minute, tzinfo=UTC),
            open=1,
            high=2,
            low=1,
            close=2,
            base_volume=3,
            source=SOURCE,
        )
        for minute in (0, 1)
    ]
    assert await MinuteCandlesRepository(session=Session()).insert_rows(rows=rows) == 2


def test_fixture_is_a_candle_list() -> None:
    assert isinstance(json.loads(FIXTURE.read_text()), list)
