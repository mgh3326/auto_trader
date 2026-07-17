"""ROB-942 (H2, ROB-940) — deterministic 1m execution engine (pure, stdlib).

Signal MATH (Donchian-15m / confirmed-shock-reversal-5m, ROB-943/H3) and
account-global multi-symbol arbitration are OUT of scope here — this module
owns only: next-bar-open fill, bar-ordering (gap-exit-first then intrabar,
same-bar SL-first), single-position-per-symbol sequencing (cooldown/timeout),
UTC-day entry cap and stop-out/loss halts, and cost/funding accounting. See
``rob940_bars_agg`` for 5m/15m aggregation and ``rob940_cost_model`` for the
fee/all-in/funding primitives this module composes. ``run_symbol_stream``
processes ONE symbol's bars/signals per call — H4 (walk-forward runner, not
built here) is expected to call it once per symbol; no cross-symbol state
exists anywhere in this module (AC4).

ultrathink decisions (frozen; see the ROB-942 completion report for the full
write-up):

  * ``SignalEvent.signal_ts`` is defined as the CLOSE boundary of the signal
    bar. For a contiguous 1m grid that is numerically identical to the ``ts``
    (open time) of the very next 1m bar — one candle's close is the next
    candle's open. The engine's "next contiguous 1m bar" (AC3) is therefore
    the bar found AT ``ts == signal_ts``; if it's missing, there is no entry.
    This keeps the engine fully decoupled from signal-timeframe (5m vs 15m)
    bucket size: it never needs to know which timeframe produced a signal.
  * TP may be given either as a distance in bps from entry (Strategy 1, known
    at signal time) or as an absolute target price (Strategy 2, whose
    distance is only knowable once E is resolved: ``d_TP=|T/E-1|``). Exactly
    one of ``tp_distance_bps``/``tp_target_price`` must be set; this engine
    resolves the actual distance against E and applies the shared >=68bp gate
    (AC7). Strategy-specific EXTRA TP bounds (e.g. Strategy 2's <=1.20%/
    >=R_min*d_SL) are NOT enforced here — those belong to the signal layer
    (H3/H4), which owns the strategy-specific formulas, not the shared
    execution-layer cost gate.
  * A gap-through gets the unfavorable "fill at open" price ONLY for a stop
    loss; a favorable gap through TP fills at the TP barrier (no windfall) —
    symmetric with "normal touch = barrier price" for every other case. This
    generalizes the repo's existing conservative SL-first convention
    (rob382_backtest.py) to the gap-open case.
  * ``exit_reason`` has exactly 3 values (take_profit/stop_loss/timeout) so
    consecutive-stop-out accounting (AC8) has one unambiguous check; a
    separate ``gap_fill`` bool preserves the gap-vs-touch fill-mechanism
    distinction for audit without forking that logic into a 4th reason.
  * UTC-day bucketing for the entry cap / stop-out halt / -2.0R halt (AC8)
    keys off the RESOLVED ``entry_ts``, not ``signal_ts``, so a signal firing
    near a UTC midnight boundary is attributed to the day its capital is
    actually at risk.
  * Cooldown and "position still open" are the SAME check: the candidate
    entry's bar index must be >= ``last_exit_idx + cooldown_bars``. Since
    ``last_exit_idx`` is only known once a position closes, a signal whose
    candidate entry falls inside the PRIOR trade's still-open hold also fails
    this check (entry_idx <= exit_idx < exit_idx + cooldown_bars) — no
    separate "position already open" reason is needed (AC4 single-position
    stream + AC8 cooldown collapse into one gate).
  * Consecutive-stop-out counting resets on ANY non-stop_loss exit (take
    profit OR timeout) — only back-to-back SL exits count as a "stop-out
    streak"; a timeout is not a stop-out.
  * ``fold_id`` is a pass-through field for H4 (walk-forward runner); this
    engine does not compute or validate fold boundaries.

ROB-942 R1 correction (cost-scenario path divergence, 2026-07-17): each call
to ``run_symbol_stream`` is an INDEPENDENT simulation over its own fresh
``_DayState`` — there is no shared/cached path across scenarios. Passing a
different ``cost_scenario`` does not just change ``net_bps`` after the fact;
``state.daily_r`` (fed by that scenario's own ``net_bps``) is part of the
loop's own control flow (AC8's ``<=-2.0R`` halt), so a higher-cost scenario
CAN cross the halt threshold sooner than a lower-cost one on the exact same
``bars_1m``/``signals`` input, producing a different trade COUNT (a shorter
path) for that invocation only. Signal eligibility itself does not diverge:
``MIN_TP_DISTANCE_BPS`` (68bp) is one fixed value independent of
``cost_scenario``, so every scenario sees the same set of candidate entries
before any cost-driven halt can prune them. Callers (H4/H5) MUST treat the
three cost-scenario ledgers as three separate runs to compare, never as a
single reference path with net-only revaluation. See
``rob940_cost_model`` module docstring and the
``test_68bp_gate_is_identical_across_all_cost_scenarios`` /
``test_cost_scenario_dependent_daily_stop_diverges_trade_count`` regressions
in ``tests/test_rob940_engine.py``.

Caller preconditions (H4, not enforced here — documented per R1 M2/M3):
  * ``signals`` for a given symbol SHOULD NOT contain duplicate
    ``signal_ts`` values. The cooldown/position gate
    (``entry_idx < earliest_allowed_entry_idx``) assumes at most one signal
    per bar per symbol; two same-``signal_ts`` signals combined with an
    immediate same-bar exit and ``cooldown_bars=0`` could otherwise slip a
    second entry into what should be a single-position stream.
  * ``sorted(signals, key=lambda s: s.signal_ts)`` (Python's stable sort) is
    the engine's ONLY tie-break for same-``signal_ts`` signals — it resolves
    ties by INPUT ORDER, not by any semantic priority. AC1's "same
    bars/config -> same bytes" determinism claim holds only if H4 presents
    ``signals`` in a canonical, reproducible order.

No DB/network/app/broker/order/fill/scheduler imports — pure stdlib plus the
existing research_contracts canonical-hash authority (itself stdlib-only),
deterministic given its input.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from rob940_bars_agg import Bar1m
from rob940_cost_model import FEE_ROUND_TRIP_BPS as _FEE_ROUND_TRIP_BPS
from rob940_cost_model import (
    MIN_TP_DISTANCE_BPS,
    CostScenario,
    FundingCrossing,
    Side,
    gross_bps,
    net_bps,
    realized_funding_bps,
)

from research_contracts.canonical_hash import canonical_sha256

_MS_PER_MINUTE = 60_000

# AC8: UTC-day 3-entry cap; consecutive 2 stop-outs OR cost-included daily
# <=-2.0R halts further entries for the rest of that UTC day.
DAILY_MAX_ENTRIES = 3
DAILY_MAX_CONSECUTIVE_STOP_OUTS = 2
DAILY_MAX_LOSS_R = -2.0

ExitReason = str  # "take_profit" | "stop_loss" | "timeout"
NoTradeReason = str


@dataclass(frozen=True)
class SignalEvent:
    strategy: str
    config_id: str
    symbol: str
    signal_ts: int  # close boundary of the signal bar == required next 1m bar's ts
    side: Side
    sl_distance_bps: float  # > 0, resolved at signal time, relative to E
    tp_distance_bps: float | None = None  # relative to E; mutually excl. w/ target
    tp_target_price: float | None = None  # absolute; distance resolved vs E
    timeout_bars: int = 1  # count of 1m bars from entry (inclusive deadline index)
    cooldown_bars: int = 0  # 1m bars after exit before the next entry is allowed
    fold_id: str | None = None

    def __post_init__(self) -> None:
        if self.side not in ("long", "short"):
            raise ValueError(f"unknown side {self.side!r}")
        # ROB-942 R1 M1: `nan <= 0` is False, so a bare `<= 0` check silently
        # let NaN/+-Inf through; check finiteness explicitly and first.
        if not math.isfinite(self.sl_distance_bps):
            raise ValueError(
                f"sl_distance_bps must be finite, got {self.sl_distance_bps!r}"
            )
        if self.sl_distance_bps <= 0:
            raise ValueError("sl_distance_bps must be positive")
        has_bps = self.tp_distance_bps is not None
        has_target = self.tp_target_price is not None
        if has_bps == has_target:  # both set or neither set
            raise ValueError(
                "exactly one of tp_distance_bps/tp_target_price must be set"
            )
        if has_bps and not math.isfinite(self.tp_distance_bps):
            raise ValueError(
                f"tp_distance_bps must be finite, got {self.tp_distance_bps!r}"
            )
        if has_bps and self.tp_distance_bps <= 0:
            raise ValueError("tp_distance_bps must be positive")
        if has_target and not math.isfinite(self.tp_target_price):
            raise ValueError(
                f"tp_target_price must be finite, got {self.tp_target_price!r}"
            )
        if has_target and self.tp_target_price <= 0:
            raise ValueError("tp_target_price must be positive")
        if self.timeout_bars < 1:
            raise ValueError("timeout_bars must be >= 1")
        if self.cooldown_bars < 0:
            raise ValueError("cooldown_bars must be >= 0")


@dataclass(frozen=True)
class TradeRecord:
    strategy: str
    config_id: str
    symbol: str
    side: Side
    signal_ts: int
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    exit_reason: ExitReason
    gross_bps: float
    fee_bps: float
    all_in_bps: float
    funding_bps: float
    net_bps: float
    fold_id: str | None
    gap_fill: bool = False


@dataclass(frozen=True)
class NoTradeRecord:
    strategy: str
    config_id: str
    symbol: str
    side: Side
    signal_ts: int
    reason: NoTradeReason
    fold_id: str | None = None


@dataclass(frozen=True)
class EngineResult:
    trades: tuple[TradeRecord, ...]
    no_trades: tuple[NoTradeRecord, ...]


def _validate_sorted_1m(bars_1m: Sequence[Bar1m]) -> None:
    for i in range(1, len(bars_1m)):
        if bars_1m[i].ts <= bars_1m[i - 1].ts:
            raise ValueError(
                "bars_1m must be strictly increasing by ts; got "
                f"{bars_1m[i - 1].ts} then {bars_1m[i].ts}"
            )


def _bar_index_by_ts(bars_1m: Sequence[Bar1m]) -> dict[int, int]:
    return {b.ts: i for i, b in enumerate(bars_1m)}


def _utc_date(ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC).date()


def resolve_entry(
    bars_1m: Sequence[Bar1m], signal_ts: int, index: dict[int, int] | None = None
) -> int | None:
    """Index of the 1m bar at ``ts == signal_ts``, or ``None`` if absent (AC3/AC10).

    ``signal_ts`` is the CLOSE boundary of the signal bar, which for a
    contiguous 1m grid equals the open time of the very next 1m bar — see the
    module docstring's ultrathink note. A missing bar there (a gap right at
    the execution boundary) is a hard no-entry; the engine never searches
    further ahead (that would be a different, unspecified "delayed entry"
    policy, not next-bar-open).
    """
    idx = index if index is not None else _bar_index_by_ts(bars_1m)
    return idx.get(signal_ts)


def _gapped_through_sl(side: Side, open_price: float, sl_price: float) -> bool:
    return open_price <= sl_price if side == "long" else open_price >= sl_price


def _gapped_through_tp(side: Side, open_price: float, tp_price: float) -> bool:
    return open_price >= tp_price if side == "long" else open_price <= tp_price


def _touched_sl(side: Side, bar: Bar1m, sl_price: float) -> bool:
    return bar.low <= sl_price if side == "long" else bar.high >= sl_price


def _touched_tp(side: Side, bar: Bar1m, tp_price: float) -> bool:
    return bar.high >= tp_price if side == "long" else bar.low <= tp_price


def _walk_exit(
    bars_1m: Sequence[Bar1m],
    entry_idx: int,
    side: Side,
    sl_price: float,
    tp_price: float,
    timeout_bars: int,
) -> tuple[int, float, ExitReason, bool]:
    """Return ``(exit_idx, exit_price, exit_reason, gap_fill)``.

    Lookahead-free by construction: walks bars strictly in chronological order
    starting at ``entry_idx`` and returns on the FIRST bar satisfying any exit
    condition (AC5). Every bar (including the entry bar itself, whose ``open``
    trivially never gaps past a barrier defined relative to that same open)
    checks gap-open exit before intrabar touch; same-bar SL+TP is SL-first,
    side-agnostic. The deadline bar (first bar at ``entry_idx + timeout_bars``)
    still checks gap-open exit first, then falls back to a timeout fill at its
    own open — it never inspects its own intrabar range (AC5: timeout is a
    market order sent at that bar's open, before its range develops).
    """
    deadline_idx = entry_idx + timeout_bars
    n = len(bars_1m)
    last_idx = min(deadline_idx, n - 1)
    for j in range(entry_idx, last_idx + 1):
        bar = bars_1m[j]
        gap_sl = _gapped_through_sl(side, bar.open, sl_price)
        gap_tp = _gapped_through_tp(side, bar.open, tp_price)
        if gap_sl:
            return j, bar.open, "stop_loss", True
        if j == deadline_idx:
            if gap_tp:
                return j, tp_price, "take_profit", False
            return j, bar.open, "timeout", False
        if gap_tp:
            return j, tp_price, "take_profit", False
        if _touched_sl(side, bar, sl_price):
            return j, sl_price, "stop_loss", False
        if _touched_tp(side, bar, tp_price):
            return j, tp_price, "take_profit", False
    # ran off the end of available data before reaching SL/TP/the deadline bar
    j = n - 1
    return j, bars_1m[j].close, "timeout", False


@dataclass
class _DayState:
    entries: int = 0
    consecutive_stop_outs: int = 0
    daily_r: float = 0.0
    halted: bool = False


def _no_trade(sig: SignalEvent, reason: NoTradeReason) -> NoTradeRecord:
    return NoTradeRecord(
        strategy=sig.strategy,
        config_id=sig.config_id,
        symbol=sig.symbol,
        side=sig.side,
        signal_ts=sig.signal_ts,
        reason=reason,
        fold_id=sig.fold_id,
    )


def run_symbol_stream(
    bars_1m: Sequence[Bar1m],
    signals: Sequence[SignalEvent],
    cost_scenario: CostScenario,
    *,
    funding_lookup=None,
) -> EngineResult:
    """Walk ``signals`` (sorted by ``signal_ts``) against ONE symbol's 1m bars.

    ``funding_lookup``, if given, is ``(symbol, side, entry_ts, exit_ts) ->
    Sequence[FundingCrossing]`` — the explicit pure input interface for H1's
    PIT-safe funding sidecar (AC6). Realized crossings are summed and
    subtracted from ``net_bps`` exactly once; omitting it is equivalent to
    zero funding crossings.
    """
    _validate_sorted_1m(bars_1m)
    index = _bar_index_by_ts(bars_1m)
    trades: list[TradeRecord] = []
    no_trades: list[NoTradeRecord] = []
    day_states: dict[date, _DayState] = {}
    earliest_allowed_entry_idx = 0

    for sig in sorted(signals, key=lambda s: s.signal_ts):
        entry_idx = resolve_entry(bars_1m, sig.signal_ts, index)
        if entry_idx is None:
            no_trades.append(_no_trade(sig, "next_bar_unavailable"))
            continue

        entry_bar = bars_1m[entry_idx]
        entry_price = entry_bar.open
        day = _utc_date(entry_bar.ts)
        state = day_states.setdefault(day, _DayState())

        if state.halted:
            no_trades.append(_no_trade(sig, "daily_stop_active"))
            continue
        if state.entries >= DAILY_MAX_ENTRIES:
            no_trades.append(_no_trade(sig, "daily_entry_cap"))
            continue
        if entry_idx < earliest_allowed_entry_idx:
            no_trades.append(_no_trade(sig, "cooldown_active"))
            continue

        sl_price = (
            entry_price * (1.0 - sig.sl_distance_bps / 1e4)
            if sig.side == "long"
            else entry_price * (1.0 + sig.sl_distance_bps / 1e4)
        )
        if sig.tp_distance_bps is not None:
            tp_distance_bps = sig.tp_distance_bps
            tp_price = (
                entry_price * (1.0 + tp_distance_bps / 1e4)
                if sig.side == "long"
                else entry_price * (1.0 - tp_distance_bps / 1e4)
            )
        else:
            tp_price = sig.tp_target_price
            tp_distance_bps = abs(tp_price / entry_price - 1.0) * 1e4

        if tp_distance_bps < MIN_TP_DISTANCE_BPS:
            no_trades.append(_no_trade(sig, "tp_below_min_distance"))
            continue

        exit_idx, exit_price, exit_reason, gap_fill = _walk_exit(
            bars_1m, entry_idx, sig.side, sl_price, tp_price, sig.timeout_bars
        )
        exit_bar = bars_1m[exit_idx]
        gross = gross_bps(sig.side, entry_price, exit_price)
        crossings: Sequence[FundingCrossing] = (
            funding_lookup(sig.symbol, sig.side, entry_bar.ts, exit_bar.ts)
            if funding_lookup is not None
            else ()
        )
        funding = realized_funding_bps(sig.side, crossings)
        net = net_bps(gross, cost_scenario, funding)

        trades.append(
            TradeRecord(
                strategy=sig.strategy,
                config_id=sig.config_id,
                symbol=sig.symbol,
                side=sig.side,
                signal_ts=sig.signal_ts,
                entry_ts=entry_bar.ts,
                entry_price=entry_price,
                exit_ts=exit_bar.ts,
                exit_price=exit_price,
                exit_reason=exit_reason,
                gross_bps=gross,
                fee_bps=_FEE_ROUND_TRIP_BPS,
                all_in_bps=cost_scenario.all_in_bps,
                funding_bps=funding,
                net_bps=net,
                fold_id=sig.fold_id,
                gap_fill=gap_fill,
            )
        )

        state.entries += 1
        state.consecutive_stop_outs = (
            state.consecutive_stop_outs + 1 if exit_reason == "stop_loss" else 0
        )
        state.daily_r += net / sig.sl_distance_bps
        if (
            state.consecutive_stop_outs >= DAILY_MAX_CONSECUTIVE_STOP_OUTS
            or state.daily_r <= DAILY_MAX_LOSS_R
        ):
            state.halted = True

        earliest_allowed_entry_idx = exit_idx + sig.cooldown_bars

    return EngineResult(trades=tuple(trades), no_trades=tuple(no_trades))


def ledger_hash(trades: Sequence[TradeRecord]) -> str:
    """AC1: byte-identical hash of an ORDERED trade ledger for the same input.

    Delegates to the repo's existing typed-canonical SHA-256 authority
    (``research_contracts.canonical_hash``), so float fields hash via their
    exact hex representation (no JSON-numeric round-trip drift) and list order
    is preserved (the ledger's ordering is part of its identity).
    """
    payload = [
        {
            "strategy": t.strategy,
            "config_id": t.config_id,
            "symbol": t.symbol,
            "side": t.side,
            "signal_ts": t.signal_ts,
            "entry_ts": t.entry_ts,
            "entry_price": t.entry_price,
            "exit_ts": t.exit_ts,
            "exit_price": t.exit_price,
            "exit_reason": t.exit_reason,
            "gross_bps": t.gross_bps,
            "fee_bps": t.fee_bps,
            "all_in_bps": t.all_in_bps,
            "funding_bps": t.funding_bps,
            "net_bps": t.net_bps,
            "fold_id": t.fold_id,
            "gap_fill": t.gap_fill,
        }
        for t in trades
    ]
    return canonical_sha256(payload)
