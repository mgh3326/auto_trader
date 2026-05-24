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
# Spot OCO stop-limit price sits a hair below the stop trigger so a SELL
# stop-limit reliably fills once triggered.
_SL_LIMIT_BUFFER_BPS = Decimal("5")


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

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "product": self.intent.product,
            "symbol": self.intent.symbol,
            "side": self.intent.side,
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
        poll_max: int = _FILL_POLL_MAX,
        poll_delay_seconds: float = _FILL_POLL_DELAY_SECONDS,
    ) -> None:
        self.product = product
        self.client = client
        self.session = session
        self.reference = reference
        self.now = now
        self.limits = limits or ScalpingRiskLimits()
        self.ledger = BinanceDemoLedgerService(session)
        self.poll_max = poll_max
        self.poll_delay_seconds = poll_delay_seconds

    async def execute(
        self, intent: OrderIntent, *, confirm: bool = False
    ) -> ExecutionResult:
        # 1. Risk re-check against the live ledger (durable caps + symbol gate).
        snapshot = await load_ledger_snapshot(
            self.ledger, product=intent.product, symbol=intent.symbol, now=self.now
        )
        market = MarketConditions(
            spread_bps=Decimal("0"),
            data_age_seconds=0.0,
            spot_free_base_qty=Decimal("0"),
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

        # 2. Reference + sizing (floor; never round up past the cap).
        ref = await self.reference.fetch(intent.product, intent.symbol)
        sized = self._size(intent, ref)
        if isinstance(sized, str):
            return ExecutionResult(
                intent=intent, status="blocked", reason_codes=(sized,)
            )
        qty, notional = sized

        # 3. Dry-run gate: zero broker mutation.
        if not confirm:
            return ExecutionResult(
                intent=intent,
                status="dry_run",
                sized_qty=qty,
                sized_notional_usdt=notional,
            )

        instrument_id = await self._resolve_or_create_instrument(intent.symbol)
        if intent.product == "usdm_futures":
            return await self._execute_futures(
                intent, ref, qty, notional, instrument_id
            )
        return await self._execute_spot(intent, ref, qty, notional, instrument_id)

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
    # Broker-side bracket (PR3): open + place exchange-native TP/SL, hold.
    # ------------------------------------------------------------------
    async def execute_bracket(
        self,
        intent: OrderIntent,
        *,
        confirm: bool = False,
        tp_bps: Decimal = Decimal("30"),
        sl_bps: Decimal = Decimal("20"),
    ) -> ExecutionResult:
        """Open, place broker-side TP+SL, and **leave the protected position**.

        Unlike :meth:`execute` (open + close-flat), this holds the position
        with exchange-native exits resting (futures: STOP_MARKET +
        TAKE_PROFIT_MARKET reduceOnly; spot: one SELL OCO). Terminal status
        ``bracketed`` means held + protected; the position is intentionally
        not flat. Exit detection + survivor-leg cleanup is a separate
        reconcile step (later poll / scheduler tick).
        """
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

        long = intent.side == "BUY"
        if long:
            tp = _align_price(ref.price * (Decimal("1") + tp_bps / _BPS), ref.tick_size)
            sl = _align_price(ref.price * (Decimal("1") - sl_bps / _BPS), ref.tick_size)
            close_side = "SELL"
        else:
            tp = _align_price(ref.price * (Decimal("1") - tp_bps / _BPS), ref.tick_size)
            sl = _align_price(ref.price * (Decimal("1") + sl_bps / _BPS), ref.tick_size)
            close_side = "BUY"

        if not confirm:
            return ExecutionResult(
                intent=intent,
                status="dry_run",
                sized_qty=qty,
                sized_notional_usdt=notional,
            )

        instrument_id = await self._resolve_or_create_instrument(intent.symbol)
        if intent.product == "usdm_futures":
            mode = await self.client.get_position_mode()
            if mode.is_hedge_mode:
                return ExecutionResult(
                    intent=intent,
                    status="blocked",
                    reason_codes=("futures_hedge_mode_blocked",),
                )
            leverage = await self.client.set_leverage(symbol=intent.symbol, leverage=1)
            if leverage.leverage != 1:
                return ExecutionResult(
                    intent=intent,
                    status="blocked",
                    reason_codes=("futures_leverage_mismatch",),
                )

        open_cid, submit = await self._open_leg(
            intent, instrument_id, qty, notional, tp_price=tp, sl_price=sl
        )
        proven = await self._fill_proven(intent.symbol, open_cid, submit.status)
        if not proven and intent.product == "usdm_futures":
            position = await self.client.get_position(symbol=intent.symbol)
            proven = not position.is_flat
        if not proven:
            reason = "bracket_open_fill_unproven"
            await self.ledger.record_anomaly(
                client_order_id=open_cid, reason=reason, now=self.now
            )
            return ExecutionResult(
                intent=intent,
                status="anomaly",
                open_client_order_id=open_cid,
                anomaly_reason=reason,
                sized_qty=qty,
                sized_notional_usdt=notional,
            )

        try:
            bracket_info, bracket_cids = await self._place_bracket(
                intent, ref, qty, tp=tp, sl=sl, close_side=close_side
            )
        except Exception as exc:  # noqa: BLE001 — failsafe: never leave unprotected
            await self._failsafe_close(intent, qty, close_side)
            reason = f"bracket_placement_failed_position_closed: {exc}"
            await self.ledger.record_anomaly(
                client_order_id=open_cid, reason=reason, now=self.now
            )
            return ExecutionResult(
                intent=intent,
                status="anomaly",
                open_client_order_id=open_cid,
                anomaly_reason=reason,
                sized_qty=qty,
                sized_notional_usdt=notional,
                final_flat=True,
            )

        await self.ledger.record_filled(
            client_order_id=open_cid,
            now=self.now,
            extra_metadata_merge={"bracket": bracket_info},
        )
        return ExecutionResult(
            intent=intent,
            status="bracketed",
            open_client_order_id=open_cid,
            bracket_client_order_ids=bracket_cids,
            sized_qty=qty,
            sized_notional_usdt=notional,
            final_flat=False,
        )

    async def _place_bracket(
        self, intent, ref, qty, *, tp: Decimal, sl: Decimal, close_side: str
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        if intent.product == "usdm_futures":
            position = await self.client.get_position(symbol=intent.symbol)
            close_qty = abs(position.position_amt) or qty
            sl_cid = _new_cid()
            tp_cid = _new_cid()
            await self.client.submit_reduce_only_trigger(
                symbol=intent.symbol,
                side=close_side,
                order_type="STOP_MARKET",
                qty=close_qty,
                stop_price=sl,
                client_order_id=sl_cid,
                confirm=True,
            )
            await self.client.submit_reduce_only_trigger(
                symbol=intent.symbol,
                side=close_side,
                order_type="TAKE_PROFIT_MARKET",
                qty=close_qty,
                stop_price=tp,
                client_order_id=tp_cid,
                confirm=True,
            )
            info = {
                "type": "futures_triggers",
                "sl_client_order_id": sl_cid,
                "tp_client_order_id": tp_cid,
                "sl_price": str(sl),
                "tp_price": str(tp),
                "close_side": close_side,
            }
            return info, (sl_cid, tp_cid)

        # Spot OCO on the free base balance (never the raw BUY qty).
        balance = await self.client.get_asset_balance(asset=_base_asset(intent.symbol))
        close = compute_close_qty(
            free_balance=balance.free,
            price=ref.price,
            min_notional=ref.min_notional,
            step_size=ref.step_size,
        )
        if not isinstance(close, CloseQtyResult):
            raise RuntimeError(f"spot bracket qty not sellable: {close}")
        sl_limit = _align_price(
            sl * (Decimal("1") - _SL_LIMIT_BUFFER_BPS / _BPS), ref.tick_size
        )
        oco_cid = _new_cid()
        oco = await self.client.submit_oco(
            symbol=intent.symbol,
            side="SELL",
            quantity=close.qty,
            tp_price=tp,
            sl_stop_price=sl,
            sl_limit_price=sl_limit,
            list_client_order_id=oco_cid,
            confirm=True,
        )
        info = {
            "type": "spot_oco",
            "order_list_id": oco.order_list_id,
            "leg_client_order_ids": list(oco.leg_client_order_ids),
            "tp_price": str(tp),
            "sl_stop_price": str(sl),
            "sl_limit_price": str(sl_limit),
        }
        return info, tuple(oco.leg_client_order_ids)

    async def _failsafe_close(self, intent, qty, close_side) -> None:
        """Best-effort flatten after a bracket-placement failure."""
        try:
            if intent.product == "usdm_futures":
                await self.client.submit_order(
                    symbol=intent.symbol,
                    side=close_side,
                    order_type="MARKET",
                    qty=qty,
                    client_order_id=_new_cid(),
                    reduce_only=True,
                    confirm=True,
                )
            else:
                balance = await self.client.get_asset_balance(
                    asset=_base_asset(intent.symbol)
                )
                close = compute_close_qty(
                    free_balance=balance.free,
                    price=Decimal("1"),
                    min_notional=Decimal("0"),
                    step_size=Decimal("0.00000001"),
                )
                if isinstance(close, CloseQtyResult):
                    await self.client.submit_order(
                        symbol=intent.symbol,
                        side="SELL",
                        order_type="MARKET",
                        qty=close.qty,
                        client_order_id=_new_cid(),
                        confirm=True,
                    )
        except Exception:  # noqa: BLE001 — failsafe is best-effort
            logger.exception("failsafe close failed for %s", intent.symbol)

    # ------------------------------------------------------------------
    # Spot lifecycle
    # ------------------------------------------------------------------
    async def _execute_spot(
        self, intent, ref, qty, notional, instrument_id
    ) -> ExecutionResult:
        open_cid, submit = await self._open_leg(intent, instrument_id, qty, notional)
        if submit.status != "FILLED":
            reason = f"spot_open_not_filled: {submit.status}"
            await self.ledger.record_anomaly(
                client_order_id=open_cid, reason=reason, now=self.now
            )
            return ExecutionResult(
                intent=intent,
                status="anomaly",
                open_client_order_id=open_cid,
                anomaly_reason=reason,
                sized_qty=qty,
                sized_notional_usdt=notional,
            )
        await self.ledger.record_filled(client_order_id=open_cid, now=self.now)

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
        )

    # ------------------------------------------------------------------
    # Futures lifecycle
    # ------------------------------------------------------------------
    async def _execute_futures(
        self, intent, ref, qty, notional, instrument_id
    ) -> ExecutionResult:
        mode = await self.client.get_position_mode()
        if mode.is_hedge_mode:
            return ExecutionResult(
                intent=intent,
                status="blocked",
                reason_codes=("futures_hedge_mode_blocked",),
            )
        leverage = await self.client.set_leverage(symbol=intent.symbol, leverage=1)
        if leverage.leverage != 1:
            return ExecutionResult(
                intent=intent,
                status="blocked",
                reason_codes=("futures_leverage_mismatch",),
            )

        open_cid, submit = await self._open_leg(intent, instrument_id, qty, notional)
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
                return ExecutionResult(
                    intent=intent,
                    status="anomaly",
                    open_client_order_id=open_cid,
                    anomaly_reason=reason,
                    sized_qty=qty,
                    sized_notional_usdt=notional,
                    final_flat=True,
                )

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
        )
