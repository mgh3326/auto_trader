"""ROB-993 — Binance Demo strategy-loop order round trip.

Opens a MARKET order sized to the accepted ``Signal``, then immediately
closes it with a reduceOnly MARKET order — the same open + reduceOnly-close
shape the ROB-298 PR 2 smoke CLI (``scripts/binance_futures_demo_smoke.py``)
proves, adapted to be driven by a plugin ``Signal`` instead of CLI args.

Position-hold-until-TP/SL is strategy-specific (the S3 adapter, a later
commit) — this infra PR proves the execution + ledger + correlation +
forecast wiring end to end with an immediate round trip ("주문 1건 데모
왕복" per the ROB-993 verification AC).

Every ROB-298 safety property is inherited unchanged because this module
calls the same ``BinanceFuturesDemoExecutionClient`` the smoke CLI uses:
demo-fapi-only host (enforced by the client itself), leverage pinned to
1x with echo verification, One-way position mode required, reduceOnly on
the close leg, root reservation before any broker call (ROB-844), a
bounded fill-proof poll (never advances the ledger past ``submitted`` on
an unproven fill, ROB-305 §4), and fail-closed anomaly recording on any
broker/ledger disagreement.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.futures_demo.dto import FuturesDemoOrderSubmitResult
from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoHedgeModeBlocked,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)

from .sizing import quantize_qty
from .strategy import Signal

logger = logging.getLogger(__name__)

_FILL_RECONCILE_MAX_POLLS = 5
_FILL_RECONCILE_DELAY_SECONDS = 1.0
_TERMINAL_NONFILL_STATUSES = frozenset({"CANCELED", "REJECTED", "EXPIRED"})


class RoundTripBlocked(Exception):
    """Root reservation lost the exposure-slot race (ROB-844)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RoundTripResult:
    open_client_order_id: str
    close_client_order_id: str
    symbol: str
    side: str
    qty: Decimal
    reconciled: bool
    open_broker_order_id: str | None = None
    close_broker_order_id: str | None = None


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _poll_order_filled(
    execution: BinanceFuturesDemoExecutionClient,
    *,
    symbol: str,
    client_order_id: str,
) -> bool:
    """Bounded ``GET /fapi/v1/order`` poll. ``True`` iff FILLED observed.

    A submit-response ``NEW`` is not a final state (ROB-305 §4). Never an
    unbounded retry loop; a transient query error keeps polling within the
    bound rather than aborting on the first error.
    """
    for attempt in range(_FILL_RECONCILE_MAX_POLLS):
        if attempt > 0:
            await asyncio.sleep(_FILL_RECONCILE_DELAY_SECONDS)
        try:
            status_result = await execution.get_order(
                symbol=symbol, client_order_id=client_order_id
            )
        except Exception as exc:  # noqa: BLE001 — transient; keep polling within bound
            logger.warning(
                "strategy_loop fill poll attempt=%d failed (cid=%s): %s",
                attempt,
                client_order_id,
                exc,
            )
            continue
        if status_result.status == "FILLED":
            return True
        if status_result.status in _TERMINAL_NONFILL_STATUSES:
            return False
    return False


async def execute_signal_round_trip(
    *,
    execution: BinanceFuturesDemoExecutionClient,
    ledger: BinanceDemoLedgerService,
    session: AsyncSession,
    signal: Signal,
    instrument_id: int,
    venue_host: str,
    qty: Decimal,
    notional_usdt: Decimal,
    leverage: int,
    strategy_loop_tag: str,
    correlation_id: str,
    open_client_order_id: str,
    close_client_order_id: str,
    close_step_size: Decimal,
    close_quantity_precision: int | None,
    global_open_root_cap: int = 1,
) -> RoundTripResult:
    """Root-reserve, open MARKET, then close reduceOnly MARKET.

    Raises ``RoundTripBlocked`` if the exposure slot could not be claimed
    (a concurrent loop/tick won the race — zero broker calls follow) or
    ``BinanceFuturesDemoHedgeModeBlocked`` if the account is not in
    One-way mode. Any broker/ledger disagreement records a ``anomaly`` row
    and re-raises — this function never reports a clean result on an
    unproven fill or a non-flat post-close position.
    """
    symbol = signal.symbol.upper()
    side = signal.side
    now = _now_utc()
    metadata = {
        "source": "rob-993-strategy-loop",
        "role": "open",
        "leverage": leverage,
        "strategy_loop_tag": strategy_loop_tag,
        "strategy_id": signal.strategy_id,
        "correlation_id": correlation_id,
        "signal_reason": signal.reason,
        "decision_ts": signal.decision_ts,
    }

    reservation = await ledger.reserve_root_planned(
        instrument_id=instrument_id,
        product="usdm_futures",
        venue_host=venue_host,
        client_order_id=open_client_order_id,
        side=side,
        order_type="MARKET",
        qty=qty,
        price=None,
        notional_usdt=notional_usdt,
        extra_metadata=metadata,
        global_open_root_cap=global_open_root_cap,
        now=now,
    )
    if reservation.status != "reserved":
        raise RoundTripBlocked(reservation.reason or "exposure_slot_taken")

    async def _release(reason: str) -> None:
        evidence = {"pre_submit_release_reason": reason}
        await ledger.record_cancelled(
            client_order_id=open_client_order_id,
            now=_now_utc(),
            extra_metadata_merge=evidence,
        )
        await ledger.record_reconciled(
            client_order_id=open_client_order_id,
            now=_now_utc(),
            extra_metadata_merge=evidence,
        )
        await session.commit()

    # 1. Position-mode check — One-way required (inherited ROB-298 PR 2 boundary).
    try:
        mode_result = await execution.get_position_mode()
    except Exception:
        await _release("position_mode_query_failed")
        raise
    if mode_result.is_hedge_mode:
        await _release("hedge_mode_blocked")
        raise BinanceFuturesDemoHedgeModeBlocked(
            "strategy loop requires One-way position mode"
        )

    # 2. Leverage pinned to 1x — set_leverage raises on any non-1 request or
    #    echo mismatch (BinanceFuturesDemoLeverageMismatch), before this call
    #    can proceed further.
    await execution.set_leverage(symbol=symbol, leverage=leverage)

    # 3. PREVIEWED — local preview, no HTTP.
    execution.preview_submit(
        symbol=symbol,
        side=side,
        order_type="MARKET",
        qty=qty,
        client_order_id=open_client_order_id,
        reduce_only=False,
    )
    await ledger.record_previewed(client_order_id=open_client_order_id, now=_now_utc())
    await session.commit()

    # 4. VALIDATED — POST /fapi/v1/order/test (no placement).
    try:
        await execution.order_test(
            symbol=symbol, side=side, order_type="MARKET", qty=qty
        )
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=open_client_order_id,
            reason=f"order_test_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        raise
    await ledger.record_validated(client_order_id=open_client_order_id, now=_now_utc())
    await session.commit()

    # 5. SUBMITTED — signed POST /fapi/v1/order (real Demo placement; open).
    try:
        submit_result = await execution.submit_order(
            symbol=symbol,
            side=side,
            order_type="MARKET",
            qty=qty,
            client_order_id=open_client_order_id,
            reduce_only=False,
            confirm=True,
        )
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=open_client_order_id,
            reason=f"submit_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        raise
    assert isinstance(submit_result, FuturesDemoOrderSubmitResult)
    await ledger.record_submitted(
        client_order_id=open_client_order_id,
        broker_order_id=submit_result.broker_order_id,
        now=_now_utc(),
        extra_metadata_merge={"submit_status": submit_result.status},
    )
    await session.commit()

    # 6. Resolve the OPEN fill (ROB-305 §4) — never advance past `submitted`
    #    on a bare submit-response NEW.
    open_fill_proven = submit_result.status == "FILLED"
    if not open_fill_proven and submit_result.status not in _TERMINAL_NONFILL_STATUSES:
        open_fill_proven = await _poll_order_filled(
            execution, symbol=symbol, client_order_id=open_client_order_id
        )
    if open_fill_proven:
        await ledger.record_filled(client_order_id=open_client_order_id, now=_now_utc())
        await session.commit()

    pre_close_pos = await execution.get_position(symbol=symbol)
    if pre_close_pos.is_flat:
        await ledger.record_anomaly(
            client_order_id=open_client_order_id,
            reason=f"open_did_not_take_effect: status={submit_result.status}",
            now=_now_utc(),
        )
        await session.commit()
        raise RuntimeError(
            f"strategy loop open did not take effect for cid={open_client_order_id}"
        )
    if not open_fill_proven:
        await ledger.record_filled(
            client_order_id=open_client_order_id,
            now=_now_utc(),
            extra_metadata_merge={"fill_evidence": "position_risk_nonflat"},
        )
        await session.commit()

    close_side = "SELL" if side == "BUY" else "BUY"
    close_qty = quantize_qty(
        abs(pre_close_pos.position_amt),
        step_size=close_step_size,
        quantity_precision=close_quantity_precision,
    )

    return await _close_with_reduce_only(
        execution=execution,
        ledger=ledger,
        session=session,
        venue_host=venue_host,
        instrument_id=instrument_id,
        open_client_order_id=open_client_order_id,
        close_client_order_id=close_client_order_id,
        symbol=symbol,
        open_side=side,
        open_broker_order_id=submit_result.broker_order_id,
        close_side=close_side,
        close_qty=close_qty,
        notional_usdt=notional_usdt,
        leverage=leverage,
        strategy_loop_tag=strategy_loop_tag,
        correlation_id=correlation_id,
    )


async def _close_with_reduce_only(
    *,
    execution: BinanceFuturesDemoExecutionClient,
    ledger: BinanceDemoLedgerService,
    session: AsyncSession,
    venue_host: str,
    instrument_id: int,
    open_client_order_id: str,
    close_client_order_id: str,
    symbol: str,
    open_side: str,
    open_broker_order_id: str,
    close_side: str,
    close_qty: Decimal,
    notional_usdt: Decimal,
    leverage: int,
    strategy_loop_tag: str,
    correlation_id: str,
) -> RoundTripResult:
    now = _now_utc()
    await ledger.record_planned(
        instrument_id=instrument_id,
        product="usdm_futures",
        venue_host=venue_host,
        client_order_id=close_client_order_id,
        side=close_side,
        order_type="MARKET",
        qty=close_qty,
        price=None,
        notional_usdt=notional_usdt,
        parent_client_order_id=open_client_order_id,
        extra_metadata={
            "source": "rob-993-strategy-loop",
            "role": "close",
            "reduce_only": True,
            "leverage": leverage,
            "strategy_loop_tag": strategy_loop_tag,
            "correlation_id": correlation_id,
        },
        now=now,
    )
    await session.commit()

    await ledger.record_previewed(client_order_id=close_client_order_id, now=_now_utc())
    await session.commit()

    try:
        await execution.order_test(
            symbol=symbol,
            side=close_side,
            order_type="MARKET",
            qty=close_qty,
            reduce_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=close_client_order_id,
            reason=f"close_order_test_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        raise
    await ledger.record_validated(client_order_id=close_client_order_id, now=_now_utc())
    await session.commit()

    try:
        close_result = await execution.submit_order(
            symbol=symbol,
            side=close_side,
            order_type="MARKET",
            qty=close_qty,
            client_order_id=close_client_order_id,
            reduce_only=True,
            confirm=True,
        )
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=close_client_order_id,
            reason=f"close_submit_failed: {exc}",
            now=_now_utc(),
        )
        await ledger.record_anomaly(
            client_order_id=open_client_order_id,
            reason=f"close_submit_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        raise
    assert isinstance(close_result, FuturesDemoOrderSubmitResult)
    await ledger.record_submitted(
        client_order_id=close_client_order_id,
        broker_order_id=close_result.broker_order_id,
        now=_now_utc(),
        extra_metadata_merge={
            "submit_status": close_result.status,
            "reduce_only": True,
        },
    )
    await session.commit()

    close_fill_proven = close_result.status == "FILLED"
    if not close_fill_proven and close_result.status not in _TERMINAL_NONFILL_STATUSES:
        close_fill_proven = await _poll_order_filled(
            execution, symbol=symbol, client_order_id=close_client_order_id
        )
    if close_fill_proven:
        await ledger.record_filled(
            client_order_id=close_client_order_id, now=_now_utc()
        )
        await session.commit()

    exit_metadata = {
        "exit_reason": "immediate_close",
        "strategy_loop_tag": strategy_loop_tag,
    }
    await ledger.record_closed(
        client_order_id=open_client_order_id,
        now=_now_utc(),
        extra_metadata_merge=exit_metadata,
    )
    await session.commit()

    return await _reconcile(
        execution=execution,
        ledger=ledger,
        session=session,
        open_client_order_id=open_client_order_id,
        close_client_order_id=close_client_order_id,
        symbol=symbol,
        side=open_side,
        qty=close_qty,
        close_fill_proven=close_fill_proven,
        open_broker_order_id=open_broker_order_id,
        close_broker_order_id=close_result.broker_order_id,
    )


async def _reconcile(
    *,
    execution: BinanceFuturesDemoExecutionClient,
    ledger: BinanceDemoLedgerService,
    session: AsyncSession,
    open_client_order_id: str,
    close_client_order_id: str,
    symbol: str,
    side: str,
    qty: Decimal,
    close_fill_proven: bool,
    open_broker_order_id: str | None,
    close_broker_order_id: str | None,
) -> RoundTripResult:
    """Reconciliation gate: open_orders empty AND position flat.

    Even when the account is flat with zero open orders, a clean result
    requires the close fill to have been proven (ROB-305 §4) — otherwise
    a safe anomaly is recorded and this function raises rather than
    reporting a silent fake success.
    """
    open_orders = await execution.get_open_orders(symbol=symbol)
    if open_orders.orders:
        residual_cids = [o.client_order_id for o in open_orders.orders]
        await ledger.record_anomaly(
            client_order_id=open_client_order_id,
            reason=f"open_orders_residual: {residual_cids!r}",
            now=_now_utc(),
        )
        await session.commit()
        raise RuntimeError(
            f"strategy loop close left residual open orders: {residual_cids!r}"
        )

    post_pos = await execution.get_position(symbol=symbol)
    if not post_pos.is_flat:
        await ledger.record_anomaly(
            client_order_id=open_client_order_id,
            reason=f"position_not_flat_after_close: amt={post_pos.position_amt}",
            now=_now_utc(),
        )
        await session.commit()
        raise RuntimeError(
            f"strategy loop close did not flatten position: amt={post_pos.position_amt}"
        )

    await ledger.record_reconciled(client_order_id=open_client_order_id, now=_now_utc())
    await session.commit()

    if not close_fill_proven:
        await ledger.record_anomaly(
            client_order_id=close_client_order_id,
            reason=(
                "close_fill_unproven_after_flat_reconcile: position flat and "
                "open orders 0, but close order never observed FILLED"
            ),
            now=_now_utc(),
        )
        await session.commit()
        raise RuntimeError(
            f"close fill unproven though account flat (cid={close_client_order_id})"
        )

    try:
        await ledger.record_closed(
            client_order_id=close_client_order_id, now=_now_utc()
        )
        await ledger.record_reconciled(
            client_order_id=close_client_order_id, now=_now_utc()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "close-row reconcile non-fatal: %s (cid=%s)", exc, close_client_order_id
        )
    await session.commit()

    return RoundTripResult(
        open_client_order_id=open_client_order_id,
        close_client_order_id=close_client_order_id,
        symbol=symbol,
        side=side,
        qty=qty,
        reconciled=True,
        open_broker_order_id=open_broker_order_id,
        close_broker_order_id=close_broker_order_id,
    )
