"""Shadow spike-trigger evaluation (records only — Q-109 = A).

Four families, evaluated once per second over the validated tick state:

- ``index_spike``   ±3% vs a reference index level — no read-only index
                    source exists in v0, so it is always ``not_evaluable``.
- ``holding_spike`` ±5% for core holdings / ±7% for other held symbols vs
                    ``market_quote_snapshots.previous_close`` (day window).
- ``vi_proxy``      ±3% within one minute, from the stream's own tick
                    history — the 60-second reference tick is required.
- ``own_fill``      one firing per new ``review.execution_ledger`` row.

``would_kick`` / suppression mirror the #906 gate's arithmetic (daily cap
2, 60-minute cooldown per market) as fields only — this module can never
kick: it has no kick, session, broker, or order callable to invoke.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.timezone import KST

from .types import OwnFill, QuoteTick, TriggerRow

INDEX_THRESHOLD = Decimal("0.03")
HOLDING_CORE_THRESHOLD = Decimal("0.05")
HOLDING_OTHER_THRESHOLD = Decimal("0.07")
VI_THRESHOLD = Decimal("0.03")
VI_WINDOW = timedelta(seconds=60)
HISTORY_KEEP = timedelta(seconds=120)

KICK_DAILY_CAP = 2
KICK_COOLDOWN = timedelta(minutes=60)

TRIGGER_TYPES = ("index_spike", "holding_spike", "vi_proxy", "own_fill")


def _kst_date(ts: datetime) -> str:
    return f"{ts.astimezone(KST):%Y-%m-%d}"


def _epoch_second(ts: datetime) -> int:
    return int(ts.timestamp())


@dataclass
class _GateState:
    count: int = 0
    last_at: datetime | None = None


class ShadowKickGate:
    """Would-be suppression arithmetic: cap 2/day + 60-minute cooldown.

    State lives in-process but re-seeds from today's committed firing
    rows, so a restart continues the same shadow budget instead of
    resetting it.  ``decide`` only computes; nothing here can kick.
    """

    def __init__(self) -> None:
        self._days: dict[tuple[str, str], _GateState] = {}

    def seed(self, market: str, kst_date: str, count: int, last_at) -> None:
        key = (market, kst_date)
        state = self._days.setdefault(key, _GateState())
        state.count = max(state.count, count)
        if last_at is not None and (state.last_at is None or last_at > state.last_at):
            state.last_at = last_at

    def decide(
        self, market: str | None, now: datetime
    ) -> tuple[bool, str | None, int, datetime | None]:
        bucket = market or "other"
        state = self._days.setdefault((bucket, _kst_date(now)), _GateState())
        if state.count >= KICK_DAILY_CAP:
            return False, "daily_cap", state.count, state.last_at
        if state.last_at is not None and now - state.last_at < KICK_COOLDOWN:
            return False, "cooldown", state.count, state.last_at
        state.count += 1
        state.last_at = now
        return True, None, state.count, state.last_at


@dataclass(frozen=True)
class HoldingsView:
    """Held symbols → market, and the core (long-term) subset."""

    held: dict[str, str]  # symbol -> 'kr'|'us'|'crypto'
    core: frozenset[str]


class TriggerEvaluator:
    """Per-second spike evaluator with edge (re-arm) semantics."""

    def __init__(self) -> None:
        self._history: dict[str, deque[tuple[datetime, Decimal]]] = {}
        self._in_breach: set[tuple[str, str, str]] = set()
        self._emitted_ne: set[tuple[str, str, str]] = set()
        self.gate = ShadowKickGate()

    # ------------------------------------------------------------------
    def _fire(
        self,
        *,
        trigger: str,
        symbol: str,
        source_symbol: str | None,
        market: str | None,
        session: str | None,
        reference: Decimal | None,
        current: Decimal | None,
        window: str,
        event_ts: datetime,
        source_ref: str | None,
        dedupe: str,
        detail: dict | None = None,
    ) -> TriggerRow:
        would_kick, suppress, daily_count, last_at = self.gate.decide(market, event_ts)
        return TriggerRow(
            dedupe_key=dedupe,
            trigger_type=trigger,
            outcome="fired",
            symbol=symbol,
            source_symbol=source_symbol,
            market=market,
            session=session,
            reference_price=reference,
            current_price=current,
            window=window,
            event_ts=event_ts,
            kst_date=_kst_date(event_ts),
            would_kick=would_kick,
            suppress_reason=suppress,
            daily_would_kick_count=daily_count,
            last_would_kick_at=last_at,
            not_evaluable_reason=None,
            source_ref=source_ref,
            detail=detail or {},
        )

    def _not_evaluable(
        self,
        *,
        trigger: str,
        symbol: str,
        market: str | None,
        reason: str,
        event_ts: datetime,
    ) -> TriggerRow | None:
        kst = _kst_date(event_ts)
        mark = (trigger, symbol, f"{reason}:{kst}")
        if mark in self._emitted_ne:
            return None
        self._emitted_ne.add(mark)
        return TriggerRow(
            dedupe_key=f"ne:{trigger}:{symbol}:{reason}:{kst}",
            trigger_type=trigger,
            outcome="not_evaluable",
            symbol=symbol,
            source_symbol=None,
            market=market,
            session=None,
            reference_price=None,
            current_price=None,
            window="none",
            event_ts=event_ts,
            kst_date=kst,
            would_kick=False,
            suppress_reason="not_evaluable",
            daily_would_kick_count=0,
            last_would_kick_at=None,
            not_evaluable_reason=reason,
            source_ref=None,
            detail={},
        )

    # ------------------------------------------------------------------
    def index_status(self, now: datetime) -> TriggerRow | None:
        """The index level has no stream/read-only source in v0."""
        return self._not_evaluable(
            trigger="index_spike",
            symbol="*",
            market=None,
            reason="index_level_unavailable",
            event_ts=now,
        )

    def _track_history(self, tick: QuoteTick) -> deque[tuple[datetime, Decimal]]:
        hist = self._history.setdefault(tick.symbol, deque())
        hist.append((tick.ts, tick.price))  # type: ignore[arg-type]
        cutoff = tick.ts - HISTORY_KEEP
        while hist and hist[0][0] < cutoff:
            hist.popleft()
        return hist

    def _reference_at_60s(
        self, hist: deque[tuple[datetime, Decimal]], now_ts: datetime
    ) -> Decimal | None:
        """Most recent tick price at or before ``now_ts - 60s``."""
        boundary = now_ts - VI_WINDOW
        ref: Decimal | None = None
        for ts, price in hist:
            if ts <= boundary:
                ref = price
            else:
                break
        return ref

    def evaluate_tick(
        self,
        tick: QuoteTick,
        holdings: HoldingsView,
        prev_close: dict[str, Decimal | None],
    ) -> list[TriggerRow]:
        """Evaluate holding/VI triggers for one trade tick.

        Orderbook ticks carry no trade price; they update nothing here —
        empty per-tick-type fields are handled by the parser, not guessed.
        """
        if tick.kind != "trade" or tick.price is None:
            return []
        rows: list[TriggerRow] = []
        hist = self._track_history(tick)
        market = tick.market

        # holding_spike -------------------------------------------------
        held_market = holdings.held.get(tick.symbol)
        if held_market is not None:
            ref_close = prev_close.get(tick.symbol)
            if ref_close is None or ref_close <= 0:
                row = self._not_evaluable(
                    trigger="holding_spike",
                    symbol=tick.symbol,
                    market=held_market,
                    reason="previous_close_unavailable",
                    event_ts=tick.ts,
                )
                if row is not None:
                    rows.append(row)
            else:
                threshold = (
                    HOLDING_CORE_THRESHOLD
                    if tick.symbol in holdings.core
                    else HOLDING_OTHER_THRESHOLD
                )
                change = (tick.price - ref_close) / ref_close
                key = ("holding_spike", tick.symbol, _kst_date(tick.ts))
                if abs(change) >= threshold:
                    if key not in self._in_breach:
                        self._in_breach.add(key)
                        rows.append(
                            self._fire(
                                trigger="holding_spike",
                                symbol=tick.symbol,
                                source_symbol=tick.source_symbol,
                                market=held_market,
                                session=tick.session,
                                reference=ref_close,
                                current=tick.price,
                                window="day",
                                event_ts=tick.ts,
                                source_ref=tick.entry_id,
                                dedupe=(
                                    f"spike:holding_spike:{tick.symbol}:"
                                    f"{_kst_date(tick.ts)}:{_epoch_second(tick.ts)}"
                                ),
                                detail={
                                    "threshold": str(threshold),
                                    "change_pct": str(change),
                                    "is_core": tick.symbol in holdings.core,
                                },
                            )
                        )
                else:
                    self._in_breach.discard(key)

        # vi_proxy ------------------------------------------------------
        # VI is a KRX mechanism; US sessions have no volatility halt to
        # proxy, so only KR ticks are candidates.
        ref_60s = self._reference_at_60s(hist, tick.ts) if market == "kr" else None
        if ref_60s is not None and ref_60s > 0:
            change = (tick.price - ref_60s) / ref_60s
            key = ("vi_proxy", tick.symbol, _kst_date(tick.ts))
            if abs(change) >= VI_THRESHOLD:
                if key not in self._in_breach:
                    self._in_breach.add(key)
                    rows.append(
                        self._fire(
                            trigger="vi_proxy",
                            symbol=tick.symbol,
                            source_symbol=tick.source_symbol,
                            market=market,
                            session=tick.session,
                            reference=ref_60s,
                            current=tick.price,
                            window="60s",
                            event_ts=tick.ts,
                            source_ref=tick.entry_id,
                            dedupe=(
                                f"spike:vi_proxy:{tick.symbol}:"
                                f"{_kst_date(tick.ts)}:{_epoch_second(tick.ts)}"
                            ),
                            detail={"change_pct": str(change)},
                        )
                    )
            else:
                self._in_breach.discard(key)
        return rows

    def evaluate_own_fills(self, fills: list[OwnFill]) -> list[TriggerRow]:
        """One firing per new execution-ledger row (id watermark upstream)."""
        rows: list[TriggerRow] = []
        for fill in fills:
            rows.append(
                self._fire(
                    trigger="own_fill",
                    symbol=fill.symbol,
                    source_symbol=None,
                    market=fill.market,
                    session=None,
                    reference=None,
                    current=fill.price,
                    window="fill",
                    event_ts=fill.filled_at,
                    source_ref=str(fill.ledger_id),
                    dedupe=f"ownfill:{fill.ledger_id}",
                    detail={
                        "side": fill.side,
                        "qty": str(fill.qty),
                        "broker": fill.broker,
                    },
                )
            )
        return rows


__all__ = [
    "HOLDING_CORE_THRESHOLD",
    "HOLDING_OTHER_THRESHOLD",
    "INDEX_THRESHOLD",
    "KICK_COOLDOWN",
    "KICK_DAILY_CAP",
    "TRIGGER_TYPES",
    "VI_THRESHOLD",
    "VI_WINDOW",
    "HoldingsView",
    "ShadowKickGate",
    "TriggerEvaluator",
]
