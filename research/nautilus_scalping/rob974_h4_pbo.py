"""ROB-982 CP7 H4 independent full-window PBO grid + evaluator seam.

Each strategy's 24 frozen configs are independently evaluated over the exact
full ``[2025-07-01T00:00Z, 2026-07-01T00:00Z)`` window at fresh
``primary_stress17`` with the dedicated no-fold sentinel (``fold_id=None``)
-- never derived from H4's selected-OOS walk-forward output, never a rolling
concatenation, never a linear revaluation of one config's numbers. This
mirrors the frozen ``rob945_pbo_builder``/``rob945_pbo_grid`` (H1-H3 S1/S2,
12-config, per-symbol) design one level up: S3/S4 are account-global engines,
so aggregation sums ONE global trade stream's ``gross_bps`` per UTC exit day,
not four independent per-symbol streams.

The actual engine call for each config is routed through the CP6 narrow
adapter (``rob974_h4_adapter``) -- this module never re-implements or forks
price/exit/PnL logic, only fan-out bookkeeping and day-bucket aggregation.
Only the frozen ``research_contracts.honest_offline_gate.
probability_backtest_overfitting`` CSCV primitive computes the PBO statistic;
this module never re-implements it and never lets its result influence a
hard gate -- it is reference-only evidence.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rob945_pbo_grid import FROZEN_DAY_KEYS
from rob974_h4_adapter import invoke_actual_s3_engine, invoke_actual_s4_engine
from rob974_h4_contracts import WINDOW_END_MS, WINDOW_START_MS

from research_contracts.canonical_hash import canonical_sha256
from research_contracts.honest_offline_gate import probability_backtest_overfitting

__all__ = [
    "PBO_SLICES",
    "H4PboError",
    "H4PboEvidence",
    "aggregate_full_window_s3_trades",
    "aggregate_full_window_s4_trades",
    "compute_h4_full_window_pbo",
    "run_full_window_s3_configs",
    "run_full_window_s4_configs",
    "seal_full_window_grid",
    "validate_full_window_grid",
]

PBO_SLICES = 4
_DAY_KEY_SET = frozenset(FROZEN_DAY_KEYS)
if len(FROZEN_DAY_KEYS) != 365:
    raise AssertionError(
        "rob945_pbo_grid.FROZEN_DAY_KEYS drifted from the expected 365-day "
        "[2025-07-01, 2026-07-01) window H4 reuses verbatim"
    )
_STRATEGIES: tuple[str, ...] = ("S3", "S4")


class H4PboError(ValueError):
    """Full-window PBO fan-out/grid/evaluator input failed validation.

    Always fail-closed -- never silently padded, zero-filled beyond a
    genuine no-trade day, or fed a caller pre-aggregated map.
    """


@dataclass(frozen=True, slots=True)
class H4PboEvidence:
    """Closed-shape, reference-only PBO evidence. Deliberately carries no
    selection/verdict/gate field -- PBO can never change a hard gate."""

    strategy: str
    value: float | None
    reason_codes: tuple[str, ...]
    slices: int
    config_count: int
    day_count: int
    grid_seal_sha256: str


def _exit_day_key(exit_ts_ms: int) -> str:
    return datetime.fromtimestamp(exit_ts_ms / 1000, tz=UTC).date().isoformat()


def _require_config_roster(strategy: str, configs: Sequence[object]) -> None:
    if strategy not in _STRATEGIES:
        raise H4PboError(
            f"h4_pbo_strategy: expected strategy in {_STRATEGIES!r}, got {strategy!r}"
        )
    config_ids = tuple(getattr(c, "config_id", None) for c in configs)
    expected = tuple(f"{strategy}-{i:02d}" for i in range(24))
    if config_ids != expected:
        raise H4PboError(
            "h4_pbo_config_roster: configs must be the exact ordered 24-row "
            f"canonical {strategy} roster, got {config_ids!r}"
        )


def aggregate_full_window_s3_trades(
    config_id: str, trades: Sequence[object], *, day_totals: dict[str, float]
) -> None:
    """Sum one config's raw, already-adversarially-validated S3 trades into
    ``day_totals`` (a pre-zero-filled 365-day mapping), keyed by UTC exit
    day. Raises on any fold-scoped trade or an exit day outside the frozen
    full window -- both are structurally unreachable through the actual H2
    engine when invoked with ``horizon_end_ts=WINDOW_END_MS`` (mirrors
    ``rob960_pbo_evaluator``'s own defense-in-depth C1/C4 posture), so this
    check exists to fail closed rather than silently accept a forged input.
    """
    seen: set[tuple[str, int]] = set()
    for trade in trades:
        if trade.config_id != config_id:
            raise H4PboError(
                f"h4_pbo_forged_trade_config: expected config_id={config_id!r}, "
                f"trade claims {trade.config_id!r}"
            )
        if trade.fold_id is not None:
            raise H4PboError(
                f"h4_pbo_fold_scoped_trade_forbidden: {config_id!r} trade carries "
                f"fold_id={trade.fold_id!r} -- full-window PBO evaluation requires "
                "the no-fold sentinel (fold_id=None); a fold-scoped trade would "
                "mean this is a rolling/selected-OOS ledger, not an independent "
                "full-window evaluation"
            )
        identity = (trade.symbol, trade.signal_ts)
        if identity in seen:
            raise H4PboError(
                f"h4_pbo_duplicate_trade_identity: {config_id!r} has a duplicate "
                f"trade identity {identity!r}"
            )
        seen.add(identity)
        day_key = _exit_day_key(trade.exit_ts)
        if day_key not in day_totals:
            raise H4PboError(
                f"h4_pbo_exit_day_out_of_window: {config_id!r} trade exit_ts="
                f"{trade.exit_ts!r} maps to day {day_key!r}, outside the frozen "
                "365-day full window"
            )
        day_totals[day_key] += trade.gross_bps


def aggregate_full_window_s4_trades(
    config_id: str, trades: Sequence[object], *, day_totals: dict[str, float]
) -> None:
    seen: set[tuple[tuple[str, str], int]] = set()
    for trade in trades:
        if trade.config_id != config_id:
            raise H4PboError(
                f"h4_pbo_forged_trade_config: expected config_id={config_id!r}, "
                f"trade claims {trade.config_id!r}"
            )
        if trade.fold_id is not None:
            raise H4PboError(
                f"h4_pbo_fold_scoped_trade_forbidden: {config_id!r} trade carries "
                f"fold_id={trade.fold_id!r} -- full-window PBO evaluation requires "
                "the no-fold sentinel (fold_id=None)"
            )
        identity = (trade.pair, trade.signal_ts)
        if identity in seen:
            raise H4PboError(
                f"h4_pbo_duplicate_trade_identity: {config_id!r} has a duplicate "
                f"trade identity {identity!r}"
            )
        seen.add(identity)
        day_key = _exit_day_key(trade.exit_ts)
        if day_key not in day_totals:
            raise H4PboError(
                f"h4_pbo_exit_day_out_of_window: {config_id!r} trade exit_ts="
                f"{trade.exit_ts!r} maps to day {day_key!r}, outside the frozen "
                "365-day full window"
            )
        day_totals[day_key] += trade.gross_bps


def run_full_window_s3_configs(
    *,
    configs: Sequence[object],
    generator: Callable[[object], Sequence[object]],
    minute_index,
    close_feature_index,
) -> dict[str, dict[str, float]]:
    """Exactly one fresh generator call and one actual-H2-engine call (via
    the CP6 adapter) per config, over the full window with ``fold_id=None``
    -- never selected-OOS reuse. Returns a zero-filled 365-day grid per
    config, ready for ``validate_full_window_grid``."""
    if len(configs) != 24:
        raise H4PboError(
            f"h4_pbo_config_count: expected exactly 24 configs, got {len(configs)}"
        )
    strategy = "S3"
    _require_config_roster(strategy, configs)
    grid: dict[str, dict[str, float]] = {}
    # Strong references (not just `id()`) are kept alive for the whole loop --
    # CPython reuses a freed object's id, so an id-only set would false-
    # positive-miss real aliasing once an earlier empty-candidate `[]` is GC'd.
    seen_buffers: list[object] = []
    for config in configs:
        candidates = generator(config)
        if any(candidates is buffer for buffer in seen_buffers):
            raise H4PboError(
                "h4_pbo_shared_candidate_buffer: generator returned an aliased "
                f"object across configs for {config.config_id!r} -- each config "
                "requires its own independent candidate buffer"
            )
        seen_buffers.append(candidates)
        sealed = invoke_actual_s3_engine(
            candidates=candidates,
            minute_index=minute_index,
            close_feature_index=close_feature_index,
            corpus_end_ts=WINDOW_END_MS,
            horizon_end_ts=WINDOW_END_MS,
            strategy=strategy,
            config_id=config.config_id,
            fold_id=None,
        )
        day_totals: dict[str, float] = dict.fromkeys(FROZEN_DAY_KEYS, 0.0)
        aggregate_full_window_s3_trades(
            config.config_id, sealed.result.trades, day_totals=day_totals
        )
        grid[config.config_id] = day_totals
    return grid


def run_full_window_s4_configs(
    *,
    configs: Sequence[object],
    generator: Callable[[object], Sequence[object]],
    minute_index,
    pair_close_index,
) -> dict[str, dict[str, float]]:
    if len(configs) != 24:
        raise H4PboError(
            f"h4_pbo_config_count: expected exactly 24 configs, got {len(configs)}"
        )
    strategy = "S4"
    _require_config_roster(strategy, configs)
    grid: dict[str, dict[str, float]] = {}
    seen_buffers: list[object] = []
    for config in configs:
        candidates = generator(config)
        if any(candidates is buffer for buffer in seen_buffers):
            raise H4PboError(
                "h4_pbo_shared_candidate_buffer: generator returned an aliased "
                f"object across configs for {config.config_id!r} -- each config "
                "requires its own independent candidate buffer"
            )
        seen_buffers.append(candidates)
        sealed = invoke_actual_s4_engine(
            candidates=candidates,
            minute_index=minute_index,
            pair_close_index=pair_close_index,
            corpus_end_ts=WINDOW_END_MS,
            horizon_end_ts=WINDOW_END_MS,
            strategy=strategy,
            config_id=config.config_id,
            fold_id=None,
        )
        day_totals: dict[str, float] = dict.fromkeys(FROZEN_DAY_KEYS, 0.0)
        aggregate_full_window_s4_trades(
            config.config_id, sealed.result.trades, day_totals=day_totals
        )
        grid[config.config_id] = day_totals
    return grid


def validate_full_window_grid(
    *, strategy: str, daily_gross_bps_by_config: Mapping[str, Mapping[str, object]]
) -> dict[str, tuple[float, ...]]:
    """Adversarially re-validate a sealed 24-config x 365-day grid: exact
    canonical config set, exact frozen day-key set per row, and every cell a
    plain finite built-in ``int``/``float`` (never ``bool``/``Decimal``/a
    float subclass/NaN/Inf). Returns canonically (config-id-ascending,
    frozen-day-order) aligned tuples ready for the frozen CSCV primitive --
    caller float-summation order (and therefore the resulting bit pattern)
    is always identical regardless of input mapping order."""
    if strategy not in _STRATEGIES:
        raise H4PboError(
            f"h4_pbo_strategy: expected strategy in {_STRATEGIES!r}, got {strategy!r}"
        )
    expected_config_ids = frozenset(f"{strategy}-{i:02d}" for i in range(24))
    config_ids = tuple(daily_gross_bps_by_config.keys())
    if set(config_ids) != expected_config_ids or len(set(config_ids)) != len(
        config_ids
    ):
        raise H4PboError(
            f"h4_pbo_grid_config_ids: expected exactly the frozen canonical "
            f"24-config set {sorted(expected_config_ids)!r}, got "
            f"{sorted(set(config_ids))!r}"
        )
    aligned: dict[str, tuple[float, ...]] = {}
    for config_id in sorted(expected_config_ids):
        row = daily_gross_bps_by_config[config_id]
        day_keys = tuple(row.keys())
        if frozenset(day_keys) != _DAY_KEY_SET:
            extra = sorted(frozenset(day_keys) - _DAY_KEY_SET)
            missing = sorted(_DAY_KEY_SET - frozenset(day_keys))
            raise H4PboError(
                f"h4_pbo_grid_day_mismatch: {config_id!r} day-key set does not "
                f"equal the frozen 365-day full window (extra={extra[:3]}, "
                f"missing={missing[:3]})"
            )
        values: list[float] = []
        for day in FROZEN_DAY_KEYS:
            raw = row[day]
            if isinstance(raw, bool) or not isinstance(raw, int | float):
                raise H4PboError(
                    f"h4_pbo_grid_non_numeric_cell: {config_id!r}/{day} is not a "
                    f"plain int/float return, got {type(raw).__name__!r}"
                )
            if type(raw) not in (int, float):
                raise H4PboError(
                    f"h4_pbo_grid_non_builtin_cell: {config_id!r}/{day} must be an "
                    f"exact built-in int/float, got {type(raw).__name__!r}"
                )
            value = float(raw)
            if not math.isfinite(value):
                raise H4PboError(
                    f"h4_pbo_grid_non_finite_cell: {config_id!r}/{day} is non-finite"
                )
            values.append(value)
        aligned[config_id] = tuple(values)
    return aligned


def seal_full_window_grid(
    *, strategy: str, aligned: Mapping[str, Sequence[float]]
) -> str:
    payload = {
        "schema_version": "rob974_h4_pbo_grid_v1",
        "strategy": strategy,
        "slices": PBO_SLICES,
        "window": [WINDOW_START_MS, WINDOW_END_MS],
        "day_keys": list(FROZEN_DAY_KEYS),
        "config_count": len(aligned),
        "returns_by_config": {
            config_id: list(aligned[config_id]) for config_id in sorted(aligned)
        },
    }
    return canonical_sha256(payload)


def compute_h4_full_window_pbo(
    *, strategy: str, daily_gross_bps_by_config: Mapping[str, Mapping[str, object]]
) -> H4PboEvidence:
    """The one-call entrypoint: validates the sealed grid, reseals it, and
    feeds ONLY the frozen ``probability_backtest_overfitting`` CSCV primitive
    (never re-implemented) with ``slices=4``. Reference-only: never raises on
    a structurally valid but statistically degenerate (e.g. all-zero) grid --
    an invalid/missing statistic surfaces as ``value=None`` plus
    ``reason_codes``, never a fabricated pass/fail verdict."""
    aligned = validate_full_window_grid(
        strategy=strategy, daily_gross_bps_by_config=daily_gross_bps_by_config
    )
    grid_seal = seal_full_window_grid(strategy=strategy, aligned=aligned)
    result = probability_backtest_overfitting(aligned, slices=PBO_SLICES)
    return H4PboEvidence(
        strategy=strategy,
        value=result.value,
        reason_codes=result.reason_codes,
        slices=PBO_SLICES,
        config_count=len(aligned),
        day_count=len(FROZEN_DAY_KEYS),
        grid_seal_sha256=grid_seal,
    )
