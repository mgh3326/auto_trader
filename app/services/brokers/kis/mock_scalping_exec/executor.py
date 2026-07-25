"""ROB-321 PR4a — KIS mock scalping monitored round-trip executor.

Drives one long round trip end to end: submit a mock BUY, confirm its fill,
monitor the quote, exit on TP/SL/time-stop with a `scalping_exit` aggressive
limit SELL, confirm the exit fill, and reconcile the pair into the ledger with
gross/net PnL. The terminal closed state is the existing ``reconciled``.

Round-trip close is reconciled from **execution evidence** (the entry/exit fills
returned by the broker port), NOT from a holdings delta — so a fast same-session
round trip that never surfaces an intermediate position is still a clean close,
not an anomaly (the ROB-321 §5 fix).

All broker/ledger I/O is injected via ports so the orchestration is unit-tested
with fakes. A real adapter (calling ``_place_order_impl(is_mock=True,
scalping_exit=...)`` + the kis_mock_ledger writer) is wired in PR4b. The HTTP
mutation is **confirm-gated**: ``confirm=False`` (default) previews only and
writes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from app.services.brokers.kis.mock_scalping.contract import (
    LedgerSnapshot,
    MarketConditions,
    ScalpingRiskLimits,
    Side,
    evaluate_risk,
)
from app.services.brokers.kis.mock_scalping.order_intent import OrderIntent
from app.services.brokers.kis.mock_scalping_exec.exit_policy import (
    TIME_STOP,
    decide_exit,
)

logger = logging.getLogger("rob321.kis_mock_scalping_exec")

_BPS = Decimal("10000")


@dataclass(frozen=True)
class Quote:
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None


@dataclass(frozen=True)
class Fill:
    price: Decimal
    quantity: Decimal


class BrokerPort(Protocol):
    async def submit_buy(
        self,
        *,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        correlation_id: str,
        confirm: bool,
    ) -> dict[str, Any]: ...

    async def submit_exit_sell(
        self,
        *,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        exit_reason: str,
        strategy_id: str,
        correlation_id: str,
        confirm: bool,
    ) -> dict[str, Any]: ...

    async def confirm_fill(self, submit_result: dict[str, Any]) -> Fill | None: ...

    def quote(self, symbol: str) -> Quote | None: ...


@dataclass(frozen=True)
class RiskInputs:
    """Fresh durable + market snapshot for the executor-owned final risk gate."""

    ledger: LedgerSnapshot
    market: MarketConditions


class RiskGatePort(Protocol):
    """Loads a fresh ledger/position + market snapshot immediately before send.

    ``load`` MUST raise on any snapshot load/parse/freshness fault so the
    executor can fail-close to zero broker mutation (ROB-843).
    """

    async def load(self, *, symbol: str, side: Side) -> RiskInputs: ...


class LedgerPort(Protocol):
    async def record_entry(
        self,
        *,
        correlation_id: str,
        symbol: str,
        strategy_id: str,
        fill: Fill,
    ) -> None: ...

    async def record_exit_reconciled(
        self,
        *,
        correlation_id: str,
        symbol: str,
        exit_reason: str,
        entry_fill: Fill,
        exit_fill: Fill,
        gross_pnl: Decimal,
        net_pnl: Decimal,
        fees: Decimal,
    ) -> None: ...

    async def record_anomaly(
        self, *, correlation_id: str, symbol: str, side: Side, detail: str
    ) -> None: ...


@dataclass(frozen=True)
class ExecutorConfig:
    max_hold_seconds: float = 120.0
    poll_interval_seconds: float = 1.0
    max_poll_count: int = 30
    max_runtime_seconds: float = 300.0
    max_fill_polls: int = 10
    # KR round-trip cost estimate (sell tax + commission, both legs), bps of
    # notional. Used only for net PnL telemetry; mock has no real fee.
    round_trip_fee_bps: Decimal = Decimal("23")


@dataclass(frozen=True)
class RoundTripResult:
    correlation_id: str
    status: str  # reconciled | dry_run | blocked | entry_unfilled | anomaly
    exit_reason: str | None = None
    quantity: Decimal | None = None
    entry_fill_price: Decimal | None = None
    exit_fill_price: Decimal | None = None
    gross_pnl: Decimal | None = None
    net_pnl: Decimal | None = None
    fees: Decimal | None = None
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    detail: str | None = None


class MockScalpingExecutor:
    def __init__(
        self,
        *,
        broker: BrokerPort,
        ledger: LedgerPort,
        config: ExecutorConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = None,  # type: ignore[assignment]
        risk: RiskGatePort | None = None,
        limits: ScalpingRiskLimits | None = None,
    ) -> None:
        import time

        self._broker = broker
        self._ledger = ledger
        self._config = config or ExecutorConfig()
        self._sleep = sleep
        self._clock = clock or time.monotonic
        # ROB-843: executor-owned final risk re-check. Production wiring MUST
        # inject a gate; a confirm-mode executor without one fail-closes.
        self._risk = risk
        self._limits = limits or ScalpingRiskLimits()

    def _new_correlation_id(self) -> str:
        return f"kis-mock-scalp-{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _size(intent: OrderIntent) -> Decimal:
        """Whole-share quantity from the KRW notional cap (floor, never round up)."""
        ref = intent.entry_reference_price
        if ref is None or ref <= 0:
            return Decimal("0")
        return (intent.target_notional_krw / ref).quantize(
            Decimal("1"), rounding=ROUND_DOWN
        )

    async def execute_monitored(
        self, intent: OrderIntent, *, confirm: bool = False
    ) -> RoundTripResult:
        cid = self._new_correlation_id()
        strategy_id = "kis-mock-v1"
        qty = self._size(intent)
        if qty <= 0:
            return RoundTripResult(
                correlation_id=cid, status="blocked", reason_codes=("size_zero",)
            )
        entry_ref = intent.entry_reference_price
        assert entry_ref is not None  # _size guards None/<=0

        # 0. Executor-owned final risk re-check (ROB-843). Reloads a fresh
        # ledger/position + market snapshot and runs evaluate_risk immediately
        # before any submit. Caller-computed risk is advisory only; any denial
        # or snapshot fault fail-closes here with ZERO broker calls.
        blocked = await self._final_risk_gate(intent, cid=cid, confirm=confirm)
        if blocked is not None:
            return blocked

        # 1. Submit BUY (confirm-gated).
        buy = await self._broker.submit_buy(
            symbol=intent.symbol,
            price=entry_ref,
            quantity=qty,
            correlation_id=cid,
            confirm=confirm,
        )
        # ROB-843 P1: the broker refused to POST — either the final pre-send
        # freshness re-check blocked it (book went stale/crossed after the risk
        # gate) or the write-ahead durable reservation could not be recorded
        # (POST 0). Zero broker calls, no fill, no ledger — surface as blocked.
        if isinstance(buy, dict) and (
            buy.get("pre_send_blocked") or buy.get("reservation_blocked")
        ):
            return RoundTripResult(
                correlation_id=cid,
                status="blocked",
                quantity=qty,
                reason_codes=tuple(buy.get("reason_codes") or ("pre_send_freshness",)),
                detail=buy.get("detail"),
            )
        if not confirm:
            logger.info("dry-run scalping entry symbol=%s qty=%s", intent.symbol, qty)
            return RoundTripResult(correlation_id=cid, status="dry_run", quantity=qty)

        # 2. Confirm entry fill (bounded).
        entry_fill = await self._await_fill(buy)
        if entry_fill is None:
            # ROB-843 P2: an entry-unfilled anomaly is the BUY leg (side=BUY) so
            # it de-dups with the native BUY row in the daily count.
            await self._ledger.record_anomaly(
                correlation_id=cid,
                symbol=intent.symbol,
                side="BUY",
                detail="entry_unfilled",
            )
            return RoundTripResult(
                correlation_id=cid,
                status="entry_unfilled",
                quantity=qty,
                reason_codes=("entry_unfilled",),
            )
        await self._ledger.record_entry(
            correlation_id=cid,
            symbol=intent.symbol,
            strategy_id=strategy_id,
            fill=entry_fill,
        )

        # 3. Monitor until TP/SL/time-stop.
        exit_reason = await self._monitor(intent)

        # 4. Submit exit SELL (aggressive limit at the bid; scalping_exit bypass).
        sell_price = self._exit_price(intent)
        sell = await self._broker.submit_exit_sell(
            symbol=intent.symbol,
            price=sell_price,
            quantity=entry_fill.quantity,
            exit_reason=exit_reason,
            strategy_id=strategy_id,
            correlation_id=cid,
            confirm=True,
        )
        exit_fill = await self._await_fill(sell)
        if exit_fill is None:
            # Failsafe: cannot prove the close — never report a clean success.
            # The exit-unconfirmed anomaly is the SELL leg (side=SELL).
            await self._ledger.record_anomaly(
                correlation_id=cid,
                symbol=intent.symbol,
                side="SELL",
                detail="exit_unconfirmed",
            )
            return RoundTripResult(
                correlation_id=cid,
                status="anomaly",
                exit_reason=exit_reason,
                quantity=entry_fill.quantity,
                entry_fill_price=entry_fill.price,
                reason_codes=("exit_unconfirmed",),
            )

        # 5. Reconcile the round trip from fill evidence.
        gross = (exit_fill.price - entry_fill.price) * entry_fill.quantity
        fees = self._estimate_fees(entry_fill, exit_fill)
        net = gross - fees
        await self._ledger.record_exit_reconciled(
            correlation_id=cid,
            symbol=intent.symbol,
            exit_reason=exit_reason,
            entry_fill=entry_fill,
            exit_fill=exit_fill,
            gross_pnl=gross,
            net_pnl=net,
            fees=fees,
        )
        logger.info(
            "scalping round-trip reconciled symbol=%s reason=%s gross=%s net=%s",
            intent.symbol,
            exit_reason,
            gross,
            net,
        )
        return RoundTripResult(
            correlation_id=cid,
            status="reconciled",
            exit_reason=exit_reason,
            quantity=entry_fill.quantity,
            entry_fill_price=entry_fill.price,
            exit_fill_price=exit_fill.price,
            gross_pnl=gross,
            net_pnl=net,
            fees=fees,
        )

    async def _final_risk_gate(
        self, intent: OrderIntent, *, cid: str, confirm: bool
    ) -> RoundTripResult | None:
        """Executor-owned pre-send risk gate. Returns a blocked ``RoundTripResult``
        to short-circuit (zero broker calls), or ``None`` to proceed.

        * No wired gate + ``confirm`` → fail-close (``risk_gate_unconfigured``);
          the executor never mutates without owning the final check.
        * No wired gate + dry-run → legacy passthrough (no mutation happens).
        * Snapshot load/parse/freshness fault → ``risk_snapshot_unavailable``.
        * Any risk denial → the accumulated stable reason codes.
        """
        if self._risk is None:
            if confirm:
                return RoundTripResult(
                    correlation_id=cid,
                    status="blocked",
                    reason_codes=("risk_gate_unconfigured",),
                )
            return None

        try:
            inputs = await self._risk.load(symbol=intent.symbol, side=intent.side)
        except Exception as exc:  # noqa: BLE001 — any snapshot fault fails closed
            logger.warning(
                "risk snapshot unavailable symbol=%s: %s", intent.symbol, exc
            )
            return RoundTripResult(
                correlation_id=cid,
                status="blocked",
                reason_codes=("risk_snapshot_unavailable",),
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )

        decision = evaluate_risk(
            symbol=intent.symbol,
            side=intent.side,
            target_notional_krw=intent.target_notional_krw,
            limits=self._limits,
            ledger=inputs.ledger,
            market=inputs.market,
        )
        if not decision.allowed:
            logger.info(
                "scalping entry blocked by final risk gate symbol=%s reasons=%s",
                intent.symbol,
                decision.reason_codes,
            )
            return RoundTripResult(
                correlation_id=cid,
                status="blocked",
                reason_codes=decision.reason_codes,
            )
        return None

    async def _monitor(self, intent: OrderIntent) -> str:
        assert intent.tp_price is not None and intent.sl_price is not None
        start = self._clock()
        for _ in range(self._config.max_poll_count):
            elapsed = self._clock() - start
            q = self._broker.quote(intent.symbol)
            reason = decide_exit(
                bid=q.bid if q else None,
                last_price=q.last if q else None,
                tp_price=intent.tp_price,
                sl_price=intent.sl_price,
                elapsed_seconds=elapsed,
                max_hold_seconds=self._config.max_hold_seconds,
            )
            if reason is not None:
                return reason
            if elapsed >= self._config.max_runtime_seconds:
                return TIME_STOP  # failsafe: never leave a position unattended
            await self._sleep(self._config.poll_interval_seconds)
        return TIME_STOP  # poll budget exhausted -> force close

    def _exit_price(self, intent: OrderIntent) -> Decimal:
        """Aggressive marketable-limit sell at the current bid (fallback sl)."""
        q = self._broker.quote(intent.symbol)
        if q is not None and q.bid is not None and q.bid > 0:
            return q.bid
        assert intent.sl_price is not None
        return intent.sl_price

    async def _await_fill(self, submit_result: dict[str, Any]) -> Fill | None:
        for _ in range(self._config.max_fill_polls):
            fill = await self._broker.confirm_fill(submit_result)
            if fill is not None:
                return fill
            await self._sleep(self._config.poll_interval_seconds)
        return None

    def _estimate_fees(self, entry: Fill, exit_: Fill) -> Decimal:
        rate = self._config.round_trip_fee_bps / _BPS
        entry_notional = entry.price * entry.quantity
        exit_notional = exit_.price * exit_.quantity
        return (entry_notional + exit_notional) * rate
