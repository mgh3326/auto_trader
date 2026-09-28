"""H5 held-position runner core. No scheduler or live endpoint is registered."""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoOrderStatusResult,
    FuturesDemoPositionResult,
)
from research.nautilus_scalping.rob974_features import FOUR_HOUR_MS

from .client import H5DemoClient
from .exposure import assert_account_exposure, position_amount
from .forecast import ensure_entry_forecast, resolve_held_outcome
from .history import (
    collect_holding_history,
    collect_signal_history,
    complete_closes,
    minute_price,
)
from .holding import Holding, choose_exit
from .sizing import H5SizingBlocked, size_entry
from .state import (
    H5IntentSnapshot,
    H5OrderEvidence,
    H5SignalSnapshot,
    H5StateBlocked,
    H5StateService,
)
from .strategy import (
    IDENTITY,
    UNIVERSE,
    H5Strategy,
    assert_h5_demo_url,
    bar_price,
    bar_price_text,
    evaluate_all,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class H5TickResult:
    decision_ts: int
    event: str
    signal_keys: tuple[str, ...] = ()
    detail: str | None = None


def _evidence(order: FuturesDemoOrderStatusResult) -> H5OrderEvidence:
    raw = order.raw_response_redacted

    def broker_time(field: str) -> dt.datetime | None:
        value = raw.get(field)
        if type(value) is not int or not 0 < value < 2**63:
            return None
        return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC)

    return H5OrderEvidence(
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id,
        symbol=order.symbol,
        side=order.side,
        orig_qty=order.orig_qty,
        executed_qty=order.executed_qty,
        avg_price=order.avg_price,
        status=order.status,
        reduce_only=order.reduce_only,
        position_side=order.position_side,
        order_created_at=broker_time("time"),
        order_updated_at=broker_time("updateTime"),
    )


class H5Executor:
    def __init__(
        self,
        *,
        client: H5DemoClient,
        strategy: H5Strategy,
        state: H5StateService,
        demo_ledger: BinanceDemoLedgerService,
        ledger_session: AsyncSession,
    ) -> None:
        self.client = client
        self.strategy = strategy
        self.state = state
        self.demo_ledger = demo_ledger
        self.ledger_session = ledger_session

    def _guard(self, *, confirm: bool) -> None:
        """Reject a non-demo identity or disabled gate before any network/DB."""
        self.strategy.validate_client(self.client)
        assert_h5_demo_url(self.strategy.base_url)
        if self.strategy.strategy_id != IDENTITY:
            raise H5StateBlocked("non-H5 plugin identity")
        if os.environ.get("BINANCE_H5_DEMO_ENABLED") != "true":
            raise H5StateBlocked("BINANCE_H5_DEMO_ENABLED is disabled")
        if os.environ.get("BINANCE_FUTURES_DEMO_ENABLED") != "true":
            raise H5StateBlocked("BINANCE_FUTURES_DEMO_ENABLED is disabled")
        if confirm is not True:
            raise H5StateBlocked("per-call confirm=True required")

    async def _fresh_exposure(
        self,
        *,
        for_entry: bool,
        closing_symbol: str | None = None,
    ) -> list[FuturesDemoPositionResult]:
        positions = await self.client.get_all_positions()
        orders = await self.client.get_all_open_orders()
        signals = await self.state.list_active_signals()
        assert_account_exposure(
            signals=signals,
            positions=positions,
            open_orders=orders,
            for_entry=for_entry,
            closing_symbol=closing_symbol,
        )
        return positions

    async def _sync_demo_ledger(
        self,
        intent: H5IntentSnapshot,
        evidence: H5OrderEvidence,
        *,
        now: dt.datetime,
    ) -> None:
        """All writes to the unified demo order ledger use its service."""
        row = await self.demo_ledger.get_by_client_order_id(intent.client_order_id)
        if row is None:
            raise H5StateBlocked("demo ledger order row missing")
        state = row.lifecycle_state
        if state == "validated":
            if (
                evidence.status in {"CANCELED", "EXPIRED", "REJECTED"}
                and evidence.executed_qty == 0
            ):
                await self.demo_ledger.record_cancelled(
                    client_order_id=intent.client_order_id, now=now
                )
                await self.demo_ledger.record_reconciled(
                    client_order_id=intent.client_order_id, now=now
                )
                await self.ledger_session.commit()
                return
            await self.demo_ledger.record_submitted(
                client_order_id=intent.client_order_id,
                broker_order_id=evidence.broker_order_id,
                now=now,
            )
            state = "submitted"
        if state == "submitted" and evidence.status in {
            "CANCELED",
            "EXPIRED",
            "REJECTED",
        }:
            if evidence.executed_qty == 0:
                await self.demo_ledger.record_cancelled(
                    client_order_id=intent.client_order_id, now=now
                )
                await self.demo_ledger.record_reconciled(
                    client_order_id=intent.client_order_id, now=now
                )
            else:
                await self.demo_ledger.record_anomaly(
                    client_order_id=intent.client_order_id,
                    reason="partial_terminal_fill_requires_manual_review",
                    now=now,
                )
            await self.ledger_session.commit()
            return
        if state == "submitted" and evidence.status == "FILLED":
            await self.demo_ledger.record_filled(
                client_order_id=intent.client_order_id,
                now=now,
                extra_metadata_merge={
                    "fill_evidence": "broker_order_status",
                    "filled_qty": format(evidence.executed_qty, "f"),
                    "avg_price": format(evidence.avg_price, "f"),
                },
            )
            state = "filled"
        if intent.reduce_only and state == "filled":
            await self.demo_ledger.record_closed(
                client_order_id=intent.client_order_id, now=now
            )
            await self.demo_ledger.record_reconciled(
                client_order_id=intent.client_order_id, now=now
            )
        await self.ledger_session.commit()

    async def _reconcile_intent(
        self, intent: H5IntentSnapshot, *, now: dt.datetime
    ) -> H5SignalSnapshot | None:
        signal = await self.state.get_signal(intent.signal_key)
        if signal is None:
            raise H5StateBlocked("intent signal missing")
        try:
            order = await self.client.get_order(
                symbol=signal.symbol, client_order_id=intent.client_order_id
            )
        except BinanceDemoOrderNotFound:
            await self.state.mark_uncertain(intent.client_order_id, now=now)
            return None
        positions = await self.client.get_all_positions()
        amt = position_amount(positions, signal.symbol)
        evidence = _evidence(order)
        current, current_intent = await self.state.apply_order_evidence(
            evidence,
            broker_position_amt=amt,
            exit_bar_close_ts=(now_ms(evidence.order_updated_at or now) // FOUR_HOUR_MS)
            * FOUR_HOUR_MS,
            now=now,
        )
        try:
            await self._sync_demo_ledger(intent, evidence, now=now)
            if current.state == "closed":
                await self._close_root_if_proven(current, now=now)
            if current_intent.state == "evidenced":
                current_intent = await self.state.settle_intent(
                    intent.client_order_id, now=now
                )
        except Exception:
            await self.state.mark_uncertain(intent.client_order_id, now=now)
            raise
        if (
            current.state == "holding"
            and not current_intent.reduce_only
            and current_intent.state == "settled"
            and current.forecast_id is None
        ):
            try:
                await ensure_entry_forecast(current, self.state, now=now)
            except Exception:
                logger.exception("H5 entry forecast write failed")
        if current.state == "closed":
            try:
                refreshed = await self.state.get_signal(current.signal_key)
                if refreshed is not None and refreshed.forecast_id is None:
                    await ensure_entry_forecast(refreshed, self.state, now=now)
                    refreshed = await self.state.get_signal(current.signal_key)
                if refreshed is not None and refreshed.forecast_id:
                    await resolve_held_outcome(refreshed, now=now)
                    await self.state.mark_forecast_resolved(
                        refreshed.signal_key, refreshed.forecast_id, now=now
                    )
            except Exception:
                logger.exception("H5 held-outcome forecast resolution failed")
        return current

    async def _close_root_if_proven(
        self, signal: H5SignalSnapshot, *, now: dt.datetime
    ) -> None:
        if signal.state != "closed" or signal.entry_client_order_id is None:
            return
        positions = await self.client.get_all_positions()
        orders = await self.client.get_all_open_orders()
        if position_amount(positions, signal.symbol) != 0 or orders.orders:
            raise H5StateBlocked("root release lacks flat and empty-order evidence")
        row = await self.demo_ledger.get_by_client_order_id(
            signal.entry_client_order_id
        )
        if row is None:
            raise H5StateBlocked("demo root missing")
        if row.lifecycle_state == "reconciled":
            return
        if row.lifecycle_state not in {"filled", "closed"}:
            raise H5StateBlocked("demo root not evidence-filled")
        if row.lifecycle_state == "filled":
            await self.demo_ledger.record_closed(
                client_order_id=signal.entry_client_order_id,
                now=now,
                extra_metadata_merge={
                    "exit_reason": signal.exit_reason,
                    "h5_signal_key": signal.signal_key,
                },
            )
        await self.demo_ledger.record_reconciled(
            client_order_id=signal.entry_client_order_id, now=now
        )
        await self.ledger_session.commit()

    async def _send_reserved(
        self,
        *,
        intent: H5IntentSnapshot,
        signal: H5SignalSnapshot,
        now: dt.datetime,
    ) -> H5SignalSnapshot | None:
        # This is the final commit before the first possible broker write.
        await self.state.fence_send(intent.client_order_id, now=now)
        try:
            await self.client.submit_order(
                symbol=signal.symbol,
                side=intent.side,
                order_type="MARKET",
                qty=intent.qty,
                client_order_id=intent.client_order_id,
                reduce_only=intent.reduce_only,
                position_side="BOTH",
                confirm=True,
            )
        except Exception:
            await self.state.mark_uncertain(intent.client_order_id, now=now)
            raise
        # A submit response, even FILLED, is not the only fill authority.
        # Read the exact client order ID and complete positions before booking.
        try:
            return await self._reconcile_intent(intent, now=now)
        except Exception:
            await self.state.mark_uncertain(intent.client_order_id, now=now)
            raise

    async def _prepare_demo_entry(
        self,
        *,
        signal: H5SignalSnapshot,
        qty: Decimal,
        notional_usdt: Decimal,
        now: dt.datetime,
    ) -> str:
        instrument_id = await self.demo_ledger.resolve_or_create_instrument(
            venue="binance",
            product="usdm_futures",
            venue_symbol=signal.symbol,
            base_asset=signal.symbol.removesuffix("USDT"),
            quote_asset="USDT",
        )
        await self.ledger_session.commit()
        cid = self._entry_cid(signal.signal_key)
        reservation = await self.demo_ledger.reserve_root_planned(
            instrument_id=instrument_id,
            product="usdm_futures",
            venue_host="demo-fapi.binance.com",
            client_order_id=cid,
            side=signal.side,
            order_type="MARKET",
            qty=qty,
            price=None,
            notional_usdt=notional_usdt,
            extra_metadata={
                "source": "h5-ls-env-v1",
                "h5_signal_key": signal.signal_key,
                "correlation_id": signal.correlation_id,
                "decision_ts": signal.decision_ts,
            },
            idempotency_metadata={
                "h5_signal_key": signal.signal_key,
                "strategy_id": IDENTITY,
            },
            global_open_root_cap=2,
            now=now,
        )
        if reservation.status != "reserved":
            raise H5StateBlocked(reservation.reason or reservation.status)
        await self.demo_ledger.record_previewed(client_order_id=cid, now=now)
        await self.demo_ledger.record_validated(client_order_id=cid, now=now)
        await self.ledger_session.commit()
        return cid

    async def _release_demo_unsent(self, cid: str, *, now: dt.datetime) -> None:
        """Only a pre-send root may be cancelled and reconciled."""
        row = await self.demo_ledger.get_by_client_order_id(cid)
        if row is None:
            return
        if row.lifecycle_state not in {"planned", "previewed", "validated"}:
            raise H5StateBlocked("cannot release possibly sent demo root")
        await self.demo_ledger.record_cancelled(client_order_id=cid, now=now)
        await self.demo_ledger.record_reconciled(client_order_id=cid, now=now)
        await self.ledger_session.commit()

    @staticmethod
    def _entry_cid(signal_key: str) -> str:
        from .state import h5_client_order_id

        return h5_client_order_id(signal_key, "entry")

    async def _enter(
        self,
        signal: H5SignalSnapshot,
        *,
        completed_bar_closes: tuple[int, ...],
        now: dt.datetime,
    ) -> H5TickResult:
        await self._fresh_exposure(for_entry=True)
        await self.client.assert_symbol_isolated_1x(signal.symbol)
        mode = await self.client.get_position_mode()
        if mode.is_hedge_mode:
            raise H5StateBlocked("H5 requires one-way position mode")
        account = await self.client.read_account()
        await self.state.record_nav(nav_usdt=account.nav_usdt, now=now)
        quote = await self.client.get_book_quote(signal.symbol)
        filters = await self.client.get_h5_filters(signal.symbol)
        size = size_entry(
            nav_usdt=account.nav_usdt,
            executable_price=quote.executable_price(signal.side),
            step_size=filters.step_size,
            min_notional_usdt=filters.min_notional_usdt,
            min_qty=filters.min_qty,
            max_qty=filters.max_qty,
            quantity_precision=filters.quantity_precision,
        )
        reserved = await self.state.reserve_entry(
            signal_key=signal.signal_key,
            completed_bar_closes=completed_bar_closes,
            entry_nav_usdt=account.nav_usdt,
            now=now,
        )
        cid = self._entry_cid(signal.signal_key)
        try:
            await self._prepare_demo_entry(
                signal=reserved,
                qty=size.qty,
                notional_usdt=size.notional_usdt,
                now=now,
            )
        except Exception:
            await self._release_demo_unsent(cid, now=now)
            await self.state.block_unsent(signal.signal_key, now=now)
            raise
        # A second complete account read immediately before intent creation
        # detects pre-submit foreign exposure; any failure stays fail-closed.
        try:
            await self._fresh_exposure(for_entry=True)
            await self.client.assert_symbol_isolated_1x(signal.symbol)
            intent = await self.state.reserve_intent(
                signal_key=signal.signal_key,
                leg_key="entry",
                side=signal.side,
                qty=size.qty,
                reduce_only=False,
                now=now,
            )
        except Exception:
            await self._release_demo_unsent(cid, now=now)
            await self.state.block_unsent(signal.signal_key, now=now)
            raise
        result = await self._send_reserved(intent=intent, signal=reserved, now=now)
        return H5TickResult(
            decision_ts=signal.decision_ts,
            event=(
                "entry_uncertain"
                if result is None
                else "entry_filled"
                if result.state == "holding"
                else "entry_pending"
            ),
            signal_keys=(signal.signal_key,),
        )

    async def _close(
        self,
        signal: H5SignalSnapshot,
        *,
        reason: str,
        qty: Decimal,
        now: dt.datetime,
    ) -> H5TickResult:
        await self._fresh_exposure(for_entry=False, closing_symbol=signal.symbol)
        await self.client.assert_symbol_isolated_1x(signal.symbol)
        side = "SELL" if signal.side == "BUY" else "BUY"
        leg_key = f"{reason}:{format(signal.closed_qty, 'f')}"
        intent = await self.state.reserve_intent(
            signal_key=signal.signal_key,
            leg_key=leg_key,
            side=side,
            qty=qty,
            reduce_only=True,
            now=now,
        )
        instrument_id = await self.demo_ledger.resolve_or_create_instrument(
            venue="binance",
            product="usdm_futures",
            venue_symbol=signal.symbol,
            base_asset=signal.symbol.removesuffix("USDT"),
            quote_asset="USDT",
        )
        await self.demo_ledger.record_planned(
            instrument_id=instrument_id,
            product="usdm_futures",
            venue_host="demo-fapi.binance.com",
            client_order_id=intent.client_order_id,
            parent_client_order_id=signal.entry_client_order_id,
            side=side,
            order_type="MARKET",
            qty=qty,
            price=None,
            extra_metadata={
                "source": "h5-ls-env-v1",
                "h5_signal_key": signal.signal_key,
                "correlation_id": signal.correlation_id,
                "exit_reason": reason,
                "reduce_only": True,
            },
            now=now,
        )
        await self.demo_ledger.record_previewed(
            client_order_id=intent.client_order_id, now=now
        )
        await self.demo_ledger.record_validated(
            client_order_id=intent.client_order_id, now=now
        )
        await self.ledger_session.commit()
        # The fresh account read after reservation verifies the close quantity
        # still belongs to this signal; no other symbol is touched.
        await self._fresh_exposure(for_entry=False, closing_symbol=signal.symbol)
        result = await self._send_reserved(intent=intent, signal=signal, now=now)
        return H5TickResult(
            decision_ts=(now_ms(now) // FOUR_HOUR_MS) * FOUR_HOUR_MS,
            event="close_sent" if result else "close_uncertain",
            signal_keys=(signal.signal_key,),
            detail=reason,
        )

    async def _manage_holding(
        self, signal: H5SignalSnapshot, *, now: dt.datetime
    ) -> H5TickResult | None:
        if signal.entered_at is None or signal.entry_price is None:
            raise H5StateBlocked("held signal missing fill evidence")
        positions = await self._fresh_exposure(
            for_entry=False, closing_symbol=signal.symbol
        )
        remaining = abs(position_amount(positions, signal.symbol))
        quote = await self.client.get_book_quote(signal.symbol)
        minutes, bars = await collect_holding_history(
            self.client,
            signal.symbol,
            entered_ms=now_ms(signal.entered_at),
            now_ms=now_ms(now),
        )
        filters = await self.client.get_h5_filters(signal.symbol)
        completed = [bar for bar in bars if bar.close_ts > now_ms(signal.entered_at)]
        decision = choose_exit(
            Holding(
                side=signal.side,
                entry_price=signal.entry_price,
                entry_qty=signal.entry_qty,
                broker_remaining_qty=remaining,
                closed_qty=signal.closed_qty,
                entered_at=signal.entered_at,
                completed_bars_held=len(completed),
                step_size=filters.step_size,
            ),
            quote_price=quote.executable_price(
                "SELL" if signal.side == "BUY" else "BUY"
            ),
            intrabar_low=min(
                (minute_price(bar, "low") for bar in minutes), default=None
            ),
            intrabar_high=max(
                (minute_price(bar, "high") for bar in minutes), default=None
            ),
            completed_bar_close=bar_price(completed[-1], "close")
            if completed
            else None,
            now=now,
        )
        if decision is None:
            return None
        return await self._close(
            signal, reason=decision.reason, qty=decision.qty, now=now
        )

    async def run_tick(
        self, *, now: dt.datetime, confirm: bool = False
    ) -> H5TickResult:
        self._guard(confirm=confirm)
        if now.tzinfo is None:
            raise H5StateBlocked("aware clock required")
        decision_ts = (now_ms(now) // FOUR_HOUR_MS) * FOUR_HOUR_MS
        account = await self.client.read_account()
        await self.state.record_nav(nav_usdt=account.nav_usdt, now=now)
        unresolved = await self.state.list_unresolved_intents()
        for intent in unresolved:
            if intent.state == "reserved":
                return H5TickResult(
                    decision_ts, "blocked", detail="pre-send intent needs review"
                )
            try:
                await self._reconcile_intent(intent, now=now)
            except Exception as exc:
                return H5TickResult(decision_ts, "blocked", detail=type(exc).__name__)
        if await self.state.list_unresolved_intents():
            return H5TickResult(
                decision_ts, "blocked", detail="order reconciliation pending"
            )
        for signal in await self.state.list_active_signals():
            if signal.state == "uncertain":
                return H5TickResult(
                    decision_ts, "blocked", detail="uncertain H5 exposure"
                )
            if signal.state == "holding":
                managed = await self._manage_holding(signal, now=now)
                if managed is not None:
                    return managed
        for forecast_signal in await self.state.list_forecast_recovery_signals():
            try:
                if forecast_signal.forecast_id is None:
                    await ensure_entry_forecast(forecast_signal, self.state, now=now)
                    forecast_signal = await self.state.get_signal(
                        forecast_signal.signal_key
                    )
                if (
                    forecast_signal is not None
                    and forecast_signal.state == "closed"
                    and forecast_signal.forecast_id is not None
                    and forecast_signal.forecast_resolved_at is None
                ):
                    await resolve_held_outcome(forecast_signal, now=now)
                    await self.state.mark_forecast_resolved(
                        forecast_signal.signal_key,
                        forecast_signal.forecast_id,
                        now=now,
                    )
            except Exception as exc:
                return H5TickResult(
                    decision_ts,
                    "blocked",
                    detail=f"forecast_recovery:{type(exc).__name__}",
                )
        if await self.state.decision_processed(decision_ts):
            return H5TickResult(decision_ts, "already_processed")
        histories = {
            symbol: await collect_signal_history(
                self.client, symbol, decision_ts=decision_ts
            )
            for symbol in UNIVERSE
        }
        if any(
            not bars or bars[-1].close_ts != decision_ts for bars in histories.values()
        ):
            return H5TickResult(decision_ts, "no_complete_4h_bar")
        quotes = {
            symbol: await self.client.get_book_quote(symbol) for symbol in UNIVERSE
        }
        close_texts = {
            symbol: bar_price_text(histories[symbol][-1], "close")
            for symbol in UNIVERSE
        }
        await self.state.record_opportunity_grid(
            [
                (
                    symbol,
                    decision_ts,
                    bar_price(histories[symbol][-1], "open"),
                    bar_price(histories[symbol][-1], "high"),
                    bar_price(histories[symbol][-1], "low"),
                    close_texts[symbol],
                    quotes[symbol].bid,
                    quotes[symbol].ask,
                )
                for symbol in UNIVERSE
            ]
        )
        signals = evaluate_all(self.strategy, histories, decision_ts=decision_ts)
        records: list[H5SignalSnapshot] = []
        for signal in signals:
            price_text = close_texts[signal.symbol]
            record = await self.state.observe_signal(signal, price_text)
            records.append(record)
        sent: list[str] = []
        for record in records:
            if record.state != "observed":
                continue
            try:
                result = await self._enter(
                    record,
                    completed_bar_closes=complete_closes(histories[record.symbol]),
                    now=now,
                )
            except (H5StateBlocked, H5SizingBlocked):
                try:
                    await self.state.block_unsent(record.signal_key, now=now)
                except H5StateBlocked:
                    pass  # a committed intent keeps this signal blocked
                continue
            sent.extend(result.signal_keys)
            if result.event != "entry_filled":
                break
        for record in records:
            if record.signal_key not in sent:
                fresh = await self.state.get_signal(record.signal_key)
                if fresh is not None and fresh.state == "observed":
                    await self.state.block_unsent(record.signal_key, now=now)
        await self.state.mark_decision_processed(decision_ts, now=now)
        return H5TickResult(
            decision_ts,
            "entry_sent" if sent else "no_entry",
            tuple(record.signal_key for record in records),
        )


def now_ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)
