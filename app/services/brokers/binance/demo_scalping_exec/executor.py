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

from sqlalchemy import select

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.demo_scalping.contract import (
    MarketConditions,
    Product,
    ScalpingRiskLimits,
    evaluate_risk,
)
from app.services.brokers.binance.demo_scalping.ledger_state import (
    load_ledger_snapshot,
)
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent
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


def _new_cid() -> str:
    return "rob307-" + uuid.uuid4().hex[:24]


def _base_asset(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _align_price(price: Decimal, tick: Decimal) -> Decimal:
    """Floor ``price`` to the exchange ``tick`` (PRICE_FILTER) so a bracket
    stop/limit price is never rejected for tick misalignment."""
    if tick <= 0:
        return price
    return (price // tick) * tick


@dataclass(frozen=True)
class ExecutionResult:
    intent: OrderIntent
    status: str  # blocked | dry_run | reconciled | anomaly
    open_client_order_id: str | None = None
    close_client_order_id: str | None = None
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    anomaly_reason: str | None = None
    sized_qty: Decimal | None = None
    sized_notional_usdt: Decimal | None = None
    final_open_orders: int | None = None
    final_flat: bool | None = None
    bracket_client_order_ids: tuple[str, ...] = field(default_factory=tuple)
    exit_reason: str | None = None  # take_profit | stop_loss | timeout | immediate

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "product": self.intent.product,
            "symbol": self.intent.symbol,
            "side": self.intent.side,
            "exit_reason": self.exit_reason,
            "open_client_order_id": self.open_client_order_id,
            "close_client_order_id": self.close_client_order_id,
            "bracket_client_order_ids": list(self.bracket_client_order_ids),
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
        self.poll_max = poll_max
        self.poll_delay_seconds = poll_delay_seconds

    async def execute(
        self, intent: OrderIntent, *, confirm: bool = False
    ) -> ExecutionResult:
        """One-shot: open + immediate close-flat (no hold)."""
        prep = await self._preflight(intent, confirm)
        if isinstance(prep, ExecutionResult):
            return prep
        ref, qty, notional, instrument_id = prep
        open_cid, error = await self._open_position(
            intent, ref, qty, notional, instrument_id
        )
        if error is not None:
            return error
        return await self._close_and_reconcile(
            intent,
            ref,
            qty,
            notional,
            open_cid,
            instrument_id,
            exit_reason="immediate",
        )

    async def execute_monitored(
        self,
        intent: OrderIntent,
        *,
        confirm: bool = False,
        tp_bps: Decimal = Decimal("30"),
        sl_bps: Decimal = Decimal("20"),
        max_poll_count: int = 30,
        poll_interval_s: float | None = None,
        max_runtime_s: float = 300.0,
    ) -> ExecutionResult:
        """Open, then poll the bookTicker within a bounded window and
        MARKET-close on a TP/SL cross — failsafe-close at window end. Always
        ends flat in-run (no unattended position; no broker-side bracket)."""
        prep = await self._preflight(intent, confirm)
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

        exit_reason = await self._monitor_until_exit(
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
        return await self._close_and_reconcile(
            intent,
            ref,
            qty,
            notional,
            open_cid,
            instrument_id,
            exit_reason=exit_reason,
        )

    async def _monitor_until_exit(
        self, intent, *, tp, sl, long, max_poll_count, poll_interval, max_runtime_s
    ) -> str:
        """Bounded poll loop → 'take_profit' | 'stop_loss' | 'timeout'."""
        deadline = time.monotonic() + max_runtime_s
        for _ in range(max_poll_count):
            book = await self.market_data.fetch_book_ticker(
                intent.product, intent.symbol
            )
            mid = (book.bid + book.ask) / Decimal("2")
            if long:
                if mid >= tp:
                    return "take_profit"
                if mid <= sl:
                    return "stop_loss"
            else:
                if mid <= tp:
                    return "take_profit"
                if mid >= sl:
                    return "stop_loss"
            if time.monotonic() >= deadline:
                break
            if poll_interval > 0:
                await asyncio.sleep(poll_interval)
        return "timeout"

    async def _preflight(
        self, intent: OrderIntent, confirm: bool
    ) -> ExecutionResult | tuple[SymbolReference, Decimal, Decimal, int]:
        """Risk re-check + reference + sizing + dry-run gate. Returns an
        ExecutionResult (blocked/dry_run) to short-circuit, else the prepared
        ``(ref, qty, notional, instrument_id)``."""
        snapshot = await load_ledger_snapshot(
            self.ledger, product=intent.product, symbol=intent.symbol, now=self.now
        )
        risk = evaluate_risk(
            product=intent.product,
            symbol=intent.symbol,
            side=intent.side,
            target_notional_usdt=intent.target_notional_usdt,
            limits=self.limits,
            ledger=snapshot,
            market=MarketConditions(
                spread_bps=Decimal("0"),
                data_age_seconds=0.0,
                spot_free_base_qty=Decimal("0"),
            ),
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
            open_cid, submit = await self._open_leg(
                intent, instrument_id, qty, notional
            )
            proven = await self._fill_proven(intent.symbol, open_cid, submit.status)
            if proven:
                await self.ledger.record_filled(client_order_id=open_cid, now=self.now)
            else:
                position = await self.client.get_position(symbol=intent.symbol)
                if not position.is_flat:
                    await self.ledger.record_filled(
                        client_order_id=open_cid,
                        now=self.now,
                        extra_metadata_merge={"fill_evidence": "position_risk_nonflat"},
                    )
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

        open_cid, submit = await self._open_leg(intent, instrument_id, qty, notional)
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
        existing = await self.session.scalar(
            select(CryptoInstrument).where(
                CryptoInstrument.venue == _VENUE,
                CryptoInstrument.product == self.product,
                CryptoInstrument.venue_symbol == symbol,
            )
        )
        if existing is not None:
            return existing.id
        inst = CryptoInstrument(
            venue=_VENUE,
            product=self.product,
            venue_symbol=symbol,
            base_asset=_base_asset(symbol),
            quote_asset="USDT",
            status="active",
        )
        self.session.add(inst)
        await self.session.flush()
        return inst.id

    # ------------------------------------------------------------------
    # Fill resolution (no ledger writes; caller records)
    # ------------------------------------------------------------------
    async def _fill_proven(self, symbol: str, cid: str, submit_status: str) -> bool:
        if submit_status == "FILLED":
            return True
        if submit_status in _TERMINAL_NONFILL:
            return False
        for attempt in range(self.poll_max):
            if attempt > 0:
                await asyncio.sleep(self.poll_delay_seconds)
            try:
                order = await self.client.get_order(symbol=symbol, client_order_id=cid)
            except Exception:  # noqa: BLE001 — transient poll error, retry
                continue
            if order.status == "FILLED":
                return True
            if order.status in _TERMINAL_NONFILL:
                return False
        return False  # fail-closed: fill not proven

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
    ) -> tuple[str, Any]:
        cid = _new_cid()
        meta = {
            "source": "rob-307-pr2-executor",
            "role": role,
            "reason_codes": list(intent.reason_codes),
        }
        if intent.product == "usdm_futures":
            meta["leverage"] = 1
        await self.ledger.record_planned(
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
            now=self.now,
        )
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
        self, intent, ref, qty, notional, open_cid, instrument_id, *, exit_reason
    ) -> ExecutionResult:
        if intent.product == "usdm_futures":
            return await self._close_and_reconcile_futures(
                intent, ref, qty, notional, open_cid, instrument_id, exit_reason
            )
        return await self._close_and_reconcile_spot(
            intent, ref, qty, notional, open_cid, instrument_id, exit_reason
        )

    async def _close_and_reconcile_spot(
        self, intent, ref, qty, notional, open_cid, instrument_id, exit_reason
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
                extra_metadata_merge={"residual": "dust"},
            )
            if close_cid is not None and close_filled:
                await self.ledger.record_closed(client_order_id=close_cid, now=self.now)
                await self.ledger.record_reconciled(
                    client_order_id=close_cid, now=self.now
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
        )

    async def _close_and_reconcile_futures(
        self, intent, ref, qty, notional, open_cid, instrument_id, exit_reason
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
            cproven = await self._fill_proven(intent.symbol, close_cid, csubmit.status)
            if cproven:
                await self.ledger.record_filled(client_order_id=close_cid, now=self.now)
                close_filled = True
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
            await self.ledger.record_reconciled(client_order_id=open_cid, now=self.now)
            if close_cid is not None and close_filled:
                await self.ledger.record_closed(client_order_id=close_cid, now=self.now)
                await self.ledger.record_reconciled(
                    client_order_id=close_cid, now=self.now
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
        )
