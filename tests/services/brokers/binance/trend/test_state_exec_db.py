"""hk 4719 trend state: acknowledgement, evidence, exits and rejections.

CI only: runs against the run-owned test database (never a production
database), like ``test_state_db.py``. The trend tables are append-only for every
role, so teardown turns triggers off for its own transaction only.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal, engine
from app.services.brokers.binance.trend.constants import (
    EXIT_HARD_STOP,
    EXIT_SIGNAL_FLIP,
)
from app.services.brokers.binance.trend.state import (
    TrendOrderEvidence,
    TrendStateBlocked,
    TrendStateService,
)
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

NOW = dt.datetime(2026, 11, 14, 3, tzinfo=dt.UTC)
BASE_TS = int(NOW.timestamp() * 1000)
_ts = iter(range(BASE_TS, BASE_TS + 10**9, 60_000))


async def _wipe() -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        await conn.execute(sa.text("DELETE FROM review.binance_trend_intents"))
        await conn.execute(sa.text("DELETE FROM review.binance_trend_signals"))


@pytest_asyncio.fixture(autouse=True)
async def clean(_bootstrap_test_schema):
    await _wipe()
    yield
    await _wipe()


@pytest.fixture
def svc() -> TrendStateService:
    return TrendStateService(AsyncSessionLocal)


def evidence(intent, *, status="FILLED", executed=None, avg="100", **changes):
    values = {
        "client_order_id": intent.client_order_id,
        "broker_order_id": "42",
        "symbol": "BTCUSDT",
        "side": intent.side,
        "orig_qty": intent.qty,
        "executed_qty": intent.qty if executed is None else D(executed),
        "avg_price": D(avg),
        "status": status,
        "reduce_only": intent.reduce_only,
        "position_side": "BOTH",
    }
    values.update(changes)
    return TrendOrderEvidence(**values)


async def to_sending(
    svc: TrendStateService, *, side="BUY", qty="0.6", symbol="BTCUSDT"
):
    signal = await svc.observe_signal(
        symbol=symbol, side=side, decision_ts=next(_ts), price_text="100"
    )
    await svc.reserve_entry(
        signal_key=signal.signal_key, notional_usdt=D("60"), now=NOW
    )
    intent = await svc.reserve_intent(
        signal_key=signal.signal_key,
        leg_key="entry",
        side=side,
        qty=D(qty),
        reference_price=D("100"),
        reduce_only=False,
        now=NOW,
    )
    return signal, await svc.mark_sending(intent.client_order_id, now=NOW)


async def to_holding(
    svc: TrendStateService, *, side="BUY", qty="0.6", symbol="BTCUSDT"
):
    signal, intent = await to_sending(svc, side=side, qty=qty, symbol=symbol)
    await svc.acknowledge(
        intent.client_order_id, broker_order_id="42", broker_status="NEW", now=NOW
    )
    position = D(qty) if side == "BUY" else -D(qty)
    held, done = await svc.apply_order_evidence(
        evidence(intent, symbol=symbol), broker_position_amt=position, now=NOW
    )
    await svc.settle_intent(intent.client_order_id, now=NOW)
    return held, done


async def test_entry_fill_is_evidence_and_position_confirmed(svc) -> None:
    held, intent = await to_holding(svc)
    assert held.state == "holding"
    assert (held.entry_qty, held.entry_price) == (D("0.6"), D("100"))
    assert held.entered_at == NOW and held.remaining_qty == D("0.6")
    assert (await svc.get_intent(intent.client_order_id)).state == "settled"


async def test_a_fill_the_position_does_not_confirm_raises(svc) -> None:
    _, intent = await to_sending(svc)
    for wrong in (D("0"), D("0.5"), D("-0.6")):
        with pytest.raises(TrendStateBlocked, match="position"):
            await svc.apply_order_evidence(
                evidence(intent), broker_position_amt=wrong, now=NOW
            )
    assert (await svc.get_intent(intent.client_order_id)).state == "sending"


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "ETHUSDT"},
        {"side": "SELL"},
        {"reduce_only": True},
        {"position_side": "LONG"},
        {"position_side": None},
        {"orig_qty": D("0.5")},
        {"status": "WEIRD"},
        {"executed": "0.5"},  # FILLED without the full quantity
    ],
)
async def test_evidence_must_match_the_intent_exactly(svc, change) -> None:
    _, intent = await to_sending(svc)
    with pytest.raises(TrendStateBlocked):
        await svc.apply_order_evidence(
            evidence(intent, **change), broker_position_amt=D("0.6"), now=NOW
        )
    assert (await svc.get_intent(intent.client_order_id)).state == "sending"


async def test_pending_evidence_acknowledges_without_resolving(svc) -> None:
    signal, intent = await to_sending(svc)
    held, after = await svc.apply_order_evidence(
        evidence(intent, status="NEW", executed="0", avg="0"),
        broker_position_amt=D("0"),
        now=NOW,
    )
    assert (held.state, after.state) == ("entry_reserved", "acknowledged")


async def test_no_fill_terminal_blocks_the_signal_and_replay_is_idempotent(svc) -> None:
    signal, intent = await to_sending(svc)
    ev = evidence(intent, status="CANCELED", executed="0", avg="0")
    blocked, done = await svc.apply_order_evidence(
        ev, broker_position_amt=D("0"), now=NOW
    )
    assert (blocked.state, done.state) == ("blocked", "evidenced")
    again, done2 = await svc.apply_order_evidence(
        ev, broker_position_amt=D("0"), now=NOW
    )
    assert (again.state, done2.state) == ("blocked", "evidenced")
    with pytest.raises(TrendStateBlocked, match="contradicts"):
        await svc.apply_order_evidence(
            evidence(intent, status="FILLED", executed="0.6"),
            broker_position_amt=D("0.6"),
            now=NOW,
        )


async def test_uncertain_resolves_only_through_evidence(svc) -> None:
    signal, intent = await to_sending(svc)
    await svc.mark_uncertain(intent.client_order_id, now=NOW)
    held, done = await svc.apply_order_evidence(
        evidence(intent), broker_position_amt=D("0.6"), now=NOW
    )
    assert (held.state, done.state) == ("holding", "evidenced")


async def test_acknowledge_and_settle_only_from_their_states(svc) -> None:
    _, intent = await to_sending(svc)
    await svc.acknowledge(
        intent.client_order_id, broker_order_id="42", broker_status="NEW", now=NOW
    )
    with pytest.raises(TrendStateBlocked):
        await svc.acknowledge(
            intent.client_order_id, broker_order_id="42", broker_status="NEW", now=NOW
        )
    with pytest.raises(TrendStateBlocked):
        await svc.settle_intent(intent.client_order_id, now=NOW)
    with pytest.raises(TrendStateBlocked):
        await svc.acknowledge(
            "trend-none", broker_order_id="", broker_status="", now=NOW
        )


async def test_record_rejected_blocks_an_entry_and_keeps_an_exit_position(svc) -> None:
    signal, intent = await to_sending(svc)
    blocked = await svc.record_rejected(
        intent.client_order_id, broker_status="http_400:-2019", now=NOW
    )
    assert blocked.state == "blocked"
    assert (await svc.get_intent(intent.client_order_id)).state == "rejected"
    held, _ = await to_holding(svc)
    exit_intent = await svc.reserve_exit_intent(
        signal_key=held.signal_key, reason=EXIT_HARD_STOP, qty=D("0.6"), now=NOW
    )
    await svc.mark_sending(exit_intent.client_order_id, now=NOW)
    kept = await svc.record_rejected(
        exit_intent.client_order_id, broker_status="http_400:-2022", now=NOW
    )
    assert kept.state == "holding"
    retry = await svc.reserve_exit_intent(
        signal_key=held.signal_key, reason=EXIT_HARD_STOP, qty=D("0.6"), now=NOW
    )
    assert retry.client_order_id != exit_intent.client_order_id  # a new leg key


async def test_exit_reason_is_validated_before_any_write(svc) -> None:
    held, _ = await to_holding(svc)
    for bad in ("tp1", "bar_close_stop", "", "HARD_STOP"):
        with pytest.raises(TrendStateBlocked, match="exit reason"):
            await svc.reserve_exit_intent(
                signal_key=held.signal_key, reason=bad, qty=D("0.1"), now=NOW
            )
    assert await svc.list_unresolved_intents() == ()


async def test_exit_needs_a_held_signal_and_a_bounded_quantity(svc) -> None:
    signal, _ = await to_sending(svc)
    with pytest.raises(TrendStateBlocked, match="held"):
        await svc.reserve_exit_intent(
            signal_key=signal.signal_key, reason=EXIT_HARD_STOP, qty=D("0.1"), now=NOW
        )
    held, _ = await to_holding(svc)
    with pytest.raises(TrendStateBlocked, match="exceeds"):
        await svc.reserve_exit_intent(
            signal_key=held.signal_key, reason=EXIT_HARD_STOP, qty=D("0.7"), now=NOW
        )


async def test_an_exit_fence_does_not_re_judge_entry_rules(svc) -> None:
    held, _ = await to_holding(svc)
    for symbol in ("ETHUSDT", "SOLUSDT"):  # the daily kill switch is already tripped
        closed, _ = await to_holding(svc, side="SELL", symbol=symbol)
        exit_intent = await svc.reserve_exit_intent(
            signal_key=closed.signal_key, reason=EXIT_HARD_STOP, qty=D("0.6"), now=NOW
        )
        await svc.mark_sending(exit_intent.client_order_id, now=NOW)
        await svc.apply_order_evidence(
            evidence(exit_intent, symbol=symbol, avg="105"),
            broker_position_amt=D("0"),
            now=NOW,
        )
    exit_intent = await svc.reserve_exit_intent(
        signal_key=held.signal_key, reason=EXIT_SIGNAL_FLIP, qty=D("0.6"), now=NOW
    )
    fenced = await svc.mark_sending(exit_intent.client_order_id, now=NOW)
    assert fenced.state == "sending"  # exits never wait for entry capacity


async def test_a_full_exit_closes_with_reason_pnl_and_flat_position(svc) -> None:
    held, _ = await to_holding(svc)
    exit_intent = await svc.reserve_exit_intent(
        signal_key=held.signal_key, reason=EXIT_HARD_STOP, qty=D("0.6"), now=NOW
    )
    assert exit_intent.reduce_only and exit_intent.side == "SELL"
    await svc.mark_sending(exit_intent.client_order_id, now=NOW)
    with pytest.raises(TrendStateBlocked, match="position"):
        await svc.apply_order_evidence(
            evidence(exit_intent, avg="95"), broker_position_amt=D("0.6"), now=NOW
        )
    closed, done = await svc.apply_order_evidence(
        evidence(exit_intent, avg="95"), broker_position_amt=D("0"), now=NOW
    )
    assert closed.state == "closed" and closed.exit_reason == EXIT_HARD_STOP
    assert closed.realized_pnl_usdt == D("-3.0")  # (95 - 100) * 0.6, long
    assert done.state == "evidenced"
    assert await svc.list_active_signals() == ()


async def test_a_partial_exit_leaves_the_remainder_held(svc) -> None:
    held, _ = await to_holding(svc)
    exit_intent = await svc.reserve_exit_intent(
        signal_key=held.signal_key, reason=EXIT_SIGNAL_FLIP, qty=D("0.5"), now=NOW
    )
    await svc.mark_sending(exit_intent.client_order_id, now=NOW)
    partial, _ = await svc.apply_order_evidence(
        evidence(exit_intent), broker_position_amt=D("0.1"), now=NOW
    )
    assert partial.state == "holding" and partial.closed_qty == D("0.5")
    assert partial.remaining_qty == D("0.1") and partial.exit_reason is None
