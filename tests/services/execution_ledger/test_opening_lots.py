# tests/services/execution_ledger/test_opening_lots.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.execution_ledger.opening_lots import (
    OpeningLotCandidate,
    build_opening_lot_plan,
    filter_opening_lot_candidates,
    normalize_opening_lot_symbol,
    split_requested_symbols,
)


def _candidate(**overrides) -> OpeningLotCandidate:  # noqa: ANN003
    data = {
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": "005930",
        "raw_symbol": "005930",
        "currency": "KRW",
        "current_qty": Decimal("10"),
        "avg_price": Decimal("70000"),
        "avg_price_modified": False,
    }
    data.update(overrides)
    return OpeningLotCandidate(**data)


def test_opening_lot_quantity_subtracts_ledger_net_since_cutover() -> None:
    cutover = datetime(2026, 5, 10, tzinfo=UTC)
    plan = build_opening_lot_plan(
        candidates=[_candidate()],
        ledger_net_by_key={
            ("kis", "live", "krx", "equity_kr", "005930", "KRW"): Decimal("3")
        },
        cutover=cutover,
    )

    assert len(plan.upserts) == 1
    upsert = plan.upserts[0]
    assert upsert.source == "manual_import"
    assert upsert.side == "buy"
    assert upsert.filled_qty == Decimal("7")
    assert upsert.filled_price == Decimal("70000")
    assert upsert.filled_at == cutover
    assert upsert.broker_order_id == "SEED-20260510-kis-krx-005930"


def test_opening_lot_skips_when_ledger_net_covers_current_position() -> None:
    plan = build_opening_lot_plan(
        candidates=[_candidate(current_qty=Decimal("10"))],
        ledger_net_by_key={
            ("kis", "live", "krx", "equity_kr", "005930", "KRW"): Decimal("10")
        },
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
    )

    assert plan.upserts == []
    assert plan.skipped[0].reason == "covered_by_ledger_net"


def test_opening_lot_skips_modified_upbit_average_price() -> None:
    plan = build_opening_lot_plan(
        candidates=[
            _candidate(
                broker="upbit",
                venue="upbit_krw",
                instrument_type="crypto",
                symbol="SOL",
                raw_symbol="KRW-SOL",
                avg_price_modified=True,
            )
        ],
        ledger_net_by_key={},
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
    )

    assert plan.upserts == []
    assert plan.skipped[0].reason == "upbit_avg_price_modified"


def test_opening_lot_skips_zero_average_price() -> None:
    plan = build_opening_lot_plan(
        candidates=[_candidate(avg_price=Decimal("0"))],
        ledger_net_by_key={},
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
    )

    assert plan.upserts == []
    assert plan.skipped[0].reason == "non_positive_avg_price"


@pytest.mark.asyncio
async def test_upbit_candidates_only_cover_krw_markets(monkeypatch) -> None:
    import app.services.brokers.upbit.client as upbit_client
    from app.services.execution_ledger.opening_lots import (
        load_upbit_opening_lot_candidates,
    )

    async def _fake_fetch_my_coins():
        return [
            {"currency": "KRW", "balance": "1000", "locked": "0"},
            {
                "currency": "SOL",
                "unit_currency": "KRW",
                "balance": "2",
                "locked": "0",
                "avg_buy_price": "200000",
                "avg_buy_price_modified": "false",
            },
            {
                "currency": "ETH",
                "unit_currency": "BTC",
                "balance": "0.5",
                "locked": "0",
                "avg_buy_price": "0.05",
                "avg_buy_price_modified": "false",
            },
        ]

    monkeypatch.setattr(upbit_client, "fetch_my_coins", _fake_fetch_my_coins)

    candidates = await load_upbit_opening_lot_candidates()

    assert [c.symbol for c in candidates] == ["SOL"]
    sol = candidates[0]
    assert sol.venue == "upbit_krw"
    assert sol.currency == "KRW"
    assert sol.raw_symbol == "KRW-SOL"


def test_normalize_opening_lot_symbol_uppercases_and_db_forms() -> None:
    assert normalize_opening_lot_symbol("005930") == "005930"
    assert normalize_opening_lot_symbol(" 005930 ") == "005930"
    assert normalize_opening_lot_symbol("brk.b") == "BRK.B"
    assert normalize_opening_lot_symbol("BRK-B") == "BRK.B"
    assert normalize_opening_lot_symbol("BRK/B") == "BRK.B"
    assert normalize_opening_lot_symbol("krw-btc") == "KRW.BTC"
    assert normalize_opening_lot_symbol("") == ""
    assert normalize_opening_lot_symbol(" ") == ""


def test_split_requested_symbols_flattens_comma_lists_and_dedupes() -> None:
    assert split_requested_symbols(["005930,196170", "005930", " , 196170 "]) == [
        "005930",
        "196170",
    ]
    assert split_requested_symbols([]) == []
    assert split_requested_symbols([",,"]) == []


def test_filter_opening_lot_candidates_exact_equality_only() -> None:
    candidates = [
        _candidate(symbol="005930", raw_symbol="005930"),
        _candidate(symbol="000660", raw_symbol="000660"),
    ]
    result = filter_opening_lot_candidates(candidates, ["005930"])

    assert [c.symbol for c in result.candidates] == ["005930"]
    assert result.requested == ["005930"]
    assert result.matched == ["005930"]
    assert result.unmatched == []


def test_filter_opening_lot_candidates_rejects_prefix_and_substring() -> None:
    result = filter_opening_lot_candidates(
        [_candidate(symbol="005930", raw_symbol="005930")],
        ["00593", "05930", "0059300", "5930"],
    )

    assert result.candidates == []
    assert result.matched == []
    assert result.unmatched == ["00593", "05930", "0059300", "5930"]


def test_filter_opening_lot_candidates_reports_unmatched_in_request_order() -> None:
    result = filter_opening_lot_candidates(
        [_candidate(symbol="005930", raw_symbol="005930")],
        ["999999", "005930", "888888"],
    )

    assert [c.symbol for c in result.candidates] == ["005930"]
    assert result.matched == ["005930"]
    assert result.unmatched == ["999999", "888888"]


def test_filter_opening_lot_candidates_matches_us_and_crypto_forms() -> None:
    us = _candidate(
        symbol="BRK/B",
        raw_symbol="BRK/B",
        venue="NASD",
        instrument_type="equity_us",
        currency="USD",
    )
    crypto = _candidate(
        broker="upbit",
        venue="upbit_krw",
        instrument_type="crypto",
        symbol="BTC",
        raw_symbol="KRW-BTC",
    )
    result = filter_opening_lot_candidates([us, crypto], ["brk.b", "KRW-BTC"])

    assert [c.symbol for c in result.candidates] == ["BRK/B", "BTC"]
    assert result.matched == ["BRK.B", "KRW.BTC"]
    assert result.unmatched == []


def test_filter_opening_lot_candidates_crypto_currency_code() -> None:
    crypto = _candidate(
        broker="upbit",
        venue="upbit_krw",
        instrument_type="crypto",
        symbol="BTC",
        raw_symbol="KRW-BTC",
    )
    result = filter_opening_lot_candidates([crypto], ["btc"])

    assert [c.symbol for c in result.candidates] == ["BTC"]
    assert result.matched == ["BTC"]
