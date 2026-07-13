"""ROB-307 PR2 — one-shot Binance Demo scalping executor.

Consumes one :class:`OrderIntent`, re-checks the risk envelope against
the **live ledger**, and drives a complete small Demo lifecycle to flat /
open-orders-0, writing the ledger lifecycle and reconciling. The first
execution PR opens **and immediately closes** (no unattended position):

* Spot: BUY (MARKET) → SELL the filled base balance → reconcile.
* USD-M Futures: 1x-pinned open (MARKET) → ``reduceOnly`` close → reconcile.

Futures submits may return ``NEW``; fills are proven by a bounded
``GET /fapi/v1/order`` poll, then non-flat positionRisk, before recording
``filled`` (ROB-305 §4). A dirty reconcile (residual open orders, or a
non-flat futures position) records ``anomaly`` instead of a clean success.

This module lives **outside** the import-guarded read-only signal package
because it must use the signed execution clients + credentials. It reuses
the public execution clients, sizing helpers, and ledger service; the
audited smoke scripts remain an independent reference (dedupe follow-up).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.demo_scalping.contract import (
    DEMO_SCALPING_FEE_RATE_BPS,
    MarketConditions,
    Product,
    ReasonCode,
    ScalpingRiskLimits,
    evaluate_risk,
)
from app.services.brokers.binance.demo_scalping.cost import (
    build_round_trip_economics,
    mae_mfe_bps,
    spot_avg_fill_price,
)
from app.services.brokers.binance.demo_scalping.ledger_state import (
    load_ledger_snapshot,
)
from app.services.brokers.binance.demo_scalping.market_data import (
    spread_bps as md_spread_bps,
)
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent
from app.services.brokers.binance.demo_scalping_exec.analytics import (
    ScalpTradeAnalyticsService,
)
from app.services.brokers.binance.demo_scalping_exec.reference import (
    DemoReferenceData,
    SymbolReference,
)
from app.services.brokers.binance.futures_demo.sizing import (
    FuturesSizingBlocked,
    compute_futures_demo_order_qty,
)
from app.services.brokers.binance.spot_demo.sizing import (
    CloseQtyResult,
    SizingBlocked,
    classify_close_residual,
    compute_close_qty,
    compute_demo_order_qty,
)

logger = logging.getLogger(__name__)

_VENUE = "binance"
_VENUE_HOST = {
    "spot": "demo-api.binance.com",
    "usdm_futures": "demo-fapi.binance.com",
}
_TERMINAL_NONFILL = frozenset({"CANCELED", "REJECTED", "EXPIRED"})
_FILL_POLL_MAX = 5
_FILL_POLL_DELAY_SECONDS = 1.0
_BPS = Decimal("10000")


@dataclass(frozen=True)
class _ExposureSlotTaken:
    """Sentinel: the atomic root reservation lost the race (ROB-844).

    Returned by ``_open_leg`` instead of ``(cid, submit)`` when another process
    already holds the exposure slot. The caller converts it to a ``blocked``
    result with ``EXPOSURE_SLOT_TAKEN`` and performs no broker order submit.
    """

    reason: str | None = None


def _new_cid() -> str:
    return "rob307-" + uuid.uuid4().hex[:24]


def _base_asset(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _align_price(price: Decimal, tick: Decimal) -> Decimal:
    """Floor ``price`` to the exchange ``tick`` (PRICE_FILTER) so a computed
    TP/SL price is never rejected for tick misalignment."""
    if tick <= 0:
        return price
    return (price // tick) * tick


def _exit_metadata(
    exit_reason: str, monitor_error: str | None, residual: str | None = None
) -> dict[str, Any]:
    """Ledger ``extra_metadata`` for the reconcile transition."""
    meta: dict[str, Any] = {"exit_reason": exit_reason}
    if residual is not None:
        meta["residual"] = residual
    if monitor_error is not None:
        meta["monitor_error"] = monitor_error
    return meta


@dataclass(frozen=True)
class ExecutionResult:
    intent: OrderIntent
    # blocked | dry_run | reconciled | anomaly
    status: str
    open_client_order_id: str | None = None
    close_client_order_id: str | None = None
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    anomaly_reason: str | None = None
    sized_qty: Decimal | None = None
    sized_notional_usdt: Decimal | None = None
    final_open_orders: int | None = None
    final_flat: bool | None = None
    # take_profit | stop_loss | timeout | monitor_error | immediate
    exit_reason: str | None = None
    # set when the monitor poll raised; the position is still closed flat
    monitor_error: str | None = None

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "product": self.intent.product,
            "symbol": self.intent.symbol,
            "side": self.intent.side,
            "exit_reason": self.exit_reason,
            "monitor_error": self.monitor_error,
            "open_client_order_id": self.open_client_order_id,
            "close_client_order_id": self.close_client_order_id,
            "reason_codes": list(self.reason_codes),
            "anomaly_reason": self.anomaly_reason,
            "sized_qty": None if self.sized_qty is None else str(self.sized_qty),
            "sized_notional_usdt": (
                None
                if self.sized_notional_usdt is None
                else str(self.sized_notional_usdt)
            ),
            "final_open_orders": self.final_open_orders,
            "final_flat": self.final_flat,
        }


@dataclass(frozen=True)
class _MonitorOutcome:
    """What the bounded monitor observed, for ROB-315 0c telemetry. All
    price-path fields are over the **conservative** price the close would
    actually achieve (bid for a long exit, ask for a short exit)."""

    exit_reason: str
    min_conservative: Decimal | None = None
    max_conservative: Decimal | None = None
    # Conservative price + spread at the poll that decided the exit (or the
    # last poll on timeout). Used for exit slippage reference + spread@fill.
    exit_conservative: Decimal | None = None
    exit_spread_bps: Decimal | None = None


@dataclass(frozen=True)
class _RunTelemetry:
    """ROB-315 0c diagnostics threaded into the analytics row. All optional —
    ``execute()`` (immediate) leaves the monitor-derived fields None."""

    entry_spread_bps: Decimal | None = None
    exit_spread_bps: Decimal | None = None
    mae_bps: Decimal | None = None
    mfe_bps: Decimal | None = None
    holding_seconds: int | None = None
    exit_reference_price: Decimal | None = None


class DemoScalpingExecutor:
    def __init__(
        self,
        *,
        product: Product,
        client: Any,
        session: Any,
        reference: DemoReferenceData | Any,
        now: dt.datetime,
        limits: ScalpingRiskLimits | None = None,
        market_data: Any | None = None,
        poll_max: int = _FILL_POLL_MAX,
        poll_delay_seconds: float = _FILL_POLL_DELAY_SECONDS,
    ) -> None:
        self.product = product
        self.client = client
        self.session = session
        self.reference = reference
        self.now = now
        self.limits = limits or ScalpingRiskLimits()
        self.market_data = market_data  # required for execute_monitored
        self.ledger = BinanceDemoLedgerService(session)
        self.analytics = ScalpTradeAnalyticsService(session)
        self.poll_max = poll_max
        self.poll_delay_seconds = poll_delay_seconds
        # Avg fill prices captured during a single run (ROB-313 cost capture).
        self._open_fill_price: Decimal | None = None
        self._close_fill_price: Decimal | None = None
        # Spread at the preflight market snapshot ≈ spread at the open fill
        # (ROB-315 0c spread@fill entry leg).
        self._entry_spread_bps: Decimal | None = None

    def _extract_fill_price(self, submit: Any) -> Decimal | None:
        """Avg fill price from a submit/order result. Futures carries
        ``avgPrice``; spot is derived from cumQuote/executedQty. ``None``
        when not derivable (unfilled / missing fields)."""
        if self.product == "usdm_futures":
            ap = getattr(submit, "avg_price", None)
            return ap if ap is not None and ap > 0 else None
        cq = getattr(submit, "cummulative_quote_qty", None)
        eq = getattr(submit, "executed_qty", None)
        if cq is None or eq is None:
            return None
        return spot_avg_fill_price(cummulative_quote_qty=cq, executed_qty=eq)

    def _round_trip_realized_pnl_usdt(
        self, intent: OrderIntent, ref: Any, qty: Decimal
    ) -> Decimal | None:
        """Round-trip net PnL (USDT, signed; a loss is negative) for the durable
        daily-loss-budget gate (``ledger_state._realized_loss_today``). ``None``
        when either leg lacks a proven fill price — never fabricated. Independent
        of the exit *reference* price (that only moves exit-slippage telemetry),
        so this equals the analytics row's ``net_pnl_usdt``."""
        entry_fill = self._open_fill_price
        if entry_fill is None or self._close_fill_price is None:
            return None
        econ = build_round_trip_economics(
            side=intent.side,
            qty=qty,
            entry_reference_price=intent.entry_reference_price or ref.price,
            entry_fill_price=entry_fill,
            fee_rate_bps=DEMO_SCALPING_FEE_RATE_BPS,
            exit_fill_price=self._close_fill_price,
        )
        return econ.net_pnl_usdt

    async def _finalize_analytics(
        self,
        intent: OrderIntent,
        ref: SymbolReference,
        qty: Decimal,
        notional: Decimal,
        instrument_id: int,
        result: ExecutionResult,
        telemetry: _RunTelemetry | None = None,
        session_tag: str | None = None,
        signal_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Best-effort: write one ``scalp_trade_analytics`` round-trip row.

        Runs in a SAVEPOINT so an analytics failure can never poison the
        trade-lifecycle transaction. Only called once the run reached
        close/reconcile; open-stage failures return earlier and record no
        row (no real round-trip). Exit-derived fields stay NULL when the
        close leg did not fill (ROB-313 — never a fabricated success).

        ROB-315 0c: MAE/MFE, spread@fill, exit-slippage reference and holding
        time arrive via ``telemetry`` (monitor-derived); they are independent
        of the entry fill price and are recorded even on a partial row."""
        if result.open_client_order_id is None:
            return
        tele = telemetry or _RunTelemetry()
        # ROB-315 0b: the entry price must come from fill-proven evidence
        # (the submit/get_order avg fill captured during the run). It is NEVER
        # the reference price — a missing fill price means we cannot fabricate
        # economics, so we record a partial row instead of a fake success.
        entry_fill = self._open_fill_price
        entry_ref = intent.entry_reference_price or ref.price
        try:
            if entry_fill is None:
                await self._record_partial_analytics(
                    intent,
                    qty,
                    instrument_id,
                    result,
                    tele,
                    session_tag=session_tag,
                    signal_snapshot=signal_snapshot,
                )
                return
            econ = build_round_trip_economics(
                side=intent.side,
                qty=qty,
                entry_reference_price=entry_ref,
                entry_fill_price=entry_fill,
                fee_rate_bps=DEMO_SCALPING_FEE_RATE_BPS,
                exit_fill_price=self._close_fill_price,
                exit_reference_price=tele.exit_reference_price,
            )
            async with self.session.begin_nested():
                await self.analytics.record(
                    open_client_order_id=result.open_client_order_id,
                    close_client_order_id=result.close_client_order_id,
                    instrument_id=instrument_id,
                    product=intent.product,
                    symbol=intent.symbol,
                    side=intent.side,
                    qty=qty,
                    entry_price=entry_fill,
                    exit_price=self._close_fill_price,
                    entry_notional_usdt=econ.entry_notional_usdt,
                    fee_rate_bps=DEMO_SCALPING_FEE_RATE_BPS,
                    entry_fee_usdt=econ.entry_fee_usdt,
                    exit_fee_usdt=econ.exit_fee_usdt,
                    entry_slippage_bps=econ.entry_slippage_bps,
                    exit_slippage_bps=econ.exit_slippage_bps,
                    entry_spread_bps=tele.entry_spread_bps,
                    exit_spread_bps=tele.exit_spread_bps,
                    mae_bps=tele.mae_bps,
                    mfe_bps=tele.mfe_bps,
                    gross_pnl_usdt=econ.gross_pnl_usdt,
                    net_pnl_usdt=econ.net_pnl_usdt,
                    net_return_bps=econ.net_return_bps,
                    holding_seconds=tele.holding_seconds,
                    exit_reason=result.exit_reason,
                    session_tag=session_tag,
                    signal_snapshot=signal_snapshot,
                    now=self.now,
                )
        except Exception:  # noqa: BLE001 — analytics is best-effort, never fatal
            logger.exception(
                "scalp analytics write failed for %s %s",
                intent.product,
                intent.symbol,
            )

    async def _record_partial_analytics(
        self,
        intent: OrderIntent,
        qty: Decimal,
        instrument_id: int,
        result: ExecutionResult,
        tele: _RunTelemetry,
        session_tag: str | None = None,
        signal_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Record a round-trip with no derivable entry fill price: the fill is
        proven (the run reached reconcile) but no avg price evidence exists, so
        entry price / economics stay NULL — never fabricated (ROB-315 0b). The
        raw close fill price (if any) and monitor-derived telemetry are still
        stored as informational."""
        assert result.open_client_order_id is not None
        async with self.session.begin_nested():
            await self.analytics.record(
                open_client_order_id=result.open_client_order_id,
                close_client_order_id=result.close_client_order_id,
                instrument_id=instrument_id,
                product=intent.product,
                symbol=intent.symbol,
                side=intent.side,
                qty=qty,
                entry_price=None,
                exit_price=self._close_fill_price,
                entry_notional_usdt=None,
                fee_rate_bps=DEMO_SCALPING_FEE_RATE_BPS,
                entry_spread_bps=tele.entry_spread_bps,
                exit_spread_bps=tele.exit_spread_bps,
                mae_bps=tele.mae_bps,
                mfe_bps=tele.mfe_bps,
                holding_seconds=tele.holding_seconds,
                exit_reason=result.exit_reason,
                session_tag=session_tag,
                signal_snapshot=signal_snapshot,
                now=self.now,
            )

    async def execute(
        self,
        intent: OrderIntent,
        *,
        confirm: bool = False,
        market: MarketConditions | None = None,
        session_tag: str | None = None,
        signal_snapshot: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """One-shot: open + immediate close-flat (no hold)."""
        prep = await self._preflight(intent, confirm, market)
        if isinstance(prep, ExecutionResult):
            return prep
        ref, qty, notional, instrument_id = prep
        open_cid, error = await self._open_position(
            intent, ref, qty, notional, instrument_id
        )
        if error is not None:
            return error
        result = await self._close_and_reconcile(
            intent,
            ref,
            qty,
            notional,
            open_cid,
            instrument_id,
            exit_reason="immediate",
        )
        # No monitor path on an immediate run: only spread@fill (entry) is known.
        telemetry = _RunTelemetry(entry_spread_bps=self._entry_spread_bps)
        await self._finalize_analytics(
            intent,
            ref,
            qty,
            notional,
            instrument_id,
            result,
            telemetry,
            session_tag=session_tag,
            signal_snapshot=signal_snapshot,
        )
        return result

    async def execute_monitored(
        self,
        intent: OrderIntent,
        *,
        confirm: bool = False,
        market: MarketConditions | None = None,
        tp_bps: Decimal = Decimal("30"),
        sl_bps: Decimal = Decimal("20"),
        max_poll_count: int = 30,
        poll_interval_s: float | None = None,
        max_runtime_s: float = 300.0,
        session_tag: str | None = None,
        signal_snapshot: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """Open, then poll the bookTicker within a bounded window and
        MARKET-close on a TP/SL cross — failsafe-close at window end. Always
        ends flat in-run (no unattended position; no broker-side bracket)."""
        prep = await self._preflight(intent, confirm, market)
        if isinstance(prep, ExecutionResult):
            return prep
        ref, qty, notional, instrument_id = prep
        if self.market_data is None:
            raise ValueError("execute_monitored requires a market_data source")

        long = intent.side == "BUY"
        if long:
            tp = _align_price(ref.price * (Decimal("1") + tp_bps / _BPS), ref.tick_size)
            sl = _align_price(ref.price * (Decimal("1") - sl_bps / _BPS), ref.tick_size)
        else:
            tp = _align_price(ref.price * (Decimal("1") - tp_bps / _BPS), ref.tick_size)
            sl = _align_price(ref.price * (Decimal("1") + sl_bps / _BPS), ref.tick_size)

        open_cid, error = await self._open_position(
            intent, ref, qty, notional, instrument_id
        )
        if error is not None:
            return error

        # Once a position is open it MUST be closed flat in-run. If the
        # monitor poll raises (timeout / rate-limit / network), fall through
        # to close+reconcile anyway with exit_reason=monitor_error.
        monitor_error: str | None = None
        outcome: _MonitorOutcome
        hold_start = time.monotonic()
        try:
            outcome = await self._monitor_until_exit(
                intent,
                tp=tp,
                sl=sl,
                long=long,
                max_poll_count=max_poll_count,
                poll_interval=(
                    poll_interval_s
                    if poll_interval_s is not None
                    else self.poll_delay_seconds
                ),
                max_runtime_s=max_runtime_s,
            )
        except Exception as exc:  # noqa: BLE001 — never leave the position open
            outcome = _MonitorOutcome("monitor_error")
            monitor_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "monitor poll failed for %s %s — forcing close",
                intent.product,
                intent.symbol,
            )
        holding_seconds = int(time.monotonic() - hold_start)
        result = await self._close_and_reconcile(
            intent,
            ref,
            qty,
            notional,
            open_cid,
            instrument_id,
            exit_reason=outcome.exit_reason,
            monitor_error=monitor_error,
        )
        # ROB-315 0c telemetry — entry spread from the preflight snapshot, the
        # rest from the monitor's own polls (MAE/MFE vs the entry reference).
        entry_ref = intent.entry_reference_price or ref.price
        mae, mfe = mae_mfe_bps(
            side=intent.side,
            entry_price=entry_ref,
            path_min=outcome.min_conservative,
            path_max=outcome.max_conservative,
        )
        telemetry = _RunTelemetry(
            entry_spread_bps=self._entry_spread_bps,
            exit_spread_bps=outcome.exit_spread_bps,
            mae_bps=mae,
            mfe_bps=mfe,
            holding_seconds=holding_seconds,
            exit_reference_price=outcome.exit_conservative,
        )
        await self._finalize_analytics(
            intent,
            ref,
            qty,
            notional,
            instrument_id,
            result,
            telemetry,
            session_tag=session_tag,
            signal_snapshot=signal_snapshot,
        )
        return result

    async def _monitor_until_exit(
        self, intent, *, tp, sl, long, max_poll_count, poll_interval, max_runtime_s
    ) -> _MonitorOutcome:
        """Bounded poll loop → ``_MonitorOutcome`` (exit reason + price-path
        telemetry; ROB-315 0c). No new polls — the min/max conservative price,
        the exit-poll conservative price and its spread are all read off the
        same bookTicker polls the TP/SL trigger already consumes.

        Conservative trigger price: a long exits via a SELL, so judge it on
        the **bid** (the price it would actually sell at); a short exits via
        a BUY, so judge it on the **ask**. This avoids triggering on a mid
        the close could not actually achieve.
        """
        deadline = time.monotonic() + max_runtime_s
        path_min: Decimal | None = None
        path_max: Decimal | None = None
        last_price: Decimal | None = None
        last_spread: Decimal | None = None
        for _ in range(max_poll_count):
            book = await self.market_data.fetch_book_ticker(
                intent.product, intent.symbol
            )
            price = book.bid if long else book.ask
            path_min = price if path_min is None else min(path_min, price)
            path_max = price if path_max is None else max(path_max, price)
            last_price = price
            last_spread = md_spread_bps(book)
            if long:
                if price >= tp:
                    return _MonitorOutcome(
                        "take_profit", path_min, path_max, price, last_spread
                    )
                if price <= sl:
                    return _MonitorOutcome(
                        "stop_loss", path_min, path_max, price, last_spread
                    )
            else:
                if price <= tp:
                    return _MonitorOutcome(
                        "take_profit", path_min, path_max, price, last_spread
                    )
                if price >= sl:
                    return _MonitorOutcome(
                        "stop_loss", path_min, path_max, price, last_spread
                    )
            if time.monotonic() >= deadline:
                break
            if poll_interval > 0:
                await asyncio.sleep(poll_interval)
        return _MonitorOutcome("timeout", path_min, path_max, last_price, last_spread)

    async def _preflight(
        self, intent: OrderIntent, confirm: bool, market: MarketConditions | None = None
    ) -> ExecutionResult | tuple[SymbolReference, Decimal, Decimal, int]:
        """Risk re-check + reference + sizing + dry-run gate. Returns an
        ExecutionResult (blocked/dry_run) to short-circuit, else the prepared
        ``(ref, qty, notional, instrument_id)``.

        ROB-315 0c / D4: the caller supplies the real ``market`` snapshot
        (spread + data age) so the ``SPREAD_TOO_WIDE`` / ``STALE_DATA`` gates
        actually fire.

        ROB-841: a missing snapshot now fails **closed** with
        ``market_conditions_unavailable`` instead of synthesizing a 0/0
        snapshot (which silently disarmed those gates). The fail-close returns
        BEFORE the ledger read, so an unavailable snapshot touches neither
        broker nor ledger."""
        # The spread@fill entry leg is the preflight spread (captured just
        # before the open). Reset per run so a blocked/early return is clean.
        if market is None:
            self._entry_spread_bps = None
            return ExecutionResult(
                intent=intent,
                status="blocked",
                reason_codes=(ReasonCode.MARKET_CONDITIONS_UNAVAILABLE,),
            )
        self._entry_spread_bps = market.spread_bps
        # Use one short independent read session and close it before identity
        # creation/reservation acquire their own transactions. The owner session
        # must remain connection-free here: otherwise pool_size concurrent runs
        # can each hold connection #1 while all wait indefinitely for #2.
        factory = self.ledger.independent_session_factory()
        async with factory() as snapshot_session:
            snapshot_ledger = BinanceDemoLedgerService(
                snapshot_session, reservation_session_factory=factory
            )
            snapshot = await load_ledger_snapshot(
                snapshot_ledger,
                product=intent.product,
                symbol=intent.symbol,
                now=self.now,
            )
        risk = evaluate_risk(
            product=intent.product,
            symbol=intent.symbol,
            side=intent.side,
            target_notional_usdt=intent.target_notional_usdt,
            limits=self.limits,
            ledger=snapshot,
            market=market,
        )
        if not risk.allowed:
            return ExecutionResult(
                intent=intent, status="blocked", reason_codes=risk.reason_codes
            )
        ref = await self.reference.fetch(intent.product, intent.symbol)
        sized = self._size(intent, ref)
        if isinstance(sized, str):
            return ExecutionResult(
                intent=intent, status="blocked", reason_codes=(sized,)
            )
        qty, notional = sized
        if not confirm:
            return ExecutionResult(
                intent=intent,
                status="dry_run",
                sized_qty=qty,
                sized_notional_usdt=notional,
            )
        instrument_id = await self._resolve_or_create_instrument(intent.symbol)
        return (ref, qty, notional, instrument_id)

    async def _open_position(
        self, intent, ref, qty, notional, instrument_id
    ) -> tuple[str | None, ExecutionResult | None]:
        """Product-specific open + fill. Returns ``(open_cid, None)`` on a
        proven fill, else ``(None, error_result)``."""
        if intent.product == "usdm_futures":
            mode = await self.client.get_position_mode()
            if mode.is_hedge_mode:
                return None, ExecutionResult(
                    intent=intent,
                    status="blocked",
                    reason_codes=("futures_hedge_mode_blocked",),
                )
            leverage = await self.client.set_leverage(symbol=intent.symbol, leverage=1)
            if leverage.leverage != 1:
                return None, ExecutionResult(
                    intent=intent,
                    status="blocked",
                    reason_codes=("futures_leverage_mismatch",),
                )
            opened = await self._open_leg(intent, instrument_id, qty, notional)
            if isinstance(opened, _ExposureSlotTaken):
                return None, self._exposure_slot_taken_result(intent, opened)
            open_cid, submit = opened
            proven, polled_price = await self._fill_proven(
                intent.symbol, open_cid, submit.status
            )
            if proven:
                await self.ledger.record_filled(client_order_id=open_cid, now=self.now)
                # ROB-315 0b: capture the entry fill price from whichever
                # evidence proved the fill — the polled get_order avg price when
                # the submit was NEW, else the FILLED submit's own avg price.
                self._open_fill_price = (
                    polled_price
                    if polled_price is not None
                    else self._extract_fill_price(submit)
                )
            else:
                position = await self.client.get_position(symbol=intent.symbol)
                if not position.is_flat:
                    await self.ledger.record_filled(
                        client_order_id=open_cid,
                        now=self.now,
                        extra_metadata_merge={"fill_evidence": "position_risk_nonflat"},
                    )
                    # Proven by positionRisk only — no usable fill price.
                    # Leave it None; _finalize records a partial row rather
                    # than fabricating a price from the reference.
                    self._open_fill_price = None
                else:
                    reason = "futures_open_fill_unproven"
                    await self.ledger.record_anomaly(
                        client_order_id=open_cid, reason=reason, now=self.now
                    )
                    return None, ExecutionResult(
                        intent=intent,
                        status="anomaly",
                        open_client_order_id=open_cid,
                        anomaly_reason=reason,
                        sized_qty=qty,
                        sized_notional_usdt=notional,
                        final_flat=True,
                    )
            return open_cid, None

        opened = await self._open_leg(intent, instrument_id, qty, notional)
        if isinstance(opened, _ExposureSlotTaken):
            return None, self._exposure_slot_taken_result(intent, opened)
        open_cid, submit = opened
        if submit.status != "FILLED":
            reason = f"spot_open_not_filled: {submit.status}"
            await self.ledger.record_anomaly(
                client_order_id=open_cid, reason=reason, now=self.now
            )
            return None, ExecutionResult(
                intent=intent,
                status="anomaly",
                open_client_order_id=open_cid,
                anomaly_reason=reason,
                sized_qty=qty,
                sized_notional_usdt=notional,
            )
        await self.ledger.record_filled(client_order_id=open_cid, now=self.now)
        self._open_fill_price = self._extract_fill_price(submit)
        return open_cid, None

    # ------------------------------------------------------------------
    # Sizing + instrument
    # ------------------------------------------------------------------
    def _size(
        self, intent: OrderIntent, ref: SymbolReference
    ) -> tuple[Decimal, Decimal] | str:
        if intent.product == "spot":
            result = compute_demo_order_qty(
                target_notional_usdt=intent.target_notional_usdt,
                price=ref.price,
                min_notional=ref.min_notional,
                step_size=ref.step_size,
                cap_usdt=self.limits.max_notional_usdt,
            )
            if isinstance(result, SizingBlocked):
                return result.reason
            return result.qty, result.notional_usdt
        result = compute_futures_demo_order_qty(
            symbol=intent.symbol,
            target_notional_usdt=intent.target_notional_usdt,
            price=ref.price,
            min_notional=ref.min_notional,
            step_size=ref.step_size,
            cap_usdt=self.limits.max_notional_usdt,
            # Single allowlist source: the configured risk envelope. The
            # futures-excluded set (BTCUSDT) still wins inside sizing.
            symbol_allowlist_override=self.limits.allowlist,
        )
        if isinstance(result, FuturesSizingBlocked):
            return result.reason
        return result.qty, result.notional_usdt

    async def _resolve_or_create_instrument(self, symbol: str) -> int:
        # Identity must be visible to the independent reservation transaction,
        # but committing the executor's shared session would also commit prior
        # lifecycle work. The ledger service owns a dedicated idempotent tx for
        # this identity boundary.
        return await self.ledger.resolve_or_create_instrument(
            venue=_VENUE,
            product=self.product,
            venue_symbol=symbol,
            base_asset=_base_asset(symbol),
            quote_asset="USDT",
        )

    # ------------------------------------------------------------------
    # Fill resolution (no ledger writes; caller records)
    # ------------------------------------------------------------------
    async def _fill_proven(
        self, symbol: str, cid: str, submit_status: str
    ) -> tuple[bool, Decimal | None]:
        """Prove a fill, returning ``(proven, polled_fill_price)``.

        ``polled_fill_price`` is the avg fill price from the ``get_order`` poll
        that proved a NEW submit (ROB-315 0b — captured here so the caller does
        not have to re-derive it). It is ``None`` when the submit was already
        FILLED (the caller uses the submit's own price) or the fill was not
        proven via order status.
        """
        if submit_status == "FILLED":
            return True, None
        if submit_status in _TERMINAL_NONFILL:
            return False, None
        for attempt in range(self.poll_max):
            if attempt > 0:
                await asyncio.sleep(self.poll_delay_seconds)
            try:
                order = await self.client.get_order(symbol=symbol, client_order_id=cid)
            except Exception:  # noqa: BLE001 — transient poll error, retry
                continue
            if order.status == "FILLED":
                return True, self._extract_fill_price(order)
            if order.status in _TERMINAL_NONFILL:
                return False, None
        return False, None  # fail-closed: fill not proven

    def _exposure_slot_taken_result(
        self, intent, slot_taken: _ExposureSlotTaken
    ) -> ExecutionResult:
        """Lost root reservation result — no broker order submit."""
        return ExecutionResult(
            intent=intent,
            status="blocked",
            reason_codes=(ReasonCode.EXPOSURE_SLOT_TAKEN,),
        )

    async def _open_leg(
        self,
        intent,
        instrument_id,
        qty,
        notional,
        *,
        reduce_only=False,
        role="open",
        tp_price: Decimal | None = None,
        sl_price: Decimal | None = None,
    ) -> tuple[str, Any] | _ExposureSlotTaken:
        cid = _new_cid()
        meta = {
            "source": "rob-307-pr2-executor",
            "role": role,
            "reason_codes": list(intent.reason_codes),
        }
        credential_fingerprint = getattr(self.client, "credential_fingerprint", None)
        if isinstance(credential_fingerprint, str) and credential_fingerprint:
            # Exact credential binding for later broker-truth reconciliation.
            # The raw API key/secret never enters ledger metadata.
            meta["credential_fingerprint"] = credential_fingerprint
        if intent.product == "usdm_futures":
            meta["leverage"] = 1
        # ROB-844: the open leg is the ROOT lifecycle. Atomically reserve its
        # exposure slot (advisory-locked recount + planned-root insert in one
        # transaction) BEFORE broker order submit. A loser of the cross-process race
        # returns here with ZERO broker submit — only the reservation winner
        # proceeds. Close/reduce-only child legs keep using record_planned and
        # never consume a slot.
        reservation = await self.ledger.reserve_root_planned(
            instrument_id=instrument_id,
            product=intent.product,
            venue_host=_VENUE_HOST[intent.product],
            client_order_id=cid,
            side=intent.side,
            order_type="MARKET",
            qty=qty,
            price=None,
            tp_price=tp_price if tp_price is not None else intent.tp_price,
            sl_price=sl_price if sl_price is not None else intent.sl_price,
            notional_usdt=notional,
            extra_metadata=meta,
            global_open_root_cap=self.limits.global_open_lifecycle_cap,
            now=self.now,
        )
        if reservation.status != "reserved":
            return _ExposureSlotTaken(reason=reservation.reason)
        await self.ledger.record_previewed(client_order_id=cid, now=self.now)
        await self.ledger.record_validated(client_order_id=cid, now=self.now)
        submit_kwargs: dict[str, Any] = {
            "symbol": intent.symbol,
            "side": intent.side,
            "order_type": "MARKET",
            "qty": qty,
            "client_order_id": cid,
            "confirm": True,
        }
        if intent.product == "usdm_futures":
            submit_kwargs["reduce_only"] = reduce_only
        submit = await self.client.submit_order(**submit_kwargs)
        await self.ledger.record_submitted(
            client_order_id=cid,
            broker_order_id=submit.broker_order_id,
            now=self.now,
            extra_metadata_merge={"submit_status": submit.status},
        )
        return cid, submit

    # ------------------------------------------------------------------
    # Close + reconcile (shared by execute() and execute_monitored()).
    # ------------------------------------------------------------------
    async def _close_and_reconcile(
        self,
        intent,
        ref,
        qty,
        notional,
        open_cid,
        instrument_id,
        *,
        exit_reason,
        monitor_error: str | None = None,
    ) -> ExecutionResult:
        if intent.product == "usdm_futures":
            return await self._close_and_reconcile_futures(
                intent,
                ref,
                qty,
                notional,
                open_cid,
                instrument_id,
                exit_reason,
                monitor_error,
            )
        return await self._close_and_reconcile_spot(
            intent,
            ref,
            qty,
            notional,
            open_cid,
            instrument_id,
            exit_reason,
            monitor_error,
        )

    async def _close_and_reconcile_spot(
        self,
        intent,
        ref,
        qty,
        notional,
        open_cid,
        instrument_id,
        exit_reason,
        monitor_error=None,
    ) -> ExecutionResult:
        # Close: SELL the free base balance (never reuse the BUY qty).
        base = _base_asset(intent.symbol)
        balance = await self.client.get_asset_balance(asset=base)
        close = compute_close_qty(
            free_balance=balance.free,
            price=ref.price,
            min_notional=ref.min_notional,
            step_size=ref.step_size,
        )
        close_cid: str | None = None
        close_filled = False
        if isinstance(close, CloseQtyResult):
            close_cid = _new_cid()
            await self.ledger.record_planned(
                instrument_id=instrument_id,
                product="spot",
                venue_host=_VENUE_HOST["spot"],
                client_order_id=close_cid,
                side="SELL",
                order_type="MARKET",
                qty=close.qty,
                price=None,
                parent_client_order_id=open_cid,
                notional_usdt=close.notional_usdt,
                extra_metadata={"source": "rob-307-pr2-executor", "role": "close"},
                now=self.now,
            )
            await self.ledger.record_previewed(client_order_id=close_cid, now=self.now)
            await self.ledger.record_validated(client_order_id=close_cid, now=self.now)
            csubmit = await self.client.submit_order(
                symbol=intent.symbol,
                side="SELL",
                order_type="MARKET",
                qty=close.qty,
                client_order_id=close_cid,
                confirm=True,
            )
            await self.ledger.record_submitted(
                client_order_id=close_cid,
                broker_order_id=csubmit.broker_order_id,
                now=self.now,
                extra_metadata_merge={"submit_status": csubmit.status},
            )
            if csubmit.status == "FILLED":
                await self.ledger.record_filled(client_order_id=close_cid, now=self.now)
                close_filled = True
            self._close_fill_price = self._extract_fill_price(csubmit)

        await self.ledger.record_closed(client_order_id=open_cid, now=self.now)

        # Reconcile: open orders empty AND only benign dust remaining.
        open_orders = await self.client.get_open_orders(symbol=intent.symbol)
        balance_after = await self.client.get_asset_balance(asset=base)
        residual = classify_close_residual(
            free_after=balance_after.free,
            price=ref.price,
            min_notional=ref.min_notional,
            step_size=ref.step_size,
            open_orders_empty=len(open_orders.orders) == 0,
        )
        if residual.kind == "dust":
            await self.ledger.record_reconciled(
                client_order_id=open_cid,
                now=self.now,
                extra_metadata_merge=_exit_metadata(exit_reason, monitor_error, "dust"),
            )
            if close_cid is not None and close_filled:
                realized_pnl = self._round_trip_realized_pnl_usdt(intent, ref, qty)
                await self.ledger.record_closed(client_order_id=close_cid, now=self.now)
                await self.ledger.record_reconciled(
                    client_order_id=close_cid,
                    now=self.now,
                    extra_metadata_merge=(
                        {"realized_pnl_usdt": str(realized_pnl)}
                        if realized_pnl is not None
                        else None
                    ),
                )
            return ExecutionResult(
                intent=intent,
                status="reconciled",
                open_client_order_id=open_cid,
                close_client_order_id=close_cid,
                sized_qty=qty,
                sized_notional_usdt=notional,
                final_open_orders=len(open_orders.orders),
                exit_reason=exit_reason,
                monitor_error=monitor_error,
            )
        reason = f"spot_reconcile_dirty: {residual.remediation_hint}"
        await self.ledger.record_anomaly(
            client_order_id=open_cid, reason=reason, now=self.now
        )
        return ExecutionResult(
            intent=intent,
            status="anomaly",
            open_client_order_id=open_cid,
            close_client_order_id=close_cid,
            anomaly_reason=reason,
            sized_qty=qty,
            sized_notional_usdt=notional,
            final_open_orders=len(open_orders.orders),
            exit_reason=exit_reason,
            monitor_error=monitor_error,
        )

    async def _close_and_reconcile_futures(
        self,
        intent,
        ref,
        qty,
        notional,
        open_cid,
        instrument_id,
        exit_reason,
        monitor_error=None,
    ) -> ExecutionResult:
        # Close with reduceOnly opposite side of the live position.
        position = await self.client.get_position(symbol=intent.symbol)
        close_cid: str | None = None
        close_filled = False
        if not position.is_flat:
            close_cid = _new_cid()
            close_side = "SELL" if position.position_amt > 0 else "BUY"
            close_qty = abs(position.position_amt)
            await self.ledger.record_planned(
                instrument_id=instrument_id,
                product="usdm_futures",
                venue_host=_VENUE_HOST["usdm_futures"],
                client_order_id=close_cid,
                side=close_side,
                order_type="MARKET",
                qty=close_qty,
                price=None,
                parent_client_order_id=open_cid,
                extra_metadata={
                    "source": "rob-307-pr2-executor",
                    "role": "close",
                    "reduce_only": True,
                },
                now=self.now,
            )
            await self.ledger.record_previewed(client_order_id=close_cid, now=self.now)
            await self.ledger.record_validated(client_order_id=close_cid, now=self.now)
            csubmit = await self.client.submit_order(
                symbol=intent.symbol,
                side=close_side,
                order_type="MARKET",
                qty=close_qty,
                client_order_id=close_cid,
                reduce_only=True,
                confirm=True,
            )
            await self.ledger.record_submitted(
                client_order_id=close_cid,
                broker_order_id=csubmit.broker_order_id,
                now=self.now,
                extra_metadata_merge={"submit_status": csubmit.status},
            )
            self._close_fill_price = self._extract_fill_price(csubmit)
            cproven, cpolled_price = await self._fill_proven(
                intent.symbol, close_cid, csubmit.status
            )
            if cproven:
                await self.ledger.record_filled(client_order_id=close_cid, now=self.now)
                close_filled = True
                # ROB-315 0b: prefer the polled get_order avg price when the
                # close submit was NEW; never leave it at a reference fallback.
                if cpolled_price is not None:
                    self._close_fill_price = cpolled_price
            else:
                post = await self.client.get_position(symbol=intent.symbol)
                if post.is_flat:
                    await self.ledger.record_filled(
                        client_order_id=close_cid,
                        now=self.now,
                        extra_metadata_merge={"fill_evidence": "position_flat"},
                    )
                    close_filled = True

        await self.ledger.record_closed(client_order_id=open_cid, now=self.now)

        # Reconcile: open orders empty AND position flat.
        open_orders = await self.client.get_open_orders(symbol=intent.symbol)
        final_position = await self.client.get_position(symbol=intent.symbol)
        clean = len(open_orders.orders) == 0 and final_position.is_flat
        if clean:
            await self.ledger.record_reconciled(
                client_order_id=open_cid,
                now=self.now,
                extra_metadata_merge=_exit_metadata(exit_reason, monitor_error),
            )
            if close_cid is not None and close_filled:
                realized_pnl = self._round_trip_realized_pnl_usdt(intent, ref, qty)
                await self.ledger.record_closed(client_order_id=close_cid, now=self.now)
                await self.ledger.record_reconciled(
                    client_order_id=close_cid,
                    now=self.now,
                    extra_metadata_merge=(
                        {"realized_pnl_usdt": str(realized_pnl)}
                        if realized_pnl is not None
                        else None
                    ),
                )
            return ExecutionResult(
                intent=intent,
                status="reconciled",
                open_client_order_id=open_cid,
                close_client_order_id=close_cid,
                sized_qty=qty,
                sized_notional_usdt=notional,
                final_open_orders=0,
                final_flat=True,
                exit_reason=exit_reason,
                monitor_error=monitor_error,
            )
        reason = (
            f"futures_reconcile_dirty: open_orders={len(open_orders.orders)} "
            f"flat={final_position.is_flat}"
        )
        await self.ledger.record_anomaly(
            client_order_id=open_cid, reason=reason, now=self.now
        )
        return ExecutionResult(
            intent=intent,
            status="anomaly",
            open_client_order_id=open_cid,
            close_client_order_id=close_cid,
            anomaly_reason=reason,
            sized_qty=qty,
            sized_notional_usdt=notional,
            final_open_orders=len(open_orders.orders),
            final_flat=final_position.is_flat,
            exit_reason=exit_reason,
            monitor_error=monitor_error,
        )
