"""ROB-960 -- wires H4's real corpus+walk-forward execution (capture-
wrapped) through H6's UNCHANGED run_full_campaign (injected, never
imported directly here -- captain plan-gate G4), producing both the H6
accounting report AND (only when real per-strategy evidence + a proven-
empty-gap corpus + valid PBO all succeeded) the strategies evidence the
CLI needs for H5's build_scorecard. Never commits (the caller owns
commit/rollback, mirroring run_rob944_campaign's own convention); never
fabricates strategies_evidence (G3/G9).

G4: ``run_empirical_campaign_with_capture`` takes ``session``/``controller``
as INJECTED parameters -- this module never imports ``app.core.db`` or
``app.services.rob944_campaign_controller`` itself, so every test of this
module's own logic is a pure fake/spy test with zero real DB coupling. The
CLI (``run_rob940_empirical_materializer.py``) is the only place that ever
constructs the real session/controller.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from rob945_capture import (
    OosSignalCaptureSink,
    expected_oos_calls_from_walkforward_result,
    wrap_config_specs_for_oos_capture,
)
from rob945_signal_concurrency import compute_signal_concurrency
from rob960_strategy_evidence import build_strategy_evidence
from run_rob944_campaign import (
    _ENV_ARTIFACT_ROOT,
    H1_MANIFEST_PATH,
    PRODUCTION_S1_STRATEGY_KEY,
    PRODUCTION_S2_STRATEGY_KEY,
    RunPreflightError,
    _assert_exact_str,
    _build_fallback_evidence_and_capture,
    _derive_primary_campaign_run_id,
    _is_empirical_success,
    _normalize_and_capture_summaries,
    _normalize_experiment_id_by_key,
    _run_precheck_bridge_and_opt_in,
    _s2_rejections_to_no_trade_records,
)


@dataclass
class EmpiricalRunOutcome:
    report: object
    attempt_evidence: list
    walkforward_results: dict | None
    strategies_evidence: dict | None
    empirical_success: bool


def _default_research_db_policy():
    from app.services.research_db_write_guard import default_research_db_policy

    return default_research_db_policy()


def _assert_gap_ranges_all_empty(gap_ranges: dict) -> None:
    """Captain plan-gate G2/C1: PBO full-window evaluation requires a
    proven-empty-gap corpus. Checked here, BEFORE any wrapped
    run_walkforward call, so a violation fails the whole strategy's corpus
    stage closed -- caught by the SAME outer except as any other corpus
    failure, collapsing to the H4 fallback batch and strategies_evidence
    staying None (never a partial/degraded scorecard)."""
    nonempty = {sym: ranges for sym, ranges in gap_ranges.items() if ranges}
    if nonempty:
        raise RunPreflightError(
            f"H1 corpus manifest reports non-empty gap_ranges for "
            f"{sorted(nonempty)!r} -- PBO full-window evaluation requires a "
            "proven-empty-gap corpus (G2); refusing to proceed"
        )


def _build_real_capture_wrapped_evidence(
    experiment_id_by_key: dict,
    *,
    full_campaign_hash: str,
    campaign_run_id: str,
    capture_summaries_into: list | None,
    walkforward_results_out: dict,
    capture_sinks_out: dict,
    corpus_cache_out: dict,
) -> list:
    """Mirrors run_rob944_campaign._build_real_attempt_evidence_inner's
    corpus-loading preamble and per-strategy generator-factory closures
    verbatim (they are private/nested there, not independently importable)
    -- the differences are the G2 empty-gap assertion, wrapping each
    strategy's ConfigSpecs for OOS signal capture (rob945_capture, already
    proven byte-identical to unwrapped by H5's own test_rob945_capture.py)
    before each run_walkforward call, and (captain direct-contract check,
    2026-07-18) snapshotting/validating the lineage args + mapping via the
    SAME frozen H4 trust-boundary functions _build_fallback_evidence_and_capture
    already applies -- _assert_exact_str + _normalize_experiment_id_by_key --
    HERE, before any corpus/walk-forward work starts, rather than deferring
    that validation to _normalize_and_capture_summaries after the first
    strategy's walk-forward has already run.
    """
    full_campaign_hash = _assert_exact_str(
        full_campaign_hash, context="lineage argument full_campaign_hash"
    )
    campaign_run_id = _assert_exact_str(
        campaign_run_id, context="lineage argument campaign_run_id"
    )
    experiment_id_by_key = _normalize_experiment_id_by_key(
        experiment_id_by_key, context="experiment_id_by_key"
    )

    import rob941_offline_loader as offline_loader
    from rob940_bars_agg import Bar1m, aggregate_complete
    from rob940_signal_manifest import FROZEN_S1_CONFIGS, FROZEN_S2_CONFIGS
    from rob940_signal_s1 import generate_s1_signals
    from rob940_signal_s2 import generate_s2_signals
    from rob941_frozen_scope import WINDOW_END_MS, WINDOW_START_MS
    from rob941_funding_sidecar import FundingSidecar
    from rob941_manifest import CorpusManifest
    from rob944_folds import generate_frozen_fold_schedule
    from rob944_walkforward import (
        ConfigSpec,
        GeneratedSignalBatch,
        run_walkforward,
        summarize_config_attempts_for_h6,
    )

    artifact_root = os.environ.get(_ENV_ARTIFACT_ROOT)
    if not artifact_root:
        raise RunPreflightError(
            f"{_ENV_ARTIFACT_ROOT} is required for --run corpus loading"
        )

    manifest = CorpusManifest.load(H1_MANIFEST_PATH)
    corpus = offline_loader.load_corpus(manifest, Path(artifact_root))

    bars_1m = {
        symbol: tuple(
            Bar1m(
                ts=r.open_time_ms,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.base_volume,
            )
            for r in rows
        )
        for symbol, rows in corpus["klines"].items()
    }
    funding_sidecars = {
        symbol: FundingSidecar.from_rows(symbol, rows)
        for symbol, rows in corpus["funding"].items()
    }
    gap_ranges = {k.symbol: k.gap_ranges for k in manifest.klines}
    _assert_gap_ranges_all_empty(gap_ranges)
    fold_schedule = generate_frozen_fold_schedule(WINDOW_START_MS, WINDOW_END_MS)

    def _s1_gen_factory(config):
        def _gen(symbol, bars_slice, fold_id):
            bars_15m = aggregate_complete(bars_slice, bucket_minutes=15)
            return generate_s1_signals(bars_15m, config, symbol=symbol, fold_id=fold_id)

        return _gen

    def _s2_gen_factory(config):
        def _gen(symbol, bars_slice, fold_id):
            bars_5m = aggregate_complete(bars_slice, bucket_minutes=5)
            gen_result = generate_s2_signals(
                bars_5m, bars_slice, config, symbol=symbol, fold_id=fold_id
            )
            return GeneratedSignalBatch(
                signals=gen_result.signals,
                rejections=_s2_rejections_to_no_trade_records(gen_result.rejections),
            )

        return _gen

    evidence: list = []
    for strategy, configs, gen_factory, strategy_key in (
        ("S1", FROZEN_S1_CONFIGS, _s1_gen_factory, PRODUCTION_S1_STRATEGY_KEY),
        ("S2", FROZEN_S2_CONFIGS, _s2_gen_factory, PRODUCTION_S2_STRATEGY_KEY),
    ):
        specs = tuple(
            ConfigSpec(config_id=c.config_id, generate_signals=gen_factory(c))
            for c in configs
        )
        sink = OosSignalCaptureSink()
        wrapped_specs = wrap_config_specs_for_oos_capture(
            specs, strategy=strategy, fold_schedule=fold_schedule, sink=sink
        )
        result = run_walkforward(
            strategy=strategy,
            configs=wrapped_specs,
            bars_1m=bars_1m,
            funding_sidecars=funding_sidecars,
            gap_ranges=gap_ranges,
            fold_schedule=fold_schedule,
        )
        sink.finalize(expected_oos_calls_from_walkforward_result(result))
        walkforward_results_out[strategy] = result
        capture_sinks_out[strategy] = sink

        summaries = summarize_config_attempts_for_h6(result)
        evidence.extend(
            _normalize_and_capture_summaries(
                summaries,
                strategy_key=strategy_key,
                experiment_id_by_key=experiment_id_by_key,
                full_campaign_hash=full_campaign_hash,
                campaign_run_id=campaign_run_id,
                capture_summaries_into=capture_summaries_into,
            )
        )

    corpus_cache_out.update(
        bars_1m=bars_1m, funding_sidecars=funding_sidecars, gap_ranges=gap_ranges
    )
    return evidence


async def run_empirical_campaign_with_capture(
    session, controller, *, expected_full_campaign_hash: str, campaign_run_id: str
) -> EmpiricalRunOutcome:
    from rob944_frozen_campaign import build_production_frozen_campaign_envelope

    envelope = build_production_frozen_campaign_envelope()
    actual_hash = envelope.full_campaign_hash()
    if actual_hash != expected_full_campaign_hash:
        raise RunPreflightError("full_campaign_hash mismatch")
    if campaign_run_id != _derive_primary_campaign_run_id(actual_hash):
        raise RunPreflightError("campaign_run_id derivation mismatch")
    _run_precheck_bridge_and_opt_in()

    plain = envelope.to_dict()
    from app.schemas.research_backtest import StrategyExperimentIdentity

    specs = [
        StrategyExperimentIdentity(
            strategy_key=row["strategy_key"],
            strategy_version=row["strategy_version"],
            hypothesis=row["hypothesis"],
            **row["components"],
        )
        for row in plain["rows"]
    ]

    walkforward_results: dict = {}
    capture_sinks: dict = {}
    corpus_cache: dict = {}
    captured_summaries: list = []
    attempt_evidence_out: list = []

    def _build_attempt_evidence(experiment_id_by_key: dict) -> list:
        try:
            evidence = _build_real_capture_wrapped_evidence(
                experiment_id_by_key,
                full_campaign_hash=actual_hash,
                campaign_run_id=campaign_run_id,
                capture_summaries_into=captured_summaries,
                walkforward_results_out=walkforward_results,
                capture_sinks_out=capture_sinks,
                corpus_cache_out=corpus_cache,
            )
        except Exception:  # noqa: BLE001 -- mirrors run_rob944_campaign._build_real_attempt_evidence's own whole-function fallback exactly
            walkforward_results.clear()
            capture_sinks.clear()
            corpus_cache.clear()
            evidence = _build_fallback_evidence_and_capture(
                experiment_id_by_key,
                full_campaign_hash=actual_hash,
                campaign_run_id=campaign_run_id,
                capture_summaries_into=captured_summaries,
            )
        attempt_evidence_out.clear()
        attempt_evidence_out.extend(evidence)
        return evidence

    report = await controller.run_full_campaign(
        session,
        specs=specs,
        actual_full_campaign_hash=actual_hash,
        expected_full_campaign_hash=expected_full_campaign_hash,
        campaign_run_id=campaign_run_id,
        guard_opt_in_enabled=True,
        guard_policy=_default_research_db_policy(),
        build_attempt_evidence=_build_attempt_evidence,
        strategy_name="rob940_walkforward",
        timeframe="mixed_5m_15m",
        # Captain direct-contract check (2026-07-18): reuse H4's exact
        # runner lineage value -- a new CLI filename is not authority to
        # introduce a new persistence lineage tag.
        runner="rob944-cli",
    )

    strategies_evidence = None
    if len(walkforward_results) == 2:
        try:
            s1_sink, s2_sink = capture_sinks["S1"], capture_sinks["S2"]
            s1_signals = () if s1_sink.is_invalid else s1_sink.snapshot()
            s2_signals = () if s2_sink.is_invalid else s2_sink.snapshot()
            concurrency = compute_signal_concurrency(
                {"S1": s1_signals, "S2": s2_signals}
            )
            strategies_evidence = {
                strategy: build_strategy_evidence(
                    strategy=strategy,
                    walkforward_result=walkforward_results[strategy],
                    capture_sink=capture_sinks[strategy],
                    signal_concurrency_evidence=concurrency.per_strategy_by_name[
                        strategy
                    ],
                    bars_1m=corpus_cache["bars_1m"],
                    funding_sidecars=corpus_cache["funding_sidecars"],
                    gap_ranges=corpus_cache["gap_ranges"],
                )
                for strategy in ("S1", "S2")
            }
        except Exception:  # noqa: BLE001 -- G3/G9: any PBO/evidence-assembly failure here collapses to strategies_evidence=None, never a partial/fabricated dict
            strategies_evidence = None

    return EmpiricalRunOutcome(
        report=report,
        attempt_evidence=[e.model_dump() for e in attempt_evidence_out],
        walkforward_results=walkforward_results or None,
        strategies_evidence=strategies_evidence,
        empirical_success=_is_empirical_success(report),
    )
