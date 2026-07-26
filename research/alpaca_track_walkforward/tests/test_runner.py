"""ROB-1062 H4 — runner integration tests against the synthetic fixture.

Runtime note: H3's ``seal_consumption.assert_sealed_config`` rebuilds and
re-hashes the ENTIRE H2 seal from disk on every single engine decision (by
design — this is H3's own NO_THRESHOLD_RELAXATION enforcement point, not
something H4 may cache around, since caching it here could mask a mutation
that breaks that very check). AP-A1's daily cadence means 393 decisions per
config across one fold; AP-A2's weekly cadence means ~56 — roughly 7x
cheaper. The full-8-config tests below therefore use AP-A2. A dedicated,
single-config AP-A1 slice covers the one AP-A1-specific regression this
module's own development surfaced (see
``test_unfilled_entry_never_produces_an_orphan_exit_ap_a1``).
"""

from __future__ import annotations

from datetime import UTC, datetime

import fold_schedule as fs
import oos_mask as om
import pytest
import runner
import synthetic_fixture as sfx

_ANCHOR_MS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
_FOLD = fs.build_fold_schedule(_ANCHOR_MS)[0]
_NUM_DAYS = (_FOLD.oos_end_ms - _FOLD.train_start_ms) // 86_400_000


@pytest.fixture(scope="module")
def bars_20():
    return sfx.build_bars_by_symbol(
        window_start_ms=_FOLD.train_start_ms, num_days=_NUM_DAYS, n_symbols=20
    )


@pytest.fixture(scope="module")
def universe_provider_20():
    return sfx.make_universe_snapshot_provider(20)


@pytest.fixture(scope="module")
def minute_provider_20():
    return sfx.make_minute_bars_provider(
        window_start_ms=_FOLD.train_start_ms, n_symbols=20
    )


def test_unfilled_entry_never_produces_an_orphan_exit_ap_a1(
    bars_20, universe_provider_20, minute_provider_20
):
    """Regression test for the real bug this module's own development
    caught: without fill-aware state patching, H3's engine's naive
    bookkeeping later emits an EXIT for a symbol whose ENTRY never actually
    filled, which the trade ledger cannot pair (a crash, not a silent
    wrong-answer). Running to completion without raising IS the proof."""
    import seal_consumption as h3_seal

    bundle = h3_seal.load_sealed_configs_and_params()
    config = next(c for c in bundle.configs if c.family == "AP-A1")
    result = runner._run_continuous_decisions(
        config=config,
        family="AP-A1",
        fold=_FOLD,
        bars_by_symbol=bars_20,
        universe_snapshot_provider=universe_provider_20,
        minute_bars_provider=minute_provider_20,
    )
    # Reaching here without a _RunnerInternalInvariantError IS the proof.
    assert isinstance(result.all_records, tuple)
    # This fixture's fill model DOES produce genuine unfilled attempts
    # (sinusoidal day-over-day moves sometimes exceed the +-0.5% band) --
    # confirm the scenario this regression test exists for actually occurs,
    # not merely that nothing crashed on an empty no-op.
    unfilled_or_incomplete = [
        a
        for a in result.fill_attempts
        if a.outcome.reason
        in ("ENTRY_UNFILLED", "EXIT_UNFILLED", "FILL_WINDOW_INCOMPLETE")
    ]
    assert len(unfilled_or_incomplete) > 0


@pytest.fixture(scope="module")
def ap_a2_full_result(bars_20, universe_provider_20, minute_provider_20):
    # Expensive (~1 minute, all 8 configs) -- module-scoped so the two
    # tests below share ONE computation instead of re-running it.
    return runner.run_family_fold(
        family="AP-A2",
        fold_id="fold-0",
        fold=_FOLD,
        bars_by_symbol=bars_20,
        universe_snapshot_provider=universe_provider_20,
        minute_bars_provider=minute_provider_20,
    )


@pytest.mark.slow
def test_run_family_fold_ap_a2_full_8_configs_end_to_end(ap_a2_full_result):
    result = ap_a2_full_result
    assert result.family == "AP-A2"
    assert len(result.config_runs) == 8
    assert {cr.config_id for cr in result.config_runs} == {
        f"AP-A2-{i:02d}" for i in range(8)
    }
    assert result.selection.status in ("SELECTED", "NO_SELECTED_CONFIG")

    for cr in result.config_runs:
        # PnL-blind counts are ALWAYS visible (AC25) -- plain ints/dicts,
        # never wrapped.
        assert isinstance(cr.oos_blind_counts.modeled_entries_count, int)
        assert isinstance(cr.oos_blind_counts.reason_code_histogram, dict)
        # OOS PnL is masked by default (AC22) -- every entry is a real
        # Masked instance bound to this exact fold/family/config, and
        # cannot be read without evidence.
        for masked in cr.oos_masked_pnl_by_trade:
            assert isinstance(masked, om.Masked)
            assert masked.fold_id == "fold-0"
            assert masked.family == "AP-A2"
            assert masked.config_id == cr.config_id
            with pytest.raises(om.OOSMaskBypassError):
                om.unmask(
                    masked,
                    om.DryCountPassEvidence(
                        fold_id="WRONG-fold",
                        family="AP-A2",
                        config_id=cr.config_id,
                        modeled_entries=999,
                        min_modeled_entries_per_fold=5,
                        passed=True,
                    ),
                )
        # TRAIN PnL (median E120) IS directly readable (AC23) -- plain
        # float or None, never masked.
        assert cr.train_metrics.median_trade_e120_bp is None or isinstance(
            cr.train_metrics.median_trade_e120_bp, float
        )


@pytest.mark.slow
def test_selection_uses_only_train_metrics_never_oos(ap_a2_full_result):
    """Structural proof mirroring AC8: selection is computed purely from
    ConfigTrainMetrics built out of TRAIN-phase trades; the selected
    config_id is one of the 8 real AP-A2 config ids or NO_SELECTED_CONFIG,
    and the winning config (if any) is byte-traceable to a config whose
    TRAIN metrics alone satisfy config_selection's rule."""
    import config_selection as cs

    result = ap_a2_full_result
    if result.selection.status == "SELECTED":
        winner = next(
            cr
            for cr in result.config_runs
            if cr.config_id == result.selection.selected_config_id
        )
        assert winner.train_metrics.closed_trades_count >= cs.MIN_TRAIN_CLOSED_TRADES
        assert winner.train_metrics.median_trade_e120_bp > 0.0
