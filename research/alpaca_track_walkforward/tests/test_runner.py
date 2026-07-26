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

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

import fill_model as fm
import fold_schedule as fs
import oos_mask as om
import provider_evidence as pe
import pytest
import runner
import synthetic_fixture as sfx
import trade_ledger as tl
import wf_seal_consumption as wf_seal

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


def test_future_universe_snapshot_is_rejected_fail_closed(
    bars_20, universe_provider_20, minute_provider_20
):
    """Verifier reproduction: a provider cannot return a future U_t."""
    import seal_consumption as h3_seal

    config = next(
        c
        for c in h3_seal.load_sealed_configs_and_params().configs
        if c.family == "AP-A1"
    )

    def future_provider(decision_ts_ms):
        current = universe_provider_20(decision_ts_ms)
        future_snapshot = replace(
            current.snapshot,
            decision_ts_ms=decision_ts_ms + 999 * 86_400_000,
        )
        return pe.bind_universe_snapshot(
            future_snapshot,
            source_as_of_ts_ms=decision_ts_ms + 999 * 86_400_000,
        )

    with pytest.raises(runner.ProviderTimeBindingError, match="does not equal"):
        runner._run_continuous_decisions(
            config=config,
            family="AP-A1",
            fold=_FOLD,
            bars_by_symbol=bars_20,
            universe_snapshot_provider=future_provider,
            minute_bars_provider=minute_provider_20,
        )


def test_future_universe_content_relabelled_to_current_timestamp_is_rejected(
    bars_20, universe_provider_20, minute_provider_20
):
    """Verifier reproduction: content keeps its source artifact as-of."""
    import seal_consumption as h3_seal

    config = next(
        c
        for c in h3_seal.load_sealed_configs_and_params().configs
        if c.family == "AP-A1"
    )

    def relabelled_provider(decision_ts_ms):
        future = universe_provider_20(decision_ts_ms + 28 * 86_400_000)
        relabelled = replace(future.snapshot, decision_ts_ms=decision_ts_ms)
        return pe.bind_universe_snapshot(
            relabelled,
            source_as_of_ts_ms=future.source_as_of_ts_ms,
        )

    with pytest.raises(runner.ProviderTimeBindingError, match="source as-of"):
        runner._run_continuous_decisions(
            config=config,
            family="AP-A1",
            fold=_FOLD,
            bars_by_symbol=bars_20,
            universe_snapshot_provider=relabelled_provider,
            minute_bars_provider=minute_provider_20,
        )


def test_mutated_universe_content_is_rejected_by_source_artifact_hash(
    bars_20, universe_provider_20, minute_provider_20
):
    """A frozen envelope cannot be relabelled after its source hash is bound."""
    import seal_consumption as h3_seal

    config = next(
        c
        for c in h3_seal.load_sealed_configs_and_params().configs
        if c.family == "AP-A1"
    )

    def tampered_provider(decision_ts_ms):
        evidence = universe_provider_20(decision_ts_ms)
        object.__setattr__(
            evidence.snapshot,
            "decision_ts_ms",
            decision_ts_ms + 28 * 86_400_000,
        )
        return evidence

    with pytest.raises(runner.ProviderTimeBindingError, match="hash mismatch"):
        runner._run_continuous_decisions(
            config=config,
            family="AP-A1",
            fold=_FOLD,
            bars_by_symbol=bars_20,
            universe_snapshot_provider=tampered_provider,
            minute_bars_provider=minute_provider_20,
        )


def test_mutated_minute_evidence_hash_is_rejected_fail_closed(
    bars_20, universe_provider_20, minute_provider_20
):
    import seal_consumption as h3_seal

    config = next(
        c
        for c in h3_seal.load_sealed_configs_and_params().configs
        if c.family == "AP-A1"
    )

    def tampered_minute_provider(symbol, decision_ts_ms):
        evidence = minute_provider_20(symbol, decision_ts_ms)
        object.__setattr__(
            evidence, "source_as_of_ts_ms", evidence.source_as_of_ts_ms + 60_000
        )
        return evidence

    with pytest.raises(runner.ProviderTimeBindingError, match="hash mismatch"):
        runner._run_continuous_decisions(
            config=config,
            family="AP-A1",
            fold=_FOLD,
            bars_by_symbol=bars_20,
            universe_snapshot_provider=universe_provider_20,
            minute_bars_provider=tampered_minute_provider,
        )


def test_runner_rejects_fold_id_not_bound_to_issued_fold(
    bars_20, universe_provider_20, minute_provider_20
):
    with pytest.raises(fs.FoldBindingError, match="does not match"):
        runner.run_family_fold(
            family="AP-A2",
            fold_id="fold-99",
            fold=_FOLD,
            bars_by_symbol=bars_20,
            universe_snapshot_provider=universe_provider_20,
            minute_bars_provider=minute_provider_20,
        )


def _filled(price: float) -> fm.FillOutcome:
    return fm.FillOutcome(
        filled=True,
        fill_price=price,
        fill_bar_offset=1,
        reason="FILLED",
    )


def _boundary_trade(
    *,
    entry_fill_ts_ms: int,
    exit_fill_ts_ms: int,
    exit_price: float,
) -> tl.Trade:
    entry_fill = _filled(100.0)
    exit_fill = _filled(exit_price)
    return tl.Trade(
        symbol="BTC/USD",
        config_id="AP-A1-00",
        entry_decision_ts_ms=entry_fill_ts_ms - fm.MINUTE_MS,
        entry_fill_ts_ms=entry_fill_ts_ms,
        exit_decision_ts_ms=exit_fill_ts_ms - fm.MINUTE_MS,
        exit_fill_ts_ms=exit_fill_ts_ms,
        entry_reference_close=100.0,
        exit_reference_close=exit_price,
        filled_qty=0.625,
        entry_fill=entry_fill,
        exit_fill=exit_fill,
    )


def _run_result_for_trade(trade: tl.Trade) -> runner._ContinuousRunResult:
    return runner._ContinuousRunResult(
        all_records=(),
        closed_trades=(trade,),
        open_legs_by_symbol={},
        fill_attempts=(),
        modeled_entry_evidence=(trade.entry_evidence,),
        provider_evidence_binding=pe.RunProviderEvidenceBinding(
            universe_artifacts=(),
            minute_artifacts=(),
        ),
    )


def test_train_entry_oos_exit_counts_entry_cost_but_never_train_closed_or_e120():
    """Frozen event-time table rows 2/3 in one lifecycle."""
    trade = _boundary_trade(
        entry_fill_ts_ms=_FOLD.train_end_ms - 60_000,
        exit_fill_ts_ms=_FOLD.oos_start_ms + 60_000,
        exit_price=120.0,
    )
    run_result = _run_result_for_trade(trade)
    scenarios = wf_seal.cost_scenarios_bp()

    train, train_trades = runner._build_phase_metrics(
        phase="TRAIN",
        run_result=run_result,
        fold=_FOLD,
        cost_scenarios_bp=scenarios,
        turnover_capacity_k=None,
    )
    oos, oos_trades = runner._build_phase_metrics(
        phase="OOS",
        run_result=run_result,
        fold=_FOLD,
        cost_scenarios_bp=scenarios,
        turnover_capacity_k=None,
    )

    assert train.modeled_entries_count == 1
    assert len(train.modeled_entry_evidence) == 1
    assert train.closed_trades_count == 0
    assert train.median_trade_e120_bp is None
    assert train_trades == ()
    assert oos.modeled_entries_count == 0
    assert oos.closed_trades_count == 0
    assert oos.median_trade_e120_bp is None
    assert oos_trades == ()


def test_oos_exit_price_mutation_cannot_move_train_e120_verifier_reproduction():
    scenarios = wf_seal.cost_scenarios_bp()
    observed = []
    for exit_price in (120.0, 80.0):
        trade = _boundary_trade(
            entry_fill_ts_ms=_FOLD.train_end_ms - 60_000,
            exit_fill_ts_ms=_FOLD.oos_start_ms + 60_000,
            exit_price=exit_price,
        )
        train, _trades = runner._build_phase_metrics(
            phase="TRAIN",
            run_result=_run_result_for_trade(trade),
            fold=_FOLD,
            cost_scenarios_bp=scenarios,
            turnover_capacity_k=None,
        )
        observed.append(
            (
                train.closed_trades_count,
                train.median_trade_e120_bp,
                train.modeled_entries_count,
            )
        )
    assert observed == [(0, None, 1), (0, None, 1)]


def test_complete_lifecycle_inside_train_is_included_in_closed_and_e120():
    trade = _boundary_trade(
        entry_fill_ts_ms=_FOLD.train_end_ms - 2 * 86_400_000,
        exit_fill_ts_ms=_FOLD.train_end_ms - 86_400_000,
        exit_price=120.0,
    )
    train, train_trades = runner._build_phase_metrics(
        phase="TRAIN",
        run_result=_run_result_for_trade(trade),
        fold=_FOLD,
        cost_scenarios_bp=wf_seal.cost_scenarios_bp(),
        turnover_capacity_k=None,
    )
    assert train.modeled_entries_count == 1
    assert train.closed_trades_count == 1
    assert train.median_trade_e120_bp == pytest.approx(1880.0)
    assert train_trades == (trade,)


def test_entry_fill_exactly_at_train_end_is_outside_half_open_train():
    trade = _boundary_trade(
        entry_fill_ts_ms=_FOLD.train_end_ms,
        exit_fill_ts_ms=_FOLD.oos_start_ms + 60_000,
        exit_price=120.0,
    )
    train, train_trades = runner._build_phase_metrics(
        phase="TRAIN",
        run_result=_run_result_for_trade(trade),
        fold=_FOLD,
        cost_scenarios_bp=wf_seal.cost_scenarios_bp(),
        turnover_capacity_k=None,
    )
    assert train.modeled_entries_count == 0
    assert train.closed_trades_count == 0
    assert train_trades == ()


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
        assert isinstance(cr.oos_blind_counts.reason_code_histogram, Mapping)
        # OOS PnL is masked by default (AC22) -- every entry is a real
        # Masked instance bound to this exact fold/family/config, and
        # cannot be read without evidence.
        for masked in cr.oos_masked_pnl_by_trade:
            assert isinstance(masked, om.Masked)
            assert masked.fold_id == "fold-0"
            assert masked.family == "AP-A2"
            assert masked.config_id == cr.config_id
            with pytest.raises(om.OOSMaskBypassError, match="cannot be constructed"):
                om.DryCountPassEvidence(
                    fold_id="WRONG-fold",
                    family="AP-A2",
                    config_id=cr.config_id,
                    modeled_entries=999,
                    min_modeled_entries_per_fold=5,
                    passed=True,
                )
        # TRAIN PnL (median E120) IS directly readable (AC23) -- plain
        # float or None, never masked.
        assert cr.train_metrics.median_trade_e120_bp is None or isinstance(
            cr.train_metrics.median_trade_e120_bp, float
        )
        assert len(cr.provider_evidence_binding.universe_artifacts) > 0
        assert len(cr.provider_evidence_binding.combined_hash) == 64


@pytest.mark.slow
def test_ap_a2_turnover_is_entries_over_weekly_evaluations_times_k(
    ap_a2_full_result,
):
    """Run A §7/§12.6 p-unit, not entries/all-symbol decision records."""
    import seal_consumption as h3_seal

    configs = {
        config.config_id: config
        for config in h3_seal.load_sealed_configs_and_params().configs
        if config.family == "AP-A2"
    }
    weekly_evaluations = len(
        runner._decision_timestamps(
            family="AP-A2",
            window_start_ms=_FOLD.train_start_ms,
            window_end_ms=_FOLD.train_end_ms,
        )
    )
    assert weekly_evaluations == 52
    for run in ap_a2_full_result.config_runs:
        k = int(configs[run.config_id].params["k"])
        assert run.train_metrics.turnover_p == (
            run.train_metrics.modeled_entries_count / (weekly_evaluations * k)
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
