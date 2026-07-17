"""ROB-960 -- full-window, fold-agnostic per-config PBO evaluator.

Implements ``rob945_pbo_builder.EvaluateConfigCallback`` using ONLY existing
frozen H1-H4 primitives -- and, per captain plan-gate G1, reuses
``rob944_walkforward._run_scenario`` DIRECTLY (never reassembles the
funding-gate/ordering/engine/gap-in-position sequence independently) at
``COST_SCENARIO_PRIMARY_STRESS`` with ``fold_id=None`` throughout. H3's
signal generators already accept ``fold_id=None`` and H2's ``TradeRecord``
copies it straight through untouched -- exactly the dedicated no-fold
sentinel ``rob945_pbo_builder._validate_trade`` requires.

Captain Task-1-closure gate (2026-07-18 08:22 KST, C1-C4) hardening, on top
of the G1/G2 plan-gate corrections:

C1 (no silent gap-authority default): ``gap_ranges`` is a REQUIRED
argument to both ``build_evaluate_config_callback`` and
``compute_pbo_evidence_for_strategy`` -- there is no ``gap_ranges=None``
default and no ``.get(symbol, ())`` fallback anywhere. Construction
reuses ``rob944_walkforward._validate_exact_universe_coverage`` to prove
``bars_1m``/``funding_sidecars``/``gap_ranges`` each cover EXACTLY the
frozen 4-symbol universe, then asserts every symbol's gap ranges are
empty -- BEFORE the closure is even returned, so a violation fails with
ZERO generator/``_run_scenario`` calls ever made. A successful response's
``gap_invalid_days=frozenset()`` is therefore always downstream of this
proof, never an independent claim.

C2 (request provenance pinned before execution): every call into the
returned closure re-validates ``request.strategy``/``config_id`` against
the callback-bound strategy/canonical config set, and
``scenario_name``/``cost_bps``/``window_start_iso``/``window_end_iso``/
``symbols`` against the exact frozen PBO identity
(``FROZEN_PBO_SCENARIO_NAME``/``FROZEN_PBO_COST_BPS``/
``FROZEN_PBO_WINDOW_START_ISO``/``FROZEN_PBO_WINDOW_END_ISO``/
``rob941_frozen_scope.UNIVERSE`` in exact order) -- any mismatch raises
``Rob945PboBuilderError`` with ZERO H3 generator/``_run_scenario`` calls.
Execution itself is always pinned at ``COST_SCENARIO_PRIMARY_STRESS``
regardless of what a request claims; this module never lets a caller's
arbitrary cost/window silently pass through into what's actually run.

C3 (exact full-window authority): ``bars_1m``/``funding_sidecars`` are
assumed to already be the EXACT full-frozen-window corpus, as validated by
the production caller (``rob960_empirical_orchestrator``, via H1's pinned
manifest + ``rob941_offline_loader.load_corpus``) -- this module's own
``_slice_bars`` is a defensive `[start, end)` filter (a no-op against
already-correct loader output), never a mechanism that promotes a short or
incomplete bar sequence into "full window" coverage. Unit tests in this
module's own test file use short synthetic bars ONLY as an injected fixture
seam -- documented there explicitly as not a production-valid corpus
contract.

C4 (H4 terminal status never leaks outside H5's closed set): a
``_run_scenario`` outcome whose ``status != "completed"`` (or whose
``engine_result is None``) is NEVER promoted into a ``SymbolOutcome`` --
this module raises ``Rob945PboBuilderError`` immediately instead. A
success response therefore only ever carries ``status="completed"`` with
real ``engine_result.trades``. Downstream of the C1 empty-gap proof,
``_run_scenario``'s own gap-rejection path is structurally unreachable, so
in practice only ``crashed``/``timeout`` remain possible non-completed
outcomes, and both now fail closed at this boundary rather than leaking a
status H5's PBO vocabulary was never designed to represent.
"""

from __future__ import annotations

import rob941_frozen_scope as frozen
from rob940_bars_agg import aggregate_complete
from rob940_cost_model import COST_SCENARIO_PRIMARY_STRESS
from rob940_signal_manifest import FROZEN_S1_CONFIGS, FROZEN_S2_CONFIGS
from rob940_signal_s1 import generate_s1_signals
from rob940_signal_s2 import generate_s2_signals
from rob944_walkforward import _run_scenario, _validate_exact_universe_coverage
from rob945_pbo_builder import (
    ConfigEvaluationResponse,
    Rob945PboBuilderError,
    SymbolOutcome,
)
from rob945_pbo_grid import (
    FROZEN_PBO_COST_BPS,
    FROZEN_PBO_SCENARIO_NAME,
    FROZEN_PBO_WINDOW_END_ISO,
    FROZEN_PBO_WINDOW_START_ISO,
)
from run_rob944_campaign import _s2_rejections_to_no_trade_records

_CONFIGS_BY_STRATEGY = {
    "S1": {c.config_id: c for c in FROZEN_S1_CONFIGS},
    "S2": {c.config_id: c for c in FROZEN_S2_CONFIGS},
}


def _slice_bars(bars, start_ms: int, end_ms: int):
    """Defensive `[start, end)` filter only -- see module docstring C3.
    Never a substitute for the caller's own full-window coverage proof."""
    return tuple(b for b in bars if start_ms <= b.ts < end_ms)


def _assert_gap_ranges_all_empty(gap_ranges) -> None:
    nonempty = {symbol: ranges for symbol, ranges in gap_ranges.items() if ranges}
    if nonempty:
        raise Rob945PboBuilderError(
            f"rob960_pbo_evaluator_gap_ranges_nonempty: {sorted(nonempty)!r} report "
            "nonempty gap_ranges -- PBO full-window evaluation requires a proven-"
            "empty-gap corpus (captain plan-gate G2/C1); refusing to construct an "
            "evaluator, zero generator/_run_scenario calls made"
        )


def _assert_request_provenance(request, *, strategy: str, configs_by_id: dict) -> None:
    if request.strategy != strategy:
        raise Rob945PboBuilderError(
            "rob960_pbo_evaluator_request_strategy_mismatch: refusing to execute -- "
            "zero generator/_run_scenario calls made"
        )
    if request.config_id not in configs_by_id:
        raise Rob945PboBuilderError(
            "rob960_pbo_evaluator_request_config_id_not_canonical: refusing to "
            "execute -- zero generator/_run_scenario calls made"
        )
    if request.scenario_name != FROZEN_PBO_SCENARIO_NAME:
        raise Rob945PboBuilderError(
            "rob960_pbo_evaluator_request_scenario_name_mismatch: refusing to "
            "execute -- zero generator/_run_scenario calls made"
        )
    if request.cost_bps != FROZEN_PBO_COST_BPS:
        raise Rob945PboBuilderError(
            "rob960_pbo_evaluator_request_cost_bps_mismatch: refusing to execute -- "
            "zero generator/_run_scenario calls made"
        )
    if (
        request.window_start_iso != FROZEN_PBO_WINDOW_START_ISO
        or request.window_end_iso != FROZEN_PBO_WINDOW_END_ISO
    ):
        raise Rob945PboBuilderError(
            "rob960_pbo_evaluator_request_window_mismatch: refusing to execute -- "
            "zero generator/_run_scenario calls made"
        )
    if request.symbols != frozen.UNIVERSE:
        raise Rob945PboBuilderError(
            "rob960_pbo_evaluator_request_symbols_mismatch: refusing to execute -- "
            "zero generator/_run_scenario calls made"
        )


def build_evaluate_config_callback(
    *, bars_1m, funding_sidecars, gap_ranges, strategy: str
):
    """Returns an ``EvaluateConfigCallback`` bound to the given corpus and
    strategy. ``gap_ranges`` is REQUIRED (C1) -- construction itself raises
    (zero generator/``_run_scenario`` calls) unless
    ``bars_1m``/``funding_sidecars``/``gap_ranges`` each cover exactly the
    frozen 4-symbol universe AND every symbol's gap ranges are empty.
    """
    _validate_exact_universe_coverage(bars_1m, funding_sidecars, gap_ranges)
    _assert_gap_ranges_all_empty(gap_ranges)
    configs_by_id = _CONFIGS_BY_STRATEGY[strategy]

    def _evaluate(request):
        _assert_request_provenance(
            request, strategy=strategy, configs_by_id=configs_by_id
        )
        config = configs_by_id[request.config_id]
        outcomes = []
        for symbol in request.symbols:
            bars_slice = _slice_bars(
                bars_1m[symbol], frozen.WINDOW_START_MS, frozen.WINDOW_END_MS
            )
            pre_execution_rejections = ()
            if strategy == "S1":
                bars_15m = aggregate_complete(bars_slice, bucket_minutes=15)
                signals = generate_s1_signals(
                    bars_15m, config, symbol=symbol, fold_id=None
                )
            else:
                bars_5m = aggregate_complete(bars_slice, bucket_minutes=5)
                gen_result = generate_s2_signals(
                    bars_5m, bars_slice, config, symbol=symbol, fold_id=None
                )
                signals = gen_result.signals
                pre_execution_rejections = _s2_rejections_to_no_trade_records(
                    gen_result.rejections
                )
            sidecar = funding_sidecars[symbol]
            outcome, engine_result = _run_scenario(
                bars_slice,
                signals,
                COST_SCENARIO_PRIMARY_STRESS,
                sidecar,
                gap_ranges[symbol],
                strategy=strategy,
                config_id=request.config_id,
                symbol=symbol,
                fold_id=None,
                pre_execution_rejections=pre_execution_rejections,
            )
            # C4: a non-completed H4 terminal outcome is NEVER promoted into
            # an H5 SymbolOutcome -- fail immediately instead.
            if outcome.status != "completed" or engine_result is None:
                raise Rob945PboBuilderError(
                    f"rob960_pbo_evaluator_noncompleted_h4_outcome: {symbol!r}/"
                    f"{request.config_id!r} produced status={outcome.status!r} -- "
                    "refusing to promote a non-completed H4 outcome into PBO evidence"
                )
            outcomes.append(
                SymbolOutcome(
                    symbol=symbol,
                    status="completed",
                    trades=engine_result.trades,
                    # Only ever frozenset() -- downstream of the mandatory
                    # empty-gap proof in build_evaluate_config_callback
                    # above; _run_scenario's own gap-rejection path is
                    # therefore structurally unreachable here.
                    gap_invalid_days=frozenset(),
                )
            )
        return ConfigEvaluationResponse(
            strategy=strategy,
            config_id=request.config_id,
            scenario_name=request.scenario_name,
            cost_bps=request.cost_bps,
            window_start_iso=request.window_start_iso,
            window_end_iso=request.window_end_iso,
            symbol_outcomes=tuple(outcomes),
        )

    return _evaluate


def compute_pbo_evidence_for_strategy(
    *, strategy: str, bars_1m, funding_sidecars, gap_ranges
):
    """The one-call entrypoint: builds the 12-config full-window grid (24
    canonical requests / 96 independent per-symbol ``_run_scenario`` calls
    across both strategies) and reduces it to ``PboAuxiliaryEvidence``.
    ``gap_ranges`` is REQUIRED (C1 -- no silent default). Raises
    ``rob945_pbo_grid.PboGridError``/``rob945_pbo_builder.Rob945PboBuilderError``
    on any structural invalidity -- per captain plan-gate G3/G9, the caller
    MUST let this propagate as a fail-closed materialization abort (no
    fabricated/placeholder PBO evidence anywhere downstream of this call).
    """
    from rob945_pbo_builder import build_pbo_daily_grid
    from rob945_pbo_grid import compute_pbo_auxiliary_evidence

    evaluate = build_evaluate_config_callback(
        bars_1m=bars_1m,
        funding_sidecars=funding_sidecars,
        gap_ranges=gap_ranges,
        strategy=strategy,
    )
    grid, gaps = build_pbo_daily_grid(strategy=strategy, evaluate_config=evaluate)
    return compute_pbo_auxiliary_evidence(
        strategy=strategy,
        daily_net_bps_by_config=grid,
        gap_invalid_days_by_config=gaps,
    )
