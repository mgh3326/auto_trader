"""ROB-1062 H4 — golden digest over the runner's full output on a fixed
synthetic fixture.

Scope note: the digest covers the FULL ``FamilyFoldResult`` for AP-A2 (all 8
sealed configs, one fold) — AP-A2's weekly cadence is used rather than
AP-A1's daily one purely for CI runtime (see ``test_runner.py``'s module
docstring: H3's per-decision seal reload makes AP-A1 ~7x more expensive for
the same config count; AP-A1's own code path is separately exercised by
``test_runner.py``'s dedicated regression test).

The digest NEVER unmasks OOS PnL (that would defeat the entire point of
AC22-25) — it is computed over ``TRAIN`` metrics (numbers), OOS blind counts
(numbers), the COUNT and (fold_id, family, config_id) BINDING metadata of
each masked OOS entry (never the underlying value), the context-binding
hash, and the selection result.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import blind_counts as bc
import canonical_hash
import fold_schedule as fs
import pytest
import runner
import synthetic_fixture as sfx

_ANCHOR_MS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
_FOLD = fs.build_fold_schedule(_ANCHOR_MS)[0]
_NUM_DAYS = (_FOLD.oos_end_ms - _FOLD.train_start_ms) // 86_400_000

# Pinned 2026-07-27 after the audit-contract change. The moved summary was
# decomposed before re-pinning: event-time attribution removed only
# cross-boundary trades from TRAIN closed/E120 (entry counts stayed fixed),
# actual-filled-notional C120 replaced the count-as-full-NAV formula, OOS
# blind counts and NO_SELECTED_CONFIG stayed unchanged, and the strengthened
# context/evidence fields were added. Synthetic prices and this hash
# projection are quantized only to remove CPython/libm last-bit differences
# between macOS and Linux; runner gates still compare full-precision values.
# Any later change remains a deliberate re-seal: STOP and inspect the
# human-readable diagnostic before touching this constant.
GOLDEN_DIGEST = "d8a78ac0a9ebe38169ff9e4abd887e4a81e4b8fe3db11308bb71323b00bac922"


def _golden_float(value: float) -> float:
    """Cross-platform golden projection only, never a gate input."""
    return round(value, 10)


def _digest_summary(result: runner.FamilyFoldResult) -> dict:
    """A JSON-safe summary of the FULL run — never touches a masked raw
    value (no ``unmask`` call anywhere in this function)."""
    return {
        "family": result.family,
        "fold_id": result.fold_id,
        "selection": {
            "status": result.selection.status,
            "selected_config_id": result.selection.selected_config_id,
        },
        "configs": {
            cr.config_id: {
                "train_metrics": {
                    "closed_trades_count": cr.train_metrics.closed_trades_count,
                    "median_trade_e120_bp": (
                        _golden_float(cr.train_metrics.median_trade_e120_bp)
                        if cr.train_metrics.median_trade_e120_bp is not None
                        else None
                    ),
                    "modeled_entries_count": cr.train_metrics.modeled_entries_count,
                    "turnover_p": _golden_float(cr.train_metrics.turnover_p),
                    "modeled_entry_evidence": [
                        {
                            "entry_fill_ts_ms": entry.entry_fill_ts_ms,
                            "filled_qty": _golden_float(entry.filled_qty),
                            "entry_fill_price": _golden_float(entry.entry_fill_price),
                            "entry_filled_notional": _golden_float(
                                entry.entry_filled_notional
                            ),
                        }
                        for entry in cr.train_metrics.modeled_entry_evidence
                    ],
                    "blind_counts": {
                        "total_decision_records": (
                            cr.train_metrics.blind_counts.total_decision_records
                        ),
                        "modeled_entries_count": (
                            cr.train_metrics.blind_counts.modeled_entries_count
                        ),
                        "closed_trades_count": (
                            cr.train_metrics.blind_counts.closed_trades_count
                        ),
                        "open_positions_count": (
                            cr.train_metrics.blind_counts.open_positions_count
                        ),
                        "entry_unfilled_count": (
                            cr.train_metrics.blind_counts.entry_unfilled_count
                        ),
                        "exit_unfilled_count": (
                            cr.train_metrics.blind_counts.exit_unfilled_count
                        ),
                        "fill_window_incomplete_count": (
                            cr.train_metrics.blind_counts.fill_window_incomplete_count
                        ),
                        "holding_days": list(
                            cr.train_metrics.blind_counts.holding_days
                        ),
                        "reason_code_histogram": dict(
                            cr.train_metrics.blind_counts.reason_code_histogram
                        ),
                    },
                },
                "oos_blind_counts": {
                    "total_decision_records": cr.oos_blind_counts.total_decision_records,
                    "modeled_entries_count": cr.oos_blind_counts.modeled_entries_count,
                    "closed_trades_count": cr.oos_blind_counts.closed_trades_count,
                    "open_positions_count": cr.oos_blind_counts.open_positions_count,
                    "entry_unfilled_count": cr.oos_blind_counts.entry_unfilled_count,
                    "exit_unfilled_count": cr.oos_blind_counts.exit_unfilled_count,
                    "fill_window_incomplete_count": (
                        cr.oos_blind_counts.fill_window_incomplete_count
                    ),
                    "holding_days": list(cr.oos_blind_counts.holding_days),
                    "reason_code_histogram": dict(
                        cr.oos_blind_counts.reason_code_histogram
                    ),
                },
                "oos_modeled_entry_evidence": [
                    {
                        "entry_fill_ts_ms": entry.entry_fill_ts_ms,
                        "filled_qty": _golden_float(entry.filled_qty),
                        "entry_fill_price": _golden_float(entry.entry_fill_price),
                        "entry_filled_notional": _golden_float(
                            entry.entry_filled_notional
                        ),
                    }
                    for entry in cr.oos_modeled_entry_evidence
                ],
                "oos_masked_pnl_bindings": [
                    {
                        "fold_id": m.fold_id,
                        "family": m.family,
                        "config_id": m.config_id,
                    }
                    for m in cr.oos_masked_pnl_by_trade
                ],
                "context_binding_hash": cr.context_binding_at_oos_start.combined_context_hash,
            }
            for cr in result.config_runs
        },
    }


def _digest_diagnostics(result: runner.FamilyFoldResult) -> dict:
    """Human-readable semantic projection for a moved golden hash.

    This intentionally uses the production C120 accounting function so a
    digest failure exposes whether the full runner and the frozen-formula
    unit tests are still exercising the same calculation.
    """
    return {
        "selection": {
            "status": result.selection.status,
            "selected_config_id": result.selection.selected_config_id,
        },
        "configs": {
            run.config_id: {
                "train": {
                    "closed": run.train_metrics.closed_trades_count,
                    "median_e120_bp": run.train_metrics.median_trade_e120_bp,
                    "modeled_entries": run.train_metrics.modeled_entries_count,
                    "open": run.train_metrics.blind_counts.open_positions_count,
                    "annualized_c120_pct": bc.annualized_stress_cost_pct(
                        entry_filled_notionals=tuple(
                            entry.entry_filled_notional
                            for entry in run.train_metrics.modeled_entry_evidence
                        ),
                        window_days=365,
                        nav_usd=2000.0,
                        cost_bp=120.0,
                    ),
                },
                "oos_blind_counts": {
                    "modeled_entries": run.oos_blind_counts.modeled_entries_count,
                    "closed": run.oos_blind_counts.closed_trades_count,
                    "open": run.oos_blind_counts.open_positions_count,
                    "entry_unfilled": run.oos_blind_counts.entry_unfilled_count,
                    "exit_unfilled": run.oos_blind_counts.exit_unfilled_count,
                    "incomplete": run.oos_blind_counts.fill_window_incomplete_count,
                    "holding_days": list(run.oos_blind_counts.holding_days),
                    "histogram": dict(run.oos_blind_counts.reason_code_histogram),
                },
                "context_binding_hash": (
                    run.context_binding_at_oos_start.combined_context_hash
                ),
            }
            for run in result.config_runs
        },
    }


def _run() -> runner.FamilyFoldResult:
    bars = sfx.build_bars_by_symbol(
        window_start_ms=_FOLD.train_start_ms, num_days=_NUM_DAYS, n_symbols=20
    )
    universe_provider = sfx.make_universe_snapshot_provider(20)
    minute_provider = sfx.make_minute_bars_provider(
        window_start_ms=_FOLD.train_start_ms, n_symbols=20
    )
    return runner.run_family_fold(
        family="AP-A2",
        fold_id="fold-0",
        fold=_FOLD,
        bars_by_symbol=bars,
        universe_snapshot_provider=universe_provider,
        minute_bars_provider=minute_provider,
    )


# Module-scoped: the full 8-config AP-A2 run is expensive (H3's own
# per-decision seal reload, see test_runner.py's module docstring) -- every
# test in this file that only needs the BASELINE result reuses this ONE
# computation rather than re-running it, to keep total CI time bounded.
# The "moves on a real change" and "reproducible across repeated calls"
# tests below each still perform their OWN independent second `_run()` call
# (unavoidable -- that is the entire point of what they prove).
@pytest.fixture(scope="module")
def baseline_result() -> runner.FamilyFoldResult:
    return _run()


@pytest.fixture(scope="module")
def baseline_digest(baseline_result) -> str:
    return canonical_hash.canonical_sha256(_digest_summary(baseline_result))


@pytest.mark.slow
def test_golden_digest_matches_the_pinned_value(baseline_digest, baseline_result):
    # First-run bootstrap: print the real digest so it can be pinned above.
    print(f"\nCOMPUTED GOLDEN DIGEST: {baseline_digest}")
    if GOLDEN_DIGEST != "pending":
        assert baseline_digest == GOLDEN_DIGEST, json.dumps(
            _digest_diagnostics(baseline_result),
            sort_keys=True,
            indent=2,
        )


@pytest.mark.slow
def test_golden_runner_costs_match_the_frozen_full_precision_formula(
    baseline_result,
):
    """Cross-check the golden run against the same frozen equation whose
    range/30-entry cases are pinned in ``test_blind_counts.py``.

    Golden float normalization is presentation-only: both sides here use
    the immutable raw fill evidence and compare the production calculation
    before any rounding.
    """
    for run in baseline_result.config_runs:
        evidence = run.train_metrics.modeled_entry_evidence
        production = bc.annualized_stress_cost_pct(
            entry_filled_notionals=tuple(
                entry.entry_filled_notional for entry in evidence
            ),
            window_days=365,
            nav_usd=2000.0,
            cost_bp=120.0,
        )
        direct = (
            100.0
            * (365.0 / 365)
            * sum(
                ((entry.filled_qty * entry.entry_fill_price) / 2000.0) * 0.012
                for entry in evidence
            )
        )
        assert production == direct


@pytest.mark.slow
def test_golden_digest_moves_on_a_real_behavioral_change(baseline_digest):
    """Proof the pinned digest is non-vacuous: a genuine behavioral change
    (widening the entry fill window from 2 minutes to 3) moves it. This is
    NOT the `r1 == r2` determinism check H3 was sent back for lacking —
    this specifically proves the digest is SENSITIVE to real behavior."""
    import fill_model as fm

    original = fm.FILL_WINDOW_MINUTE_COUNT
    try:
        fm.FILL_WINDOW_MINUTE_COUNT = 3
        mutated_summary = _digest_summary(_run())
    finally:
        fm.FILL_WINDOW_MINUTE_COUNT = original
    mutated = canonical_hash.canonical_sha256(mutated_summary)

    assert mutated != baseline_digest


@pytest.mark.slow
def test_golden_digest_is_reproducible_across_a_second_independent_run(baseline_digest):
    """A lighter-weight, same-process determinism check (r1 == r2) --
    useful but NOT a substitute for the "moves on a real change" proof
    above (H3 was sent back precisely for stopping at this check alone)."""
    second_digest = canonical_hash.canonical_sha256(_digest_summary(_run()))
    assert second_digest == baseline_digest
