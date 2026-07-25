from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.services.paper_cohort.market_snapshot import CanonicalSnapshotCapture
from app.services.paper_cohort.signals import (
    SignalComputationInput,
    VenueQuote,
    build_would_order_evidence,
    compute_target_signal,
)
from tests.services.paper_cohort.test_market_snapshot import (
    CAPTURED_AT,
    FakePublicClient,
    request,
)

pytestmark = pytest.mark.unit


async def snapshot():
    clocks = iter([CAPTURED_AT, CAPTURED_AT + timedelta(milliseconds=200)])
    return await CanonicalSnapshotCapture(
        FakePublicClient(), clock=lambda: next(clocks)
    ).capture(request())


def signal_input() -> SignalComputationInput:
    return SignalComputationInput(
        cohort_id="cohort-1",
        assignment_id="assignment-1",
        experiment_id="1" * 64,
        strategy_version_id="strategy-v1",
        strategy_hash="2" * 64,
        config_hash="3" * 64,
        policy_hash="4" * 64,
        symbol="BTCUSDT",
        target_weight=Decimal("0.6"),
        capital_notional_usd=Decimal("10000"),
    )


def quote(venue: str, ask: str) -> VenueQuote:
    symbol = "BTCUSDT" if venue == "binance" else "BTC/USD"
    return VenueQuote(
        venue=venue,
        symbol=symbol,
        bid_price=Decimal(ask) - Decimal("1"),
        ask_price=Decimal(ask),
        bid_qty=Decimal("10"),
        ask_qty=Decimal("10"),
        fetched_at=CAPTURED_AT + timedelta(milliseconds=300),
        qty_increment=Decimal("0.0001"),
        min_qty=Decimal("0.0001"),
        min_notional=Decimal("10"),
    )


@pytest.mark.asyncio
async def test_signal_is_byte_equivalent_before_any_venue_quote() -> None:
    canonical = await snapshot()
    first = compute_target_signal(canonical, signal_input())
    second = compute_target_signal(canonical, signal_input())

    assert first.model_dump_json() == second.model_dump_json()
    assert first.signal_hash == first.recomputed_signal_hash()
    assert first.reference_price == "100.5"
    assert first.target_weight == "0.6"
    assert first.target_notional == "6000"
    assert first.side == "buy"

    low_quote_order = build_would_order_evidence(first, quote("alpaca", "100"))
    high_quote_order = build_would_order_evidence(first, quote("alpaca", "120"))
    assert low_quote_order != high_quote_order
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.asyncio
async def test_capability_conversion_is_exact_and_fail_closed() -> None:
    signal = compute_target_signal(await snapshot(), signal_input())
    binance = build_would_order_evidence(signal, quote("binance", "101"))
    alpaca = build_would_order_evidence(signal, quote("alpaca", "101"))

    assert binance.order == {
        "symbol": "BTCUSDT",
        "side": "buy",
        "order_type": "market",
        "sizing": "notional",
        "notional": "6000",
    }
    assert alpaca.order["symbol"] == "BTC/USD"
    assert alpaca.order["side"] == "buy"
    assert alpaca.order["order_type"] == "limit"
    assert alpaca.order["sizing"] == "qty"
    assert Decimal(alpaca.order["qty"]) > 0

    sell = signal.model_copy(update={"side": "sell"})
    blocked = build_would_order_evidence(sell, quote("binance", "101"))
    assert blocked.reason_code == "unsupported_capability"
    assert blocked.order is None


@pytest.mark.asyncio
async def test_alpaca_quantity_rounds_down_to_an_exact_increment_multiple() -> None:
    signal = compute_target_signal(await snapshot(), signal_input())
    venue_quote = quote("alpaca", "98").model_copy(
        update={"qty_increment": Decimal("0.05")}
    )

    evidence = build_would_order_evidence(signal, venue_quote)

    assert evidence.reason_code == "ok"
    assert evidence.order is not None
    assert evidence.order["qty"] == "61.2"
    assert Decimal(evidence.order["qty"]) % Decimal("0.05") == 0


@pytest.mark.asyncio
async def test_alpaca_missing_asset_constraints_remains_fail_closed_without_asserts() -> (
    None
):
    signal = compute_target_signal(await snapshot(), signal_input())
    valid = quote("alpaca", "101")
    malformed = VenueQuote.model_construct(
        **valid.model_dump(exclude={"qty_increment"}),
        qty_increment=None,
    )

    evidence = build_would_order_evidence(signal, malformed)

    assert evidence.reason_code == "unsupported_capability"
    assert evidence.order is None
