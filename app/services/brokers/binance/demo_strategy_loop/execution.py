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

ROB-993 adversarial review (verify-993-2256.md) additionally hardened this
module against a shared-account (multiple consumers of the same Demo
credentials) threat model:

  * Finding 2 — a broker-flat pre-submit gate (fresh signed
    ``get_position``/``get_open_orders``) runs right after the root
    reservation and before ANY other broker call; a non-flat/non-empty
    symbol blocks the whole round trip with zero submits. The close leg's
    quantity is then computed from the position **delta** attributable to
    our own open fill (``post_open_position - baseline``), not the raw
    account-wide ``positionAmt`` — a delta that doesn't match what we
    submitted aborts before any close submit.
  * Finding 3 — the open root stays in the ``filled`` (blocking, ROB-844
    exposure-slot-occupying) lifecycle state until the close-side
    reconcile (open_orders empty AND position flat AND close fill proven)
    has fully passed; only then does it transition
    ``filled -> closed -> reconciled`` back to back. Any reconcile
    failure instead writes ``anomaly`` directly from ``filled`` — the
    root never passes through the non-blocking ``closed`` state on a
    failure path, and never releases the slot before broker state is
    verified even on the success path.
  * Finding 4 — every submit/poll response that will be trusted as
    order-shape or fill evidence is echo-verified against what was
    requested (symbol/side/client_order_id/qty/reduceOnly);
    ``BrokerEchoMismatch`` fails closed (anomaly + raise) on any
    disagreement rather than accepting a tampered/inconsistent response.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoOrderSubmitResult,
    FuturesDemoPositionResult,
)
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

# ROB-993 adversarial review (verify-993-2256.md, Finding 1) — the max
# concurrent position cap for this lane is a hard invariant (1), not a
# caller-tunable parameter. Previously exposed as a public
# ``execute_signal_round_trip(global_open_root_cap=...)`` default; removed
# entirely so there is no override surface at all.
_GLOBAL_OPEN_ROOT_CAP: Final[int] = 1


class RoundTripBlocked(Exception):
    """Root reservation lost the exposure-slot race (ROB-844), or the
    broker-flat pre-submit gate found a pre-existing position/open order on
    the shared Demo account (ROB-993 adversarial review Finding 2)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BrokerEchoMismatch(Exception):
    """The broker's submit/query response didn't echo back what we
    requested (symbol/side/client_order_id/qty/reduceOnly) — treated as
    tampering/anomaly, never a clean success (ROB-993 adversarial review
    Finding 4)."""

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


def _assert_order_echo(
    *,
    context: str,
    expected_symbol: str,
    expected_side: str,
    expected_client_order_id: str,
    expected_qty: Decimal,
    expected_reduce_only: bool,
    echoed_symbol: str,
    echoed_side: str,
    echoed_client_order_id: str,
    echoed_qty: Decimal,
    echoed_reduce_only: bool,
) -> None:
    """Raise :class:`BrokerEchoMismatch` if any echoed field disagrees with
    what was requested. Accumulates every mismatch (no short-circuit) so
    the anomaly record is fully auditable."""
    mismatches: list[str] = []
    if echoed_symbol != expected_symbol:
        mismatches.append(f"symbol {echoed_symbol!r} != {expected_symbol!r}")
    if echoed_side != expected_side:
        mismatches.append(f"side {echoed_side!r} != {expected_side!r}")
    if echoed_client_order_id != expected_client_order_id:
        mismatches.append(
            f"client_order_id {echoed_client_order_id!r} != {expected_client_order_id!r}"
        )
    if echoed_qty != expected_qty:
        mismatches.append(f"qty {echoed_qty!r} != {expected_qty!r}")
    if echoed_reduce_only != expected_reduce_only:
        mismatches.append(
            f"reduce_only {echoed_reduce_only!r} != {expected_reduce_only!r}"
        )
    if mismatches:
        raise BrokerEchoMismatch(f"{context}: " + "; ".join(mismatches))


async def _fetch_account_flat_snapshot(
    execution: BinanceFuturesDemoExecutionClient,
) -> tuple[list[FuturesDemoPositionResult], int]:
    """Fresh, account-wide (ALL symbols) position + open-order snapshot.

    ROB-993 R2 adversarial review (verify-993-r2-2329.md, Finding 2): the
    max-concurrent-positions=1 invariant is account-global, so the
    broker-flat gate must look at every symbol the shared Demo account
    might hold — not just the signal's own symbol, which cannot see a
    position/order another consumer (a different symbol, a different
    process) left on the account. Returns ``(nonflat_positions,
    open_order_count)``.
    """
    positions = await execution.get_all_positions()
    open_orders = await execution.get_all_open_orders()
    nonflat = [p for p in positions if not p.is_flat]
    return nonflat, len(open_orders.orders)


async def _poll_order_filled(
    execution: BinanceFuturesDemoExecutionClient,
    *,
    symbol: str,
    client_order_id: str,
    expected_side: str,
    expected_qty: Decimal,
    expected_reduce_only: bool,
) -> bool:
    """Bounded ``GET /fapi/v1/order`` poll. ``True`` iff FILLED observed.

    A submit-response ``NEW`` is not a final state (ROB-305 §4). Never an
    unbounded retry loop; a transient query error keeps polling within the
    bound rather than aborting on the first error. A FILLED response whose
    echoed symbol/side/client_order_id/qty/reduceOnly disagree with what we
    requested raises :class:`BrokerEchoMismatch` rather than being accepted
    as fill evidence (ROB-993 adversarial review Finding 4).
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
            _assert_order_echo(
                context="get_order_poll",
                expected_symbol=symbol,
                expected_side=expected_side,
                expected_client_order_id=client_order_id,
                expected_qty=expected_qty,
                expected_reduce_only=expected_reduce_only,
                echoed_symbol=status_result.symbol,
                echoed_side=status_result.side,
                echoed_client_order_id=status_result.client_order_id,
                echoed_qty=status_result.orig_qty,
                echoed_reduce_only=status_result.reduce_only,
            )
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
) -> RoundTripResult:
    """Root-reserve, open MARKET, then close reduceOnly MARKET.

    Raises ``RoundTripBlocked`` if the exposure slot could not be claimed
    (a concurrent loop/tick won the race — zero broker calls follow) or if
    the shared Demo account already carries a position/open order on this
    symbol (the pre-submit flat gate — Finding 2), or
    ``BinanceFuturesDemoHedgeModeBlocked`` if the account is not in
    One-way mode. Any broker/ledger disagreement records an ``anomaly``
    row and re-raises — this function never reports a clean result on an
    unproven fill, an own-fill-delta mismatch, a broker echo mismatch, or
    a non-flat post-close position.
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
        global_open_root_cap=_GLOBAL_OPEN_ROOT_CAP,
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

    # 1. Broker-flat pre-submit gate (ROB-993 adversarial review Finding 2;
    #    hardened account-wide + re-checked fresh in R2, verify-993-r2-2329.md
    #    Finding 2). A shared-credential Demo account can already carry a
    #    position/open order — on ANY symbol, from another consumer (the
    #    production demo-scalping bot, an operator smoke run, this loop
    #    trading a different symbol) — that this loop's own local ledger
    #    reservation cannot see. A fresh account-wide signed snapshot right
    #    after reservation — before ANY other broker call — refuses to
    #    proceed rather than risk opening while the account is not flat.
    try:
        nonflat_positions, open_order_count = await _fetch_account_flat_snapshot(
            execution
        )
    except Exception:
        await _release("broker_pre_submit_snapshot_failed")
        raise
    if nonflat_positions or open_order_count:
        await _release("broker_not_flat_pre_submit")
        raise RoundTripBlocked(
            "broker_not_flat_pre_submit: "
            f"nonflat_positions={[(p.symbol, str(p.position_amt)) for p in nonflat_positions]} "
            f"open_orders={open_order_count}"
        )
    baseline_qty = Decimal("0")

    # 2. Position-mode check — One-way required (inherited ROB-298 PR 2 boundary).
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

    # 3. Leverage pinned to 1x — set_leverage raises on any non-1 request or
    #    echo mismatch (BinanceFuturesDemoLeverageMismatch), before this call
    #    can proceed further.
    await execution.set_leverage(symbol=symbol, leverage=leverage)

    # 4. PREVIEWED — local preview, no HTTP.
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

    # 5. VALIDATED — POST /fapi/v1/order/test (no placement).
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

    # 5b. Fresh account-wide flat re-check, immediately before the mutating
    #     open submit (ROB-993 R2 adversarial review, verify-993-r2-2329.md
    #     Finding 2). Four broker calls (position-mode, set_leverage,
    #     order_test, plus the initial gate itself) separate the first flat
    #     check from the actual submit; re-reading right here shrinks that
    #     window so a position/order that appeared in the gap is still
    #     caught before any broker mutation, not just detected after the
    #     fact via the own-fill-delta check below.
    try:
        (
            fresh_nonflat_positions,
            fresh_open_order_count,
        ) = await _fetch_account_flat_snapshot(execution)
    except Exception:
        await _release("broker_pre_submit_refresh_failed")
        raise
    if fresh_nonflat_positions or fresh_open_order_count:
        await _release("broker_not_flat_pre_submit_refresh")
        raise RoundTripBlocked(
            "broker_not_flat_pre_submit_refresh: "
            f"nonflat_positions={[(p.symbol, str(p.position_amt)) for p in fresh_nonflat_positions]} "
            f"open_orders={fresh_open_order_count}"
        )

    # 6. SUBMITTED — signed POST /fapi/v1/order (real Demo placement; open).
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
    try:
        _assert_order_echo(
            context="open_submit",
            expected_symbol=symbol,
            expected_side=side,
            expected_client_order_id=open_client_order_id,
            expected_qty=qty,
            expected_reduce_only=False,
            echoed_symbol=submit_result.symbol,
            echoed_side=submit_result.side,
            echoed_client_order_id=submit_result.client_order_id,
            echoed_qty=submit_result.qty,
            echoed_reduce_only=submit_result.reduce_only,
        )
    except BrokerEchoMismatch as exc:
        await ledger.record_anomaly(
            client_order_id=open_client_order_id,
            reason=f"open_submit_echo_mismatch: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        raise
    await ledger.record_submitted(
        client_order_id=open_client_order_id,
        broker_order_id=submit_result.broker_order_id,
        now=_now_utc(),
        extra_metadata_merge={"submit_status": submit_result.status},
    )
    await session.commit()

    # 7. Resolve the OPEN fill (ROB-305 §4) — never advance past `submitted`
    #    on a bare submit-response NEW.
    open_fill_proven = submit_result.status == "FILLED"
    if not open_fill_proven and submit_result.status not in _TERMINAL_NONFILL_STATUSES:
        try:
            open_fill_proven = await _poll_order_filled(
                execution,
                symbol=symbol,
                client_order_id=open_client_order_id,
                expected_side=side,
                expected_qty=qty,
                expected_reduce_only=False,
            )
        except BrokerEchoMismatch as exc:
            await ledger.record_anomaly(
                client_order_id=open_client_order_id,
                reason=f"open_fill_poll_echo_mismatch: {exc}",
                now=_now_utc(),
            )
            await session.commit()
            raise
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

    # 8. Own-fill delta attribution (ROB-993 adversarial review Finding 2
    #    continuation). The close quantity must be attributable to OUR OWN
    #    fill — the delta between the post-open snapshot and the verified
    #    (zero) baseline — not the raw account-wide positionAmt. A delta
    #    that doesn't match what we submitted means something else moved
    #    this symbol's position in the narrow window since the pre-submit
    #    gate (a different consumer of the shared Demo account); abort
    #    before any close submit rather than close an unverified quantity.
    expected_delta = qty if side == "BUY" else -qty
    actual_delta = pre_close_pos.position_amt - baseline_qty
    if actual_delta != expected_delta:
        await ledger.record_anomaly(
            client_order_id=open_client_order_id,
            reason=(
                f"own_fill_delta_mismatch: expected_delta={expected_delta} "
                f"actual_delta={actual_delta} baseline={baseline_qty} "
                f"post_open_position={pre_close_pos.position_amt}"
            ),
            now=_now_utc(),
        )
        await session.commit()
        raise RuntimeError(
            f"strategy loop own-fill delta mismatch for cid={open_client_order_id}: "
            f"expected {expected_delta}, observed {actual_delta}"
        )

    close_side = "SELL" if side == "BUY" else "BUY"
    close_qty = quantize_qty(
        abs(actual_delta),
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

    try:
        _assert_order_echo(
            context="close_submit",
            expected_symbol=symbol,
            expected_side=close_side,
            expected_client_order_id=close_client_order_id,
            expected_qty=close_qty,
            expected_reduce_only=True,
            echoed_symbol=close_result.symbol,
            echoed_side=close_result.side,
            echoed_client_order_id=close_result.client_order_id,
            echoed_qty=close_result.qty,
            echoed_reduce_only=close_result.reduce_only,
        )
    except BrokerEchoMismatch as exc:
        await ledger.record_anomaly(
            client_order_id=close_client_order_id,
            reason=f"close_submit_echo_mismatch: {exc}",
            now=_now_utc(),
        )
        await ledger.record_anomaly(
            client_order_id=open_client_order_id,
            reason=f"close_submit_echo_mismatch: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        raise

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
        try:
            close_fill_proven = await _poll_order_filled(
                execution,
                symbol=symbol,
                client_order_id=close_client_order_id,
                expected_side=close_side,
                expected_qty=close_qty,
                expected_reduce_only=True,
            )
        except BrokerEchoMismatch as exc:
            await ledger.record_anomaly(
                client_order_id=close_client_order_id,
                reason=f"close_fill_poll_echo_mismatch: {exc}",
                now=_now_utc(),
            )
            await ledger.record_anomaly(
                client_order_id=open_client_order_id,
                reason=f"close_fill_poll_echo_mismatch: {exc}",
                now=_now_utc(),
            )
            await session.commit()
            raise
    if close_fill_proven:
        await ledger.record_filled(
            client_order_id=close_client_order_id, now=_now_utc()
        )
        await session.commit()

    # NOTE (ROB-993 adversarial review Finding 3): the open root is NOT
    # transitioned to `closed` here. It stays `filled` (blocking — occupies
    # the ROB-844 exposure slot) until `_reconcile` below has verified
    # open_orders empty AND position flat AND close_fill_proven — only then
    # does it collapse `filled -> closed -> reconciled` back to back. Any
    # reconcile failure instead writes `anomaly` directly from `filled`.
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
        strategy_loop_tag=strategy_loop_tag,
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
    strategy_loop_tag: str,
    open_broker_order_id: str | None,
    close_broker_order_id: str | None,
) -> RoundTripResult:
    """Reconciliation gate: open_orders empty AND position flat AND close
    fill proven — every check runs while the open root is still ``filled``
    (blocking; ROB-993 adversarial review Finding 3). ``closed`` is written
    only immediately before ``reconciled``, once all three checks have
    already passed; any failure instead records ``anomaly`` directly from
    ``filled`` so the exposure slot is never released on an unverified
    broker state.
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

    if not close_fill_proven:
        reason = (
            "close_fill_unproven_after_flat_reconcile: position flat and "
            "open orders 0, but close order never observed FILLED"
        )
        await ledger.record_anomaly(
            client_order_id=open_client_order_id, reason=reason, now=_now_utc()
        )
        await ledger.record_anomaly(
            client_order_id=close_client_order_id, reason=reason, now=_now_utc()
        )
        await session.commit()
        raise RuntimeError(
            f"close fill unproven though account flat (cid={close_client_order_id})"
        )

    # All checks passed — collapse filled -> closed -> reconciled together,
    # with no other broker call in between.
    exit_metadata = {
        "exit_reason": "immediate_close",
        "strategy_loop_tag": strategy_loop_tag,
    }
    await ledger.record_closed(
        client_order_id=open_client_order_id,
        now=_now_utc(),
        extra_metadata_merge=exit_metadata,
    )
    await ledger.record_reconciled(client_order_id=open_client_order_id, now=_now_utc())
    await session.commit()

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
