"""ROB-945 (H5) -- PBO daily-grid BUILDER seam.

Fable Q2=A (orch-fable-answer-rob945b-20260718.md) requires the 12-config
PBO grid to come from 12 INDEPENDENT full-window @17 evaluations of the
frozen 4-symbol universe, never derived from H4's selected-only
walk-forward output, never a rolling concatenation, and never a linear
revaluation of one config's numbers.

Captain correction (2026-07-18): the callback must NOT hand back a
pre-aggregated day map (self-attested evidence) -- it returns raw,
independent per-symbol trade streams (H4's own ``TradeRecord`` type,
reused, never re-implemented), and THIS module performs the actual
canonical-sort + UTC-exit-day aggregation + zero-fill itself, so the
aggregation logic is this module's own, auditable responsibility rather
than something a callback could fake.

Every request/response pair is fully identity- and provenance-checked
(exact strategy/config, exact frozen scenario/cost/window, exact frozen
4-symbol coverage) and every trade's own identity is re-validated before
it is trusted. A misbehaving callback that reuses/mutates one shared
buffer across calls, or returns the SAME symbol data for every config
(a "selected-only"/linear-revalue style bug), is caught because this
builder aggregates fresh from each response's own trades -- it never
carries state between the 12 independent calls.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import rob941_frozen_scope as frozen
from rob940_engine import TradeRecord
from rob945_pbo_grid import (
    EXPECTED_CONFIG_COUNT,
    FROZEN_DAY_KEYS,
    FROZEN_PBO_COST_BPS,
    FROZEN_PBO_SCENARIO_NAME,
    FROZEN_PBO_WINDOW_END_ISO,
    FROZEN_PBO_WINDOW_START_ISO,
    FROZEN_STRATEGIES,
)

_FROZEN_SYMBOLS: tuple[str, ...] = frozen.UNIVERSE
_CLOSED_SIDES = frozenset({"long", "short"})
_CLOSED_EXIT_REASONS = frozenset({"take_profit", "stop_loss", "timeout"})


class Rob945PboBuilderError(ValueError):
    """A request/response pair failed identity/provenance validation, or a
    trade's own identity was forged -- always fail-closed."""


@dataclass(frozen=True)
class PboEvaluationRequest:
    """Immutable request the builder issues to the injected callback --
    the callback cannot silently substitute a different scenario/cost/
    window/symbol set, since the builder re-validates every field of
    whatever comes back against exactly this request."""

    strategy: str
    config_id: str
    scenario_name: str
    cost_bps: float
    window_start_iso: str
    window_end_iso: str
    symbols: tuple[str, ...]


_CLOSED_SYMBOL_OUTCOME_STATUSES = frozenset({"completed", "crashed", "gap_invalid"})


@dataclass(frozen=True)
class SymbolOutcome:
    """One symbol's independent evaluation outcome for one config: its own
    raw trade stream (never pre-aggregated) plus any UTC days this symbol's
    own stream reports as gap-invalid. ``status`` is a closed terminal
    label (mirrors H4/H6's own completed/crashed/rejected discipline) --
    a crashed or otherwise non-``completed`` symbol stream can never be
    silently relabeled complete just because it happens to carry zero
    trades."""

    symbol: str
    status: str
    trades: tuple[TradeRecord, ...]
    gap_invalid_days: frozenset[str]


@dataclass(frozen=True)
class ConfigEvaluationResponse:
    strategy: str
    config_id: str
    scenario_name: str
    cost_bps: float
    window_start_iso: str
    window_end_iso: str
    symbol_outcomes: tuple[SymbolOutcome, ...]


EvaluateConfigCallback = Callable[[PboEvaluationRequest], ConfigEvaluationResponse]


def _canonical_trade_sort_key(
    trade: TradeRecord,
) -> tuple[int, str, str, str | None, int]:
    return (
        trade.entry_ts,
        trade.symbol,
        trade.config_id,
        trade.fold_id,
        trade.signal_ts,
    )


def _exit_day_key(exit_ts_ms: int) -> str:
    return datetime.fromtimestamp(exit_ts_ms / 1000, tz=UTC).date().isoformat()


def _validate_trade(
    trade: TradeRecord, *, strategy: str, config_id: str, symbol: str
) -> None:
    if not isinstance(trade, TradeRecord):
        raise Rob945PboBuilderError(
            f"pbo_builder_invalid_trade_type: {config_id!r}/{symbol!r} entry is not a "
            f"TradeRecord, got {type(trade).__name__!r}"
        )
    if (
        trade.strategy != strategy
        or trade.config_id != config_id
        or trade.symbol != symbol
    ):
        raise Rob945PboBuilderError(
            f"pbo_builder_forged_trade_identity: requested (strategy={strategy!r}, "
            f"config_id={config_id!r}, symbol={symbol!r}) but trade claims "
            f"(strategy={trade.strategy!r}, config_id={trade.config_id!r}, "
            f"symbol={trade.symbol!r})"
        )
    if trade.fold_id is not None:
        raise Rob945PboBuilderError(
            f"pbo_builder_fold_scoped_trade_forbidden: {config_id!r}/{symbol!r} trade "
            f"carries fold_id={trade.fold_id!r} -- PBO auxiliary evidence is a "
            "full-window evaluation and must carry the dedicated no-fold sentinel "
            "(fold_id=None); a fold-00..07-scoped trade would mean this is a "
            "rolling/selected-OOS ledger, not an independent full-window evaluation"
        )
    for field_name in ("gross_bps", "fee_bps", "all_in_bps", "funding_bps", "net_bps"):
        value = getattr(trade, field_name)
        if not math.isfinite(value):
            raise Rob945PboBuilderError(
                f"pbo_builder_non_finite_trade_field: {config_id!r}/{symbol!r}."
                f"{field_name} is non-finite ({value!r})"
            )
    for price_field in ("entry_price", "exit_price"):
        price = getattr(trade, price_field)
        if not math.isfinite(price) or price <= 0:
            raise Rob945PboBuilderError(
                f"pbo_builder_invalid_price: {config_id!r}/{symbol!r}.{price_field} must "
                f"be finite and positive, got {price!r}"
            )
    for ts_field in ("signal_ts", "entry_ts", "exit_ts"):
        ts_value = getattr(trade, ts_field)
        if isinstance(ts_value, bool) or not isinstance(ts_value, int):
            raise Rob945PboBuilderError(
                f"pbo_builder_invalid_timestamp: {config_id!r}/{symbol!r}.{ts_field} must "
                f"be a plain int, got {ts_value!r}"
            )
    if not (trade.signal_ts <= trade.entry_ts <= trade.exit_ts):
        raise Rob945PboBuilderError(
            f"pbo_builder_out_of_order_timestamps: {config_id!r}/{symbol!r} must satisfy "
            f"signal_ts <= entry_ts <= exit_ts, got signal_ts={trade.signal_ts!r}, "
            f"entry_ts={trade.entry_ts!r}, exit_ts={trade.exit_ts!r}"
        )
    if trade.side not in _CLOSED_SIDES:
        raise Rob945PboBuilderError(
            f"pbo_builder_invalid_side: {config_id!r}/{symbol!r}.side {trade.side!r} "
            f"outside the closed set {sorted(_CLOSED_SIDES)!r}"
        )
    if trade.exit_reason not in _CLOSED_EXIT_REASONS:
        raise Rob945PboBuilderError(
            f"pbo_builder_invalid_exit_reason: {config_id!r}/{symbol!r}.exit_reason "
            f"{trade.exit_reason!r} outside the closed set "
            f"{sorted(_CLOSED_EXIT_REASONS)!r}"
        )


def _aggregate_symbol_outcome(
    outcome: SymbolOutcome,
    *,
    strategy: str,
    config_id: str,
    day_totals: dict[str, float],
) -> None:
    if outcome.status != "completed":
        raise Rob945PboBuilderError(
            f"pbo_builder_symbol_not_completed: {config_id!r}/{outcome.symbol!r} status "
            f"is {outcome.status!r}, not 'completed' -- a crashed/partial stream can "
            "never be silently treated as usable auxiliary evidence"
        )
    seen_identity: set[tuple[str, str, str, int]] = set()
    for trade in sorted(outcome.trades, key=_canonical_trade_sort_key):
        _validate_trade(
            trade, strategy=strategy, config_id=config_id, symbol=outcome.symbol
        )
        identity = (trade.strategy, trade.config_id, trade.symbol, trade.signal_ts)
        if identity in seen_identity:
            raise Rob945PboBuilderError(
                f"pbo_builder_duplicate_trade_identity: {config_id!r}/{outcome.symbol!r} "
                f"has a duplicate trade identity {identity!r}"
            )
        seen_identity.add(identity)
        day_key = _exit_day_key(trade.exit_ts)
        if day_key not in day_totals:
            raise Rob945PboBuilderError(
                f"pbo_builder_exit_day_out_of_window: {config_id!r}/{outcome.symbol!r} "
                f"trade exit_ts={trade.exit_ts!r} maps to day {day_key!r}, outside the "
                "frozen 365-day window"
            )
        day_totals[day_key] += trade.net_bps


def build_pbo_daily_grid(
    *, strategy: str, evaluate_config: EvaluateConfigCallback
) -> tuple[dict[str, dict[str, float]], dict[str, frozenset[str]]]:
    """Issue exactly 12 canonical, fully identity/provenance-checked
    requests (one per frozen ``{strategy}-00``..``{strategy}-11`` config),
    canonically sort and aggregate each response's 4 independent per-symbol
    trade streams by UTC exit day (zero-fill for no-trade days), and return
    ``(daily_net_bps_by_config, gap_invalid_days_by_config)`` ready for
    ``rob945_pbo_grid.compute_pbo_auxiliary_evidence``.
    """
    if strategy not in FROZEN_STRATEGIES:
        raise Rob945PboBuilderError(
            f"pbo_builder_strategy: expected strategy in {FROZEN_STRATEGIES!r}, got "
            f"{strategy!r}"
        )
    grid: dict[str, dict[str, float]] = {}
    gaps: dict[str, frozenset[str]] = {}
    for i in range(EXPECTED_CONFIG_COUNT):
        config_id = f"{strategy}-{i:02d}"
        request = PboEvaluationRequest(
            strategy=strategy,
            config_id=config_id,
            scenario_name=FROZEN_PBO_SCENARIO_NAME,
            cost_bps=FROZEN_PBO_COST_BPS,
            window_start_iso=FROZEN_PBO_WINDOW_START_ISO,
            window_end_iso=FROZEN_PBO_WINDOW_END_ISO,
            symbols=_FROZEN_SYMBOLS,
        )
        response = evaluate_config(request)

        if (
            response.strategy != request.strategy
            or response.config_id != request.config_id
            or response.scenario_name != request.scenario_name
            or response.cost_bps != request.cost_bps
            or response.window_start_iso != request.window_start_iso
            or response.window_end_iso != request.window_end_iso
        ):
            raise Rob945PboBuilderError(
                f"pbo_builder_response_provenance_mismatch: response for "
                f"{config_id!r} does not echo the exact request it was given"
            )

        response_symbols = tuple(o.symbol for o in response.symbol_outcomes)
        if set(response_symbols) != set(_FROZEN_SYMBOLS) or len(
            response.symbol_outcomes
        ) != len(_FROZEN_SYMBOLS):
            raise Rob945PboBuilderError(
                f"pbo_builder_missing_symbol_coverage: {config_id!r} response covers "
                f"{sorted(response_symbols)!r}, expected exactly "
                f"{sorted(_FROZEN_SYMBOLS)!r}"
            )

        # canonical frozen symbol order -- REGARDLESS of the order the
        # response happened to list them in -- so float summation order
        # (and therefore the resulting bit pattern) is always identical.
        outcomes_by_symbol = {o.symbol: o for o in response.symbol_outcomes}
        day_totals: dict[str, float] = dict.fromkeys(FROZEN_DAY_KEYS, 0.0)
        gap_invalid_days: set[str] = set()
        for symbol in _FROZEN_SYMBOLS:
            outcome = outcomes_by_symbol[symbol]
            _aggregate_symbol_outcome(
                outcome, strategy=strategy, config_id=config_id, day_totals=day_totals
            )
            gap_invalid_days.update(outcome.gap_invalid_days)

        grid[config_id] = day_totals
        gaps[config_id] = frozenset(gap_invalid_days)
    return grid, gaps
