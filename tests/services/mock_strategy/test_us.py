"""Observable order and episode behavior with fake persistence and paper broker."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.models.mock_strategy import OPEN_STATUSES, MockStrategyOrder
from app.services.brokers.alpaca.schemas import Asset, Order
from app.services.mock_strategy.us import UsPaperStrategy, plan_orders
from app.services.mock_strategy.us_broker import PreDispatchRefusal
from app.services.mock_strategy.us_signals import Candidate, UsSelection, session_times

pytestmark = pytest.mark.asyncio
DAY = date(2026, 11, 2)


def selection(day=DAY, horizon=5):
    return UsSelection(
        day,
        date(2026, 10, 30),
        horizon,
        (Candidate("AAPL", Decimal("123")),),
        (Candidate("NVDA", Decimal("123")),),
        "fixture-prefix",
    )


def asset(symbol, **kwargs):
    return Asset(
        id=f"asset-{symbol}",
        symbol=symbol,
        status="active",
        tradable=True,
        fractionable=True,
        shortable=True,
        easy_to_borrow=True,
        **kwargs,
    )


class MemoryRecorder:
    def __init__(self):
        self.rows = {}
        self.fail_updates = False

    @asynccontextmanager
    async def account_lock(self, **kwargs):
        yield

    async def record_many(self, intents):
        return [await self.record(i) for i in intents]

    async def record(self, intent):
        if intent.client_order_id not in self.rows:
            self.rows[intent.client_order_id] = MockStrategyOrder(
                **asdict(intent),
                id=len(self.rows) + 1,
                status="planned",
                broker_order_id=None,
                opened_at=None,
                closed_at=None,
                entry_price=None,
                exit_price=None,
                realized_pnl=None,
            )
        return self.rows[intent.client_order_id]

    async def get(self, client_order_id):
        return self.rows.get(client_order_id)

    async def claim(self, client_order_id):
        row = self.rows[client_order_id]
        if row.status != "planned":
            return False
        row.status = "sending"
        return True

    async def update(self, client_order_id, *, status, meta=None, **kwargs):
        if self.fail_updates:
            raise RuntimeError("fixture database unavailable")
        row = self.rows[client_order_id]
        row.status = status
        row.meta = row.meta | (meta or {})
        for key, value in kwargs.items():
            if value is not None:
                setattr(row, key, value)
        return row

    async def list_orders(self, *, venue, account_ref=None, strategy_id=None):
        return [
            r
            for r in self.rows.values()
            if r.venue == venue
            and (account_ref is None or r.account_ref == account_ref)
            and (strategy_id is None or r.strategy_id == strategy_id)
        ]

    async def list_due(self, *, asof, **kwargs):
        return [
            r
            for r in await self.list_orders(**kwargs)
            if r.status in OPEN_STATUSES
            and r.side in {"buy", "short"}
            and r.exit_due_at is not None
            and r.exit_due_at <= asof
        ]


class PaperBroker:
    def __init__(self):
        self.account_ref = "fixture-paper"
        self.orders = {}
        self.posts = []
        self.fail_submit = False
        self.accept_only = False
        self.mismatch = False
        self.asset_map = {s: asset(s) for s in ("AAPL", "NVDA")}

    async def preflight(self, day):
        return self.account_ref

    async def assets(self):
        return self.asset_map

    async def lookup(self, coid):
        return self.orders.get(coid)

    async def prepare(self, request, *, side, asof, confirm):
        assert confirm is True

    async def submit(self, request, *, side, asof, confirm):
        assert confirm is True
        self.posts.append((side, request))
        if self.fail_submit:
            raise TimeoutError("fixture lost response")
        qty = request.qty or request.notional / Decimal("100")
        exit_price = Decimal("110") if side == "sell" else Decimal("90")
        order = Order(
            **request.model_dump(exclude={"symbol"}),
            symbol="WRONG" if self.mismatch else request.symbol,
            id=f"order-{len(self.posts)}",
            status="accepted" if self.accept_only else "filled",
            filled_qty=Decimal(0) if self.accept_only else qty,
            filled_avg_price=None
            if self.accept_only
            else exit_price
            if side in {"sell", "cover"}
            else Decimal("100"),
            filled_at=None
            if self.accept_only
            else datetime.combine(asof, datetime.min.time(), tzinfo=UTC),
        )
        self.orders[request.client_order_id] = order
        return order


async def run(strategy, **kwargs):
    return await strategy.run(
        asof=DAY, selection=selection, top_n=1, confirm=True, **kwargs
    )


async def test_idempotent_rerun_reuses_frozen_plan_and_never_duplicates():
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    await run(strategy)
    forbidden_rebuild = Mock(side_effect=AssertionError("must reuse committed plan"))
    result = await strategy.run(
        asof=DAY, selection=forbidden_rebuild, top_n=1, confirm=True
    )
    assert len(broker.posts) == 2
    assert result["plan_orders"] == 2
    assert {row.status for row in recorder.rows.values()} == {"filled"}
    forbidden_rebuild.assert_not_called()
    with pytest.raises(ValueError, match="parameters differ"):
        await run(strategy, notional=Decimal("9999"))


async def test_short_eligibility_and_whole_shares():
    signals, broker = selection(), PaperBroker()
    plan = plan_orders(signals, broker.asset_map, account_ref="fixture")
    long, short = plan.orders
    assert long.notional == Decimal("10000") and long.qty is None
    assert short.qty == Decimal("81") and short.notional is None
    broker.asset_map["NVDA"].easy_to_borrow = False
    plan = plan_orders(signals, broker.asset_map, account_ref="fixture")
    assert [row.side for row in plan.orders] == ["buy"]
    assert plan.skipped == (
        {"symbol": "NVDA", "side": "short", "reason": "not_easy_to_borrow"},
    )


@pytest.mark.parametrize("mode", ["timeout", "bad_echo", "crash_after_claim"])
async def test_ambiguous_sends_are_never_replayed(mode):
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    if mode == "crash_after_claim":
        for intent in plan_orders(
            selection(), broker.asset_map, account_ref=broker.account_ref
        ).orders:
            await recorder.record(intent)
            await recorder.claim(intent.client_order_id)
    else:
        broker.fail_submit = mode == "timeout"
        broker.mismatch = mode == "bad_echo"
        await run(strategy)
    count = len(broker.posts)
    broker.fail_submit = False
    broker.orders.clear()  # no lookup evidence is NOT permission to send
    await run(strategy)
    assert len(broker.posts) == count
    assert {row.status for row in recorder.rows.values()} <= {"sending", "uncertain"}


async def test_result_commit_failure_leaves_committed_send_fence():
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    recorder.fail_updates = True
    with pytest.raises(RuntimeError):
        await run(strategy)
    first = next(iter(recorder.rows.values()))
    assert first.status == "sending" and len(broker.posts) == 1
    recorder.fail_updates = False
    await run(strategy)
    assert len(broker.posts) == 2  # second planned symbol; first only reconciled


async def test_close_due_closes_exactly_due_long_and_short_and_preserves_future():
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    await run(strategy)
    future = replace(selection(date(2026, 11, 3)), signal_date=DAY)
    await strategy.run(
        asof=future.asof, selection=lambda: future, top_n=1, confirm=True
    )
    result = await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    assert len(result["orders"]) == 2
    assert {o["episode_status"] for o in result["orders"]} == {"closed"}
    assert [side for side, _ in broker.posts] == [
        "buy",
        "short",
        "buy",
        "short",
        "sell",
        "cover",
    ]
    entries = [r for r in recorder.rows.values() if r.side in {"buy", "short"}]
    assert [r.status for r in entries] == ["closed", "closed", "filled", "filled"]
    assert [r.realized_pnl for r in entries[:2]] == [Decimal("1000"), Decimal("810")]
    await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    assert len(broker.posts) == 6


@pytest.mark.parametrize(
    "failure", ["corrupt_fill", "record_conflict", "outcome_store"]
)
async def test_bad_due_entry_does_not_block_independent_exit(failure):
    class FailingRecorder(MemoryRecorder):
        blocked_id = None

        async def record(self, intent):
            if failure == "record_conflict" and intent.side == "sell":
                raise ValueError("synthetic exit intent conflict")
            return await super().record(intent)

        async def update(self, client_order_id, **kwargs):
            if failure == "outcome_store" and client_order_id == self.blocked_id:
                raise RuntimeError("synthetic outcome store unavailable")
            return await super().update(client_order_id, **kwargs)

    recorder, broker = FailingRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    await run(strategy)
    bad = next(r for r in recorder.rows.values() if r.side == "buy")
    recorder.blocked_id = bad.client_order_id
    if failure == "corrupt_fill":
        bad.meta = {**bad.meta, "filled_qty": "not-a-decimal"}
        del broker.orders[bad.client_order_id]
    result = await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    assert result["orders"][0]["episode_status"] == "unresolved"
    assert result["orders"][0]["error_class"] in {
        "InvalidOperation",
        "ValueError",
        "RuntimeError",
    }
    assert result["orders"][1]["episode_status"] == "closed"
    assert [side for side, _ in broker.posts] == ["buy", "short", "cover"]
    assert bad.status != "closed"


async def test_due_entry_cancellation_propagates_without_processing_other_entries():
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    await run(strategy)
    canceled = asyncio.CancelledError()

    async def canceled_lookup(coid):
        raise canceled

    broker.lookup = canceled_lookup
    with pytest.raises(asyncio.CancelledError) as caught:
        await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    assert caught.value is canceled
    assert len(broker.posts) == 2


async def test_accepted_is_not_fill_and_partial_terminal_entry_closes_only_proven_qty():
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    broker.accept_only = True
    await run(strategy)
    result = await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    assert len(broker.posts) == 2 and all(
        o["reason"] == "entry_not_terminal_with_fill" for o in result["orders"]
    )
    entry = next(r for r in recorder.rows.values() if r.side == "short")
    original = broker.orders[entry.client_order_id]
    broker.orders[entry.client_order_id] = original.model_copy(
        update={
            "status": "canceled",
            "filled_qty": Decimal("7"),
            "filled_avg_price": Decimal("100"),
        }
    )
    broker.accept_only = False
    result = await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    assert broker.posts[-1][0] == "cover" and broker.posts[-1][1].qty == Decimal("7")
    assert entry.status == "closed" and entry.realized_pnl == Decimal("70")


async def test_close_ack_waits_for_fill_evidence_without_resubmission():
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    await run(strategy)
    broker.accept_only = True
    result = await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    assert all(
        o["episode_status"] == "awaiting_close_evidence" for o in result["orders"]
    )
    assert all(r.realized_pnl is None for r in recorder.rows.values())
    for row in recorder.rows.values():
        if row.side in {"sell", "cover"}:
            broker.orders[row.client_order_id] = broker.orders[
                row.client_order_id
            ].model_copy(
                update={
                    "status": "filled",
                    "filled_qty": row.qty,
                    "filled_avg_price": Decimal("105"),
                }
            )
    await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    assert len(broker.posts) == 4
    assert {r.status for r in recorder.rows.values()} == {"closed"}


async def test_new_evidence_mismatch_cannot_close_using_older_fill_metadata():
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    await run(strategy)
    entry = next(row for row in recorder.rows.values() if row.side == "buy")
    broker.orders[entry.client_order_id] = broker.orders[
        entry.client_order_id
    ].model_copy(update={"symbol": "WRONG"})
    await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    assert entry.status == "uncertain" and entry.realized_pnl is None
    assert [side for side, _ in broker.posts] == ["buy", "short", "cover"]


@pytest.mark.parametrize(
    "terminal,filled",
    [("canceled", "0"), ("expired", "0"), ("rejected", "0"), ("canceled", "40")],
)
async def test_terminal_exit_opens_only_a_new_durable_residual_leg(terminal, filled):
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    await run(strategy)
    entry = next(r for r in recorder.rows.values() if r.side == "buy")
    broker.accept_only = True
    await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    first = next(r for r in recorder.rows.values() if r.side == "sell")
    first_id = first.client_order_id
    broker.orders[first_id] = broker.orders[first_id].model_copy(
        update={
            "status": terminal,
            "filled_qty": Decimal(filled),
            "filled_avg_price": Decimal("105") if Decimal(filled) else None,
        }
    )
    broker.accept_only = False
    await strategy.close_due(asof=date(2026, 11, 10), confirm=True)
    exits = [r for r in recorder.rows.values() if r.side == "sell"]
    assert len(exits) == 2
    residual = exits[-1]
    assert residual.client_order_id != first_id
    assert residual.qty == Decimal("100") - Decimal(filled)
    assert residual.meta["exit_leg"] == 1
    assert entry.status == "closed"
    assert entry.realized_pnl == Decimal("1000") - Decimal(filled) * Decimal("5")
    assert entry.exit_price == Decimal("110") - Decimal(filled) / Decimal("20")
    assert sum(req.client_order_id == first_id for _, req in broker.posts) == 1
    count = len(broker.posts)
    await strategy.close_due(asof=date(2026, 11, 10), confirm=True)
    assert len(broker.posts) == count


@pytest.mark.parametrize(
    "state", ["nonterminal_partial", "timeout", "missing_fill_evidence"]
)
async def test_unknown_or_nonterminal_exit_never_gets_another_leg(state):
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    await run(strategy)
    broker.accept_only = True
    broker.fail_submit = state == "timeout"
    await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    for row in recorder.rows.values():
        if row.side in {"sell", "cover"} and row.client_order_id in broker.orders:
            broker.orders[row.client_order_id] = broker.orders[
                row.client_order_id
            ].model_copy(
                update={
                    "status": "partially_filled"
                    if state == "nonterminal_partial"
                    else "canceled",
                    "filled_qty": Decimal("1")
                    if state == "nonterminal_partial"
                    else None,
                    "filled_avg_price": Decimal("105"),
                }
            )
    count = len(broker.posts)
    broker.fail_submit = False
    await strategy.close_due(asof=date(2026, 11, 10), confirm=True)
    assert len(broker.posts) == count
    assert len([r for r in recorder.rows.values() if r.side in {"sell", "cover"}]) == 2


async def test_late_no_post_exit_refusal_gets_a_new_residual_leg(monkeypatch):
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    await run(strategy)
    submit = broker.submit

    async def refusal(request, *, side, **kwargs):
        if side == "sell":
            raise PreDispatchRefusal("fixture late pre-POST refusal")
        return await submit(request, side=side, **kwargs)

    monkeypatch.setattr(broker, "submit", refusal)
    await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    first = next(row for row in recorder.rows.values() if row.side == "sell")
    assert first.status == "rejected" and not first.broker_order_id
    monkeypatch.setattr(broker, "submit", submit)
    await strategy.close_due(asof=date(2026, 11, 10), confirm=True)
    exits = [row for row in recorder.rows.values() if row.side == "sell"]
    assert len(exits) == 2 and exits[1].meta["exit_leg"] == 1
    assert exits[1].qty == Decimal("100")
    assert (
        next(row for row in recorder.rows.values() if row.side == "buy").status
        == "closed"
    )


async def test_unknown_residual_leg_keeps_its_reservation_across_reruns():
    recorder, broker = MemoryRecorder(), PaperBroker()
    strategy = UsPaperStrategy(recorder, broker)
    await run(strategy)
    broker.accept_only = True
    await strategy.close_due(asof=date(2026, 11, 9), confirm=True)
    first = next(row for row in recorder.rows.values() if row.side == "sell")
    broker.orders[first.client_order_id] = broker.orders[
        first.client_order_id
    ].model_copy(
        update={
            "status": "canceled",
            "filled_qty": Decimal("40"),
            "filled_avg_price": Decimal("105"),
        }
    )
    broker.fail_submit = True
    await strategy.close_due(asof=date(2026, 11, 10), confirm=True)
    exits = [row for row in recorder.rows.values() if row.side == "sell"]
    assert len(exits) == 2 and exits[1].status == "uncertain"
    assert exits[1].qty == Decimal("60")
    posts = len(broker.posts)
    broker.fail_submit = False
    await strategy.close_due(asof=date(2026, 11, 11), confirm=True)
    assert len(broker.posts) == posts
    assert len([row for row in recorder.rows.values() if row.side == "sell"]) == 2


async def test_post_dispatch_cancellation_preserves_original_even_when_db_updates_fail(
    monkeypatch,
):
    recorder, broker = MemoryRecorder(), PaperBroker()
    intent = plan_orders(
        selection(), broker.asset_map, account_ref=broker.account_ref
    ).orders[0]
    row = await recorder.record(intent)
    recorder.fail_updates = True
    stop = asyncio.CancelledError("fixture cancellation")

    async def canceled(*args, **kwargs):
        raise stop

    monkeypatch.setattr(broker, "submit", canceled)
    with pytest.raises(asyncio.CancelledError) as caught:
        await UsPaperStrategy(recorder, broker)._execute(row, DAY)
    assert caught.value is stop and row.status == "sending"
    assert not await recorder.claim(row.client_order_id)


async def test_trading_day_exit_uses_holidays_and_requires_confirm():
    _, _, due = session_times(date(2026, 11, 25), 1)
    assert due == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)  # Thanksgiving excluded
    strategy = UsPaperStrategy(MemoryRecorder(), PaperBroker())
    with pytest.raises(ValueError, match="confirm"):
        await strategy.run(asof=DAY, selection=selection)
