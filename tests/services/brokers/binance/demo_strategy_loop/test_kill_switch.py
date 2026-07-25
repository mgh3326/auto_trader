"""ROB-993 — kill switch: pure evaluation + durable ledger snapshot."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.db import AsyncSessionLocal
from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.demo_strategy_loop.kill_switch import (
    KillSwitchReasonCode,
    KillSwitchSnapshot,
    StrategyLoopKillSwitchLimits,
    build_kill_switch_snapshot,
    evaluate_kill_switch,
)

_RESIDUE_PREFIX = "rob-993-kswtest-%"


async def _purge_residue() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(BinanceDemoOrderLedger).where(
                BinanceDemoOrderLedger.client_order_id.like(_RESIDUE_PREFIX)
            )
        )
        await db.commit()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_ledger_residue():
    """DB tests here commit real rows (shared test_db — ROB-842 hazard);
    purge this file's own ``rob-993-kswtest-*`` client_order_id prefix
    before and after every test so re-runs never collide on the unique
    ``client_order_id`` constraint."""
    await _purge_residue()
    yield
    await _purge_residue()


def test_evaluate_kill_switch_allows_clean_snapshot() -> None:
    decision = evaluate_kill_switch(
        snapshot=KillSwitchSnapshot(
            open_position_count=0, consecutive_stop_losses_today=0
        ),
        limits=StrategyLoopKillSwitchLimits(),
    )
    assert decision.allowed is True
    assert decision.reason_codes == ()


def test_evaluate_kill_switch_trips_on_open_position_cap() -> None:
    decision = evaluate_kill_switch(
        snapshot=KillSwitchSnapshot(
            open_position_count=1, consecutive_stop_losses_today=0
        ),
        limits=StrategyLoopKillSwitchLimits(max_concurrent_positions=1),
    )
    assert decision.allowed is False
    assert decision.reason_codes == (
        KillSwitchReasonCode.MAX_CONCURRENT_POSITIONS_REACHED,
    )


def test_evaluate_kill_switch_trips_on_consecutive_stop_losses() -> None:
    decision = evaluate_kill_switch(
        snapshot=KillSwitchSnapshot(
            open_position_count=0, consecutive_stop_losses_today=2
        ),
        limits=StrategyLoopKillSwitchLimits(max_consecutive_stop_losses_per_utc_day=2),
    )
    assert decision.allowed is False
    assert decision.reason_codes == (
        KillSwitchReasonCode.CONSECUTIVE_STOP_LOSS_LIMIT_REACHED,
    )


def test_evaluate_kill_switch_accumulates_all_reasons() -> None:
    decision = evaluate_kill_switch(
        snapshot=KillSwitchSnapshot(
            open_position_count=5, consecutive_stop_losses_today=9
        ),
        limits=StrategyLoopKillSwitchLimits(),
    )
    assert decision.allowed is False
    assert set(decision.reason_codes) == {
        KillSwitchReasonCode.MAX_CONCURRENT_POSITIONS_REACHED,
        KillSwitchReasonCode.CONSECUTIVE_STOP_LOSS_LIMIT_REACHED,
    }


async def _seed_root(
    ledger: BinanceDemoLedgerService,
    session,
    *,
    instrument_id: int,
    client_order_id: str,
    strategy_loop_tag: str,
    exit_reason: str,
    closed_at: dt.datetime,
) -> None:
    now = closed_at - dt.timedelta(minutes=5)
    await ledger.record_planned(
        instrument_id=instrument_id,
        product="usdm_futures",
        venue_host="demo-fapi.binance.com",
        client_order_id=client_order_id,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("10"),
        price=None,
        extra_metadata={"strategy_loop_tag": strategy_loop_tag},
        now=now,
    )
    await ledger.record_previewed(client_order_id=client_order_id, now=now)
    await ledger.record_validated(client_order_id=client_order_id, now=now)
    await ledger.record_submitted(
        client_order_id=client_order_id,
        broker_order_id=f"broker-{client_order_id}",
        now=now,
    )
    await ledger.record_filled(client_order_id=client_order_id, now=now)
    await ledger.record_closed(
        client_order_id=client_order_id,
        now=closed_at,
        extra_metadata_merge={"exit_reason": exit_reason},
    )
    await session.commit()


@pytest.mark.asyncio
async def test_build_kill_switch_snapshot_counts_consecutive_stop_losses(
    db_session,
) -> None:
    ledger = BinanceDemoLedgerService(db_session)
    instrument_id = await ledger.resolve_or_create_instrument(
        venue="binance",
        product="usdm_futures",
        venue_symbol="XRPUSDT",
        base_asset="XRP",
        quote_asset="USDT",
    )
    run_id = uuid.uuid4().hex[:8]
    tag = f"rob-993-strategy-loop-test-{run_id}"
    cid_prefix = f"rob-993-kswtest-{run_id}"
    now = dt.datetime.now(dt.UTC)
    day_start = dt.datetime.combine(now.date(), dt.time.min, tzinfo=dt.UTC)

    # Oldest -> newest: SL, SL, TAKE_PROFIT (breaks the streak) -> most-recent-first
    # walk should count exactly the trailing (newest) non-SL-broken run, i.e. 0,
    # because the *most recent* close is the take-profit.
    await _seed_root(
        ledger,
        db_session,
        instrument_id=instrument_id,
        client_order_id=f"{cid_prefix}-open-1",
        strategy_loop_tag=tag,
        exit_reason="stop_loss",
        closed_at=day_start + dt.timedelta(hours=1),
    )
    await _seed_root(
        ledger,
        db_session,
        instrument_id=instrument_id,
        client_order_id=f"{cid_prefix}-open-2",
        strategy_loop_tag=tag,
        exit_reason="stop_loss",
        closed_at=day_start + dt.timedelta(hours=2),
    )
    await _seed_root(
        ledger,
        db_session,
        instrument_id=instrument_id,
        client_order_id=f"{cid_prefix}-open-3",
        strategy_loop_tag=tag,
        exit_reason="immediate_close",
        closed_at=day_start + dt.timedelta(hours=3),
    )

    snapshot = await build_kill_switch_snapshot(ledger, strategy_loop_tag=tag, now=now)
    assert snapshot.consecutive_stop_losses_today == 0

    # A second loop tag's SL streak is not double-counted.
    await _seed_root(
        ledger,
        db_session,
        instrument_id=instrument_id,
        client_order_id=f"{cid_prefix}-open-4",
        strategy_loop_tag=tag,
        exit_reason="stop_loss",
        closed_at=day_start + dt.timedelta(hours=4),
    )
    snapshot_after = await build_kill_switch_snapshot(
        ledger, strategy_loop_tag=tag, now=now
    )
    assert snapshot_after.consecutive_stop_losses_today == 1


@pytest.mark.asyncio
async def test_build_kill_switch_snapshot_ignores_other_strategy_tags(
    db_session,
) -> None:
    ledger = BinanceDemoLedgerService(db_session)
    instrument_id = await ledger.resolve_or_create_instrument(
        venue="binance",
        product="usdm_futures",
        venue_symbol="DOGEUSDT",
        base_asset="DOGE",
        quote_asset="USDT",
    )
    now = dt.datetime.now(dt.UTC)
    day_start = dt.datetime.combine(now.date(), dt.time.min, tzinfo=dt.UTC)
    run_id = uuid.uuid4().hex[:8]

    await _seed_root(
        ledger,
        db_session,
        instrument_id=instrument_id,
        client_order_id=f"rob-993-kswtest-{run_id}-other-tag-open-1",
        strategy_loop_tag=f"some-other-loop-{run_id}",
        exit_reason="stop_loss",
        closed_at=day_start + dt.timedelta(hours=1),
    )

    snapshot = await build_kill_switch_snapshot(
        ledger, strategy_loop_tag=f"rob-993-strategy-loop-{run_id}", now=now
    )
    assert snapshot.consecutive_stop_losses_today == 0
