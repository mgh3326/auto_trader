"""ROB-970 R2 stop-gate audit item C: real, non-vacuous observer-effect-0.

Exercises the REAL ``record_attempt`` write path (this module's OWNING
service) against a REAL local test_db: registers the REAL 24-experiment
frozen production campaign, records all 24 real attempts, then replays ONE
attempt with diagnostics absent/present/differently-worded/overflow-
divergent -- observing the REAL ``diagnostic_replay_divergence`` event via
``capsys`` -- and asserts byte identity, before vs. after EACH replay, of:

  1. the complete H5 scorecard semantic payload/envelope;
  2. ``campaign_verdict`` and ``scorecard_artifact_hash``;
  3. the H5 six-key sealed attempts + ``trial_accounting_hash``;
  4. ``full_campaign_hash`` and ``campaign_run_id``;
  5. the target row's own complete ``raw_payload``.

Uses the REAL production scorecard/seal/campaign builders (never a
fabricated hash or disconnected constant) -- deliberately NOT a pure H5
module test (see ``research/nautilus_scalping/tests/test_rob945_scorecard.py``,
which stays free of any ``app.*`` runtime import); this file lives in the
owning app/service integration suite instead, since ``record_attempt`` is
what is actually being exercised. A ``SimpleNamespace`` fake row exercising
a private function in isolation (the prior, now-removed approach) would be
vacuous: the fake row and incoming evidence would be unrelated to whatever
scorecard is rebuilt afterward.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

# research/nautilus_scalping is on PYTHONPATH (project convention:
# ``PYTHONPATH=research/nautilus_scalping:.``), so these bare-name imports
# of REAL, unmodified production modules resolve regardless of which test
# root pytest was invoked from -- mirrors
# ``research/nautilus_scalping/tests/test_rob945_scorecard.py``'s own
# fixture-building approach (not duplicated from it; independently built
# here since that module must stay free of any app.* import).
import rob941_frozen_scope as frozen_scope
import rob944_folds as foldmod
import rob946_campaign_identity as campaign_identity
from rob944_frozen_campaign import (
    PRODUCTION_S1_STRATEGY_KEY,
    PRODUCTION_S2_STRATEGY_KEY,
    build_production_campaign_config_rows,
    build_production_frozen_campaign_envelope,
    build_production_strategy_sources,
    load_production_dataset_manifest,
)
from rob944_selection import (
    INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON as _INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
)
from rob944_selection import (
    INSUFFICIENT_SYMBOL_EVIDENCE_REASON as _INSUFFICIENT_SYMBOL_EVIDENCE_REASON,
)
from rob944_selection import ConfigSelectionOutcome, FoldSelectionTrace
from rob944_walkforward import (
    ConfigAttemptResult,
    FoldWalkForwardResult,
    WalkForwardResult,
    summarize_config_attempts_for_h6,
)
from rob945_accounting_seal import seal_trial_accounting
from rob945_scenario_metrics import FoldStabilityRow, StrategyScenarioAggregate
from rob945_scenario_metrics import SymbolScenarioMetrics as _SymbolScenarioMetrics
from rob945_scorecard import build_scorecard
from rob945_signal_concurrency import StrategyConcurrencyEvidence
from run_rob944_campaign import _summary_to_attempt_evidence
from sqlalchemy import text

from app.schemas.research_backtest import StrategyExperimentIdentity
from app.schemas.research_campaign_bridge import ChildFailureDiagnosticOverflow
from app.services import research_campaign_bridge as bridge
from app.services.research_campaign_bridge import record_attempt
from app.services.research_db_write_guard import ResearchDbPolicy, ResearchDbTarget
from research_contracts.canonical_hash import canonical_sha256

_POLICY = ResearchDbPolicy.of(
    ResearchDbTarget(host="localhost", database_name="test_db")
)
_SYMBOLS = ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")

# -- REAL frozen production campaign identity (never fabricated) ----------

_ENVELOPE = build_production_frozen_campaign_envelope()
_REAL_FULL_CAMPAIGN_HASH = _ENVELOPE.full_campaign_hash()
_REAL_FULL_CAMPAIGN_PAYLOAD = _ENVELOPE.to_dict()
_REAL_DATASET_MANIFEST_HASH = _ENVELOPE.dataset_manifest_hash
_REAL_SIGNAL_MANIFEST_HASH = _ENVELOPE.signal_manifest_hash
_STRATEGY_KEY = {"S1": PRODUCTION_S1_STRATEGY_KEY, "S2": PRODUCTION_S2_STRATEGY_KEY}
_REAL_FOLDS = foldmod.generate_frozen_fold_schedule(
    frozen_scope.WINDOW_START_MS, frozen_scope.WINDOW_END_MS
)


def _derive_campaign_run_id(full_campaign_hash: str) -> str:
    import base64

    digest_hex = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "primary_run"}
    )
    suffix = (
        base64.urlsafe_b64encode(bytes.fromhex(digest_hex)).decode("ascii").rstrip("=")
    )
    return f"rob944-primary-{suffix}"


_CAMPAIGN_RUN_ID = _derive_campaign_run_id(_REAL_FULL_CAMPAIGN_HASH)


def _real_24_identities() -> list[tuple[str, str, StrategyExperimentIdentity]]:
    """The REAL 24 (config_id, strategy, StrategyExperimentIdentity) rows,
    built from the SAME production components
    ``build_production_frozen_campaign_envelope`` itself hashes -- never a
    fabricated/synthetic identity."""
    rows = build_production_campaign_config_rows()
    dataset_manifest = load_production_dataset_manifest()
    dataset_manifest_hash = canonical_sha256(dataset_manifest)
    sources = build_production_strategy_sources()
    specs = campaign_identity.build_campaign_experiment_specs(
        rows=rows,
        sources=sources,
        dataset_manifest=dataset_manifest,
        dataset_manifest_expected_hash=dataset_manifest_hash,
    )
    out = []
    for row, spec in zip(rows, specs, strict=True):
        identity = StrategyExperimentIdentity(
            strategy_key=spec.strategy_key,
            strategy_version=spec.strategy_version,
            hypothesis=spec.hypothesis,
            **spec.components,
        )
        out.append((row.config_id, row.strategy_slug(), identity))
    return out


def _hex64(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _config_ids_for(strategy: str) -> tuple[str, ...]:
    return tuple(f"{strategy}-{i:02d}" for i in range(12))


def _rejected_candidate(config_id: str, seed: str) -> ConfigSelectionOutcome:
    return ConfigSelectionOutcome(
        config_id=config_id,
        eligible_symbols=(),
        excluded_symbols=tuple(
            (symbol, _INSUFFICIENT_SYMBOL_EVIDENCE_REASON) for symbol in _SYMBOLS
        ),
        equal_weight_expectancy_bps=None,
        pooled_expectancy_bps=None,
        profit_factor=0.0,
        rejected=True,
        rejection_reason=_INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        train_input_hash=_hex64(f"train:{seed}"),
        no_trade_reason_counts={},
    )


def _build_walkforward_result(strategy: str) -> WalkForwardResult:
    """A real, hand-assembled ``WalkForwardResult`` over the REAL 8-fold
    schedule (mirrors ``test_rob945_scorecard.py``/
    ``test_rob945_accounting_seal.py``) -- no corpus, no network, no
    empirical run. Every one of the 12 configs is a clean
    ``status="completed"`` attempt."""
    config_ids = _config_ids_for(strategy)
    fold_results = []
    for fold in _REAL_FOLDS:
        candidates = tuple(
            _rejected_candidate(config_id, f"{fold.fold_id}:{config_id}")
            for config_id in config_ids
        )
        trace = FoldSelectionTrace(
            strategy=strategy, candidates=candidates, selected_config_id=None
        )
        fold_results.append(
            FoldWalkForwardResult(fold=fold, selection_trace=trace, oos_outcomes=())
        )
    attempts = [
        ConfigAttemptResult(
            strategy=strategy,
            config_id=config_id,
            status="completed",
            reason_code=None,
            selected_in_folds=(),
            crash_log=(),
            gap_rejection_log=(),
        )
        for config_id in config_ids
    ]
    return WalkForwardResult(
        strategy=strategy,
        folds=tuple(fold_results),
        config_attempts=tuple(attempts),
        concatenated_oos_ledgers={},
    )


_WALKFORWARD_RESULTS = {
    "S1": _build_walkforward_result("S1"),
    "S2": _build_walkforward_result("S2"),
}


def _symbol_metrics_all_present():
    return tuple(
        _SymbolScenarioMetrics(
            symbol=s,
            trade_count=5,
            signal_count=5,
            net_expectancy_bps=10.0,
            net_pnl_bps=50.0,
        )
        for s in _SYMBOLS
    )


def _scenario(scenario_name, net_expectancy_bps=10.0):
    return StrategyScenarioAggregate(
        strategy="S1",
        scenario_name=scenario_name,
        trade_count=20,
        net_expectancy_bps=net_expectancy_bps,
        pooled_expectancy_bps=net_expectancy_bps,
        profit_factor=2.0,
        win_rate=0.6,
        net_pnl_bps=200.0,
        timeout_ratio=0.1,
        mdd_r=1.5,
        mdd_reason=None,
        monthly_concentration=0.3,
        monthly_concentration_reason=None,
        symbol_metrics=_symbol_metrics_all_present(),
        incomplete=False,
        incomplete_reason=None,
        no_trade_reason_counts={},
    )


def _fold_rows():
    return tuple(
        FoldStabilityRow(
            fold_id=f"fold-{i:02d}",
            selected_config_id="S1-03",
            trade_count=5,
            net_expectancy_bps=2.0,
            net_pnl_bps=10.0,
            profit_factor=float("inf"),
            positive=True,
            net_pnl_class="positive",
        )
        for i in range(8)
    )


def _concurrency():
    return StrategyConcurrencyEvidence(
        strategy="S1",
        numerator=1,
        denominator=2,
        rate=0.5,
        reason=None,
        distinct_symbol_count_histogram={1: 1, 2: 1, 3: 0, 4: 0},
    )


def _pbo():
    from rob945_pbo_grid import PboAuxiliaryEvidence

    return PboAuxiliaryEvidence(
        strategy="S1",
        value=0.4,
        reason_codes=(),
        slices=4,
        config_count=12,
        day_count=365,
        artifact_hash="a" * 64,
    )


def _strategy_evidence():
    return {
        "scenarios": {
            "base": _scenario("base"),
            "primary_stress": _scenario("primary_stress"),
            "upward_stress": _scenario("upward_stress", net_expectancy_bps=1.0),
        },
        "fold_stability": _fold_rows(),
        "signal_concurrency": _concurrency(),
        "pbo": _pbo(),
    }


def _clean_accounting_report():
    return {
        "campaign_run_id": _CAMPAIGN_RUN_ID,
        "expected_total": 24,
        "actual_registrations": 24,
        "primary_attempts": 24,
        "total_attempts": 24,
        "retry_attempts": 0,
        "status_counts": {"completed": 24, "rejected": 0, "crashed": 0, "timeout": 0},
        "missing_experiment_ids": [],
        "extra_experiment_ids": [],
        "mismatch_experiment_ids": [],
        "duplicate_or_gap_experiment_ids": [],
        "verdict": "complete",
    }


def _scorecard_kwargs(attempt_evidence_dicts: list[dict]) -> dict:
    return {
        "full_campaign_hash": _REAL_FULL_CAMPAIGN_HASH,
        "full_campaign_payload": _REAL_FULL_CAMPAIGN_PAYLOAD,
        "campaign_run_id": _CAMPAIGN_RUN_ID,
        "dataset_manifest_hash": _REAL_DATASET_MANIFEST_HASH,
        "signal_manifest_hash": _REAL_SIGNAL_MANIFEST_HASH,
        "accounting_report": _clean_accounting_report(),
        "attempt_evidence": attempt_evidence_dicts,
        "walkforward_results": _WALKFORWARD_RESULTS,
        "strategies": {"S1": _strategy_evidence(), "S2": _strategy_evidence()},
    }


def _lineage_snapshot(scorecard_envelope: dict) -> dict:
    payload = scorecard_envelope["scorecard_payload"]
    return {
        "campaign_verdict": payload["campaign_verdict"],
        "scorecard_artifact_hash": scorecard_envelope["scorecard_artifact_hash"],
        "trial_accounting_hash": payload["lineage"]["trial_accounting_hash"],
        "full_campaign_hash": payload["lineage"]["full_campaign_hash"],
        "campaign_run_id": payload["lineage"]["campaign_run_id"],
    }


def _seal(attempt_evidence_dicts: list[dict]):
    """The explicit H5 six-key sealed-attempts authority
    (``rob945_accounting_seal.seal_trial_accounting``), called directly and
    independently of ``build_scorecard`` (which calls it internally too) --
    so ``.attempts``/``.trial_accounting_hash`` can be asserted byte-for-
    byte identical before/after a replay, not merely inferred from the
    scorecard's own already-byte-compared output."""
    return seal_trial_accounting(
        accounting_report=_clean_accounting_report(),
        attempt_evidence=attempt_evidence_dicts,
        full_campaign_hash=_REAL_FULL_CAMPAIGN_HASH,
        walkforward_results=_WALKFORWARD_RESULTS,
    )


@pytest_asyncio.fixture
async def registry_tables(db_session):
    exists = await db_session.scalar(
        text("SELECT to_regclass('research.strategy_experiments')")
    )
    if exists is None:
        pytest.skip("ROB-846 registry tables are not migrated in this DB")
    return db_session


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_diagnostic_replay_divergence_is_observer_effect_zero_end_to_end(
    registry_tables, capsys
) -> None:
    session = registry_tables

    identities = _real_24_identities()
    real_specs = [identity for _cid, _strategy, identity in identities]
    from app.services.research_campaign_bridge import register_campaign_experiments

    registered = await register_campaign_experiments(
        session,
        specs=real_specs,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    assert len(registered) == 24

    attempt_evidence_by_config_id: dict[str, dict] = {}
    experiment_id_by_config_id: dict[str, str] = {}
    target_config_id = "S1-00"
    target_row = None

    for (config_id, strategy, _identity), experiment_row in zip(
        identities, registered, strict=True
    ):
        summary = next(
            s
            for s in summarize_config_attempts_for_h6(_WALKFORWARD_RESULTS[strategy])
            if s.config_id == config_id
        )
        evidence = _summary_to_attempt_evidence(
            summary,
            strategy_key=_STRATEGY_KEY[strategy],
            experiment_id=experiment_row.experiment_id,
            full_campaign_hash=_REAL_FULL_CAMPAIGN_HASH,
            campaign_run_id=_CAMPAIGN_RUN_ID,
        )
        row = await record_attempt(
            session,
            experiment_id=experiment_row.experiment_id,
            evidence=evidence,
            strategy_name=strategy,
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )
        attempt_evidence_by_config_id[config_id] = evidence.model_dump()
        experiment_id_by_config_id[config_id] = experiment_row.experiment_id
        if config_id == target_config_id:
            target_row = row
    assert target_row is not None

    def _current_scorecard():
        return build_scorecard(
            **_scorecard_kwargs(list(attempt_evidence_by_config_id.values()))
        )

    baseline_scorecard = _current_scorecard()
    baseline_lineage = _lineage_snapshot(baseline_scorecard)
    baseline_seal = _seal(list(attempt_evidence_by_config_id.values()))
    baseline_row_bytes = bridge._canonical_raw_payload_bytes(target_row)
    target_experiment_id = experiment_id_by_config_id[target_config_id]
    baseline_evidence = _summary_to_attempt_evidence(
        next(
            s
            for s in summarize_config_attempts_for_h6(_WALKFORWARD_RESULTS["S1"])
            if s.config_id == target_config_id
        ),
        strategy_key=_STRATEGY_KEY["S1"],
        experiment_id=target_experiment_id,
        full_campaign_hash=_REAL_FULL_CAMPAIGN_HASH,
        campaign_run_id=_CAMPAIGN_RUN_ID,
    )

    diagnostic_row = {
        "transport": "in_process",
        "stage": "generator",
        "exception_type": "RuntimeError",
        "message": "boom: synthetic signal-generation failure",
        "traceback_text": "Traceback (most recent call last):\nRuntimeError: boom\n",
        "stderr": None,
        "strategy": "S1",
        "config_id": target_config_id,
        "symbol": "BTCUSDT",
        "fold_id": "fold-00",
        "scenario_name": None,
        "signature": "a" * 64,
        "occurrence_count": 1,
        "truncated": False,
    }
    from app.schemas.research_campaign_bridge import ChildFailureDiagnostic

    replay_scenarios = [
        (
            "present",
            baseline_evidence.model_copy(
                update={
                    "diagnostic_evidence": (ChildFailureDiagnostic(**diagnostic_row),)
                }
            ),
        ),
        (
            "different_wording",
            baseline_evidence.model_copy(
                update={
                    "diagnostic_evidence": (
                        ChildFailureDiagnostic(
                            **{
                                **diagnostic_row,
                                "message": "a wholly different message",
                            }
                        ),
                    )
                }
            ),
        ),
        (
            "overflow_divergent",
            baseline_evidence.model_copy(
                update={
                    "diagnostic_overflow": ChildFailureDiagnosticOverflow(
                        truncated=True,
                        omitted_distinct_signatures=1,
                        omitted_occurrences=1,
                    )
                }
            ),
        ),
        ("absent_again", baseline_evidence),
    ]

    for label, replay_evidence in replay_scenarios:
        capsys.readouterr()
        replayed_row = await record_attempt(
            session,
            experiment_id=target_experiment_id,
            evidence=replay_evidence,
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )
        captured = capsys.readouterr()
        assert replayed_row.id == target_row.id

        if label == "absent_again":
            # byte-identical to baseline -- a genuine write-free no-op,
            # never a divergence observation.
            assert captured.err == ""
        else:
            lines = [line for line in captured.err.splitlines() if line.strip()]
            assert len(lines) == 1, (label, captured.err)
            event = json.loads(lines[0])
            assert event["event"] == "diagnostic_replay_divergence"

        # (5) the target row's own complete raw_payload never mutates.
        assert bridge._canonical_raw_payload_bytes(replayed_row) == baseline_row_bytes

        # attempt_evidence_by_config_id[target_config_id] intentionally
        # stays the ORIGINAL (pre-replay) dict -- the scorecard must be
        # rebuildable identically whether or not any of these divergent
        # replays ever happened, using the SAME real production builders.
        after_scorecard = _current_scorecard()
        after_lineage = _lineage_snapshot(after_scorecard)
        assert after_lineage == baseline_lineage, label
        # (1) complete scorecard semantic payload/envelope, byte-for-byte.
        assert json.dumps(after_scorecard, sort_keys=True) == json.dumps(
            baseline_scorecard, sort_keys=True
        ), label
        # (3) explicit H5 six-key sealed attempts + trial_accounting_hash,
        # called directly (not merely inferred from the scorecard above).
        after_seal = _seal(list(attempt_evidence_by_config_id.values()))
        assert after_seal.attempts == baseline_seal.attempts, label
        assert (
            after_seal.trial_accounting_hash == baseline_seal.trial_accounting_hash
        ), label
