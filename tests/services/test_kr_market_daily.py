"""Unit coverage for KR market-wide daily facts; all provider inputs are fixtures."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.kr_market_daily import KRMarketBreadth
from app.services.kr_market_daily import collector as market_daily_collector
from app.services.kr_market_daily.client import (
    BreadthFetchError,
    NaverMarketDailyClient,
    count_breadth,
    parse_breadth_page,
    parse_deposit_rows,
    parse_investor_rows,
)
from app.services.kr_market_daily.collector import collect_market_daily
from app.services.kr_market_daily.reader import read_market_daily_context
from app.services.kr_market_daily.store import insert_market_daily

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kr_market_daily"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def test_breadth_fixture_counts_every_change_sign_and_keeps_partial_coverage() -> None:
    first_total, first = parse_breadth_page(fixture("breadth_page_1.json"))
    second_total, second = parse_breadth_page(fixture("breadth_page_2.json"))
    assert first_total == second_total == 3
    assert count_breadth(first + second) == (1, 1, 1)
    assert len(first) < first_total


def test_investor_fixture_keeps_unknown_code_and_exact_decimal_strings() -> None:
    rows = parse_investor_rows(fixture("investor_groups.json"), date(2026, 11, 25))
    assert rows[0]["net_amount"] == Decimal("1200")
    assert rows[0]["investor_name"] == "개인"
    assert rows[1]["investor_code"] == "9911"
    assert rows[1]["investor_name"] is None
    assert rows[1]["net_amount"] == Decimal("-2.50")


def test_deposit_fixture_confirms_hundred_million_krw_unit() -> None:
    (row,) = parse_deposit_rows(fixture("deposit_credit.json"))
    assert row["bizdate"] == date(2026, 11, 24)
    assert row["amount_unit"] == "억원"
    assert row["customer_deposit"] == Decimal("62500.25")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_decimal_parser_rejects_nonfinite_values(value: str) -> None:
    payload = {
        "content": [
            {
                "bizdate": "20261125",
                "netAmounts": [
                    {
                        "investorGubun": "8000",
                        "diffValue": value,
                        "buyPrice": "0",
                        "sellPrice": "0",
                        "buyQuant": "0",
                        "sellQuant": "0",
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match="finite"):
        parse_investor_rows(payload, date(2026, 11, 25))


@pytest.mark.parametrize("value", [None, "bad", "20260230"])
def test_investor_parser_rejects_missing_or_invalid_bizdate(value: Any) -> None:
    with pytest.raises(ValueError, match="bizdate"):
        parse_investor_rows({"content": [{"bizdate": value}]}, date(2026, 11, 25))


def test_breadth_parser_requires_stock_identity() -> None:
    with pytest.raises(ValueError, match="stockCode"):
        parse_breadth_page(
            {"totalCount": 1, "stocks": [{"compareToPreviousClosePrice": "1"}]}
        )


def test_breadth_key_keeps_intraday_and_after_close_observations() -> None:
    assert [
        column.name for column in KRMarketBreadth.__table__.primary_key.columns
    ] == [
        "market",
        "trade_date",
        "observed_at",
    ]


@pytest.mark.asyncio
async def test_breadth_fetch_stores_partial_page_as_incomplete(monkeypatch) -> None:
    from app.services.research_candles import session_segment

    monkeypatch.setattr(session_segment, "session_segment_of", lambda *_: "KRX_REGULAR")

    class FakeClient:
        async def fetch_breadth(self, market: str) -> dict[str, Any]:
            return {
                "market": market,
                "up_count": 1,
                "down_count": 0,
                "flat_count": 0,
                "total_count": 3,
                "fetched_count": 1,
                "complete": False,
            }

        async def fetch_investors(
            self, market: str, bizdate: date
        ) -> list[dict[str, Any]]:
            return []

        async def fetch_deposits(self) -> list[dict[str, Any]]:
            return []

    result = await collect_market_daily(
        target_date=date(2026, 11, 26),
        today=date(2026, 11, 26),
        client=FakeClient(),
        now=datetime(2026, 11, 26, 6, 40, tzinfo=UTC),
    )
    assert result["breadth"][0]["complete"] is False
    assert result["breadth"][0]["fetched_count"] < result["breadth"][0]["total_count"]


@pytest.mark.asyncio
async def test_breadth_is_skipped_when_krx_session_segment_is_unknown(
    monkeypatch,
) -> None:
    from app.services.research_candles import session_segment

    monkeypatch.setattr(session_segment, "session_segment_of", lambda *_: "UNKNOWN")

    class FakeClient:
        breadth_calls = 0

        async def fetch_breadth(self, market: str) -> dict[str, Any]:
            self.breadth_calls += 1
            return {}

        async def fetch_investors(
            self, market: str, bizdate: date
        ) -> list[dict[str, Any]]:
            return []

        async def fetch_deposits(self) -> list[dict[str, Any]]:
            return []

    client = FakeClient()
    result = await collect_market_daily(
        target_date=date(2026, 11, 26),
        today=date(2026, 11, 26),
        client=client,
        now=datetime(2026, 11, 26, 6, 40, tzinfo=UTC),
    )
    assert client.breadth_calls == 0
    assert result["breadth"] == []


@pytest.mark.asyncio
async def test_first_received_at_is_captured_after_each_source_returns(
    monkeypatch,
) -> None:
    from app.services.research_candles import session_segment

    monkeypatch.setattr(session_segment, "session_segment_of", lambda *_: "KRX_REGULAR")
    base = datetime(2026, 11, 26, 1, 0, tzinfo=UTC)

    class Clock:
        calls = 0

        @classmethod
        def now(cls, tz: Any) -> datetime:
            value = base + timedelta(seconds=cls.calls)
            cls.calls += 1
            return value

    monkeypatch.setattr(market_daily_collector, "datetime", Clock)

    class FakeClient:
        async def fetch_breadth(self, market: str) -> dict[str, Any]:
            return {
                "market": market,
                "up_count": 1,
                "down_count": 0,
                "flat_count": 0,
                "total_count": 1,
                "fetched_count": 1,
                "complete": True,
            }

        async def fetch_investors(
            self, market: str, bizdate: date
        ) -> list[dict[str, Any]]:
            return [{"investor_code": market}]

        async def fetch_deposits(self) -> list[dict[str, Any]]:
            return [{"bizdate": date(2026, 11, 25)}]

    result = await market_daily_collector.collect_market_daily(
        target_date=date(2026, 11, 26),
        today=date(2026, 11, 26),
        client=FakeClient(),
    )
    timestamps = [
        row["first_received_at"]
        for group in ("breadth", "investor_groups", "deposit_credit")
        for row in result[group]
    ]
    assert len(timestamps) == 5
    assert len(set(timestamps)) == 5


@pytest.mark.asyncio
async def test_insert_rerun_uses_conflict_do_nothing_only() -> None:
    class FakeSession:
        statements: list[str]

        def __init__(self) -> None:
            self.statements = []

        async def execute(self, statement: Any) -> Any:
            self.statements.append(
                str(statement.compile(compile_kwargs={"literal_binds": True}))
            )
            return SimpleNamespace(rowcount=0)

        async def flush(self) -> None:
            return None

    session = FakeSession()
    row = {
        "market": "KOSPI",
        "trade_date": date(2026, 11, 25),
        "investor_code": "8000",
        "investor_name": "개인",
        "net_amount": Decimal("1"),
        "buy_amount": None,
        "sell_amount": None,
        "buy_quantity": None,
        "sell_quantity": None,
        "amount_unit": "KRW",
        "quantity_unit": "주",
        "source": "naver",
    }
    received = datetime(2026, 11, 25, tzinfo=UTC)
    await insert_market_daily(
        session, breadth=[], investors=[row], deposits=[], first_received_at=received
    )
    await insert_market_daily(
        session, breadth=[], investors=[row], deposits=[], first_received_at=received
    )
    assert len(session.statements) == 2
    assert all(
        "ON CONFLICT DO NOTHING" in statement for statement in session.statements
    )
    assert all("DO UPDATE" not in statement for statement in session.statements)


@pytest.mark.asyncio
async def test_reader_returns_null_with_closed_reasons_when_sources_missing() -> None:
    class FakeResult:
        def scalars(self) -> FakeResult:
            return self

        def all(self) -> list[Any]:
            return []

        def first(self) -> None:
            return None

    class FakeSession:
        async def execute(self, statement: Any) -> FakeResult:
            return FakeResult()

    result = await read_market_daily_context(FakeSession(), date(2026, 11, 26))
    assert result["latest_breadth"]["value"] == {"KOSPI": None, "KOSDAQ": None}
    assert result["latest_breadth"]["reason"] == "breadth_missing"
    assert result["investor_groups"]["reason"] == "investor_groups_missing"
    assert result["deposit_credit"] == {
        "value": None,
        "reason": "deposit_credit_missing",
    }


@pytest.mark.asyncio
async def test_404_is_a_typed_failure_not_an_empty_payload() -> None:
    class Response:
        headers = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            raise RuntimeError("404")

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, *_: object, **__: object) -> Response:
            return Response()

    class Http:
        def __init__(self) -> None:
            self.client = Client()

        def AsyncClient(self, **_: object) -> Client:
            return self.client

    with pytest.raises(BreadthFetchError):
        await NaverMarketDailyClient(http=Http()).get_json(
            "https://m.stock.naver.com/api/stocks/marketValue/KOSPI",
            {},
            BreadthFetchError,
        )


@pytest.mark.asyncio
async def test_breadth_fetch_rejects_duplicate_symbols_across_pages() -> None:
    class Response:
        headers = {"content-type": "application/json"}

        def __init__(self, payload: object) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return self.payload

    class Client:
        def __init__(self) -> None:
            self.payloads = iter(
                (
                    {
                        "totalCount": 2,
                        "stocks": [
                            {"stockCode": "A", "compareToPreviousClosePrice": "1"}
                        ],
                    },
                    {
                        "totalCount": 2,
                        "stocks": [
                            {"stockCode": "A", "compareToPreviousClosePrice": "1"}
                        ],
                    },
                )
            )

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, *_: object, **__: object) -> Response:
            return Response(next(self.payloads))

    class Http:
        def AsyncClient(self, **_: object) -> Client:
            return Client()

    async def no_sleep(_: float) -> None:
        return None

    with pytest.raises(BreadthFetchError, match="breadth payload invalid"):
        await NaverMarketDailyClient(http=Http(), sleep=no_sleep).fetch_breadth("KOSPI")
