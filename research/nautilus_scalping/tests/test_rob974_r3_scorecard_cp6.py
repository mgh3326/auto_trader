"""ROB-974 R3 M4/CP6 scorecard and paired-artifact contract tests.

The first slice deliberately uses the production plan's real exact-12 row
identities and all eight real folds.  It exercises only pure, in-memory
surfaces: no corpus, database, network, broker, order, fill, or publication
boundary is reachable from this test module.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import math
import subprocess
import sys
from functools import lru_cache

import pytest
import rob974_r3_scorecard as scorecard_module
from funding_oi_archive import FundingRow
from rob941_funding_sidecar import FundingSidecar
from rob974_features import CommonSnapshot, SymbolFeature
from rob974_h2_dtos import S3EngineResult
from rob974_h2_scenarios import PATH_SCENARIOS
from rob974_h3_manifest import SYMBOLS
from rob974_h3_s4 import HISTORICAL_NOTIONAL_ASSUMPTION, S4Candidate
from rob974_h4_adapter import (
    SealedS3Terminal,
    seal_s3_engine_input,
    seal_s3_engine_output,
)
from rob974_h6a_accounting import AttemptAccountingRow
from rob974_r3_accounting import build_exact_12_accounting
from rob974_r3_evidence_context import issue_r3_production_evidence_context
from rob974_r3_gate_adapter import (
    build_production_gate_campaign_evidence,
    production_gate_audit_scope,
)
from rob974_r3_gate_metrics import (
    S3_GATE_SCHEMA,
    S4_GATE_SCHEMA,
    GateAuditBatch,
    build_gate_audit,
)
from rob974_r3_h3_adapter import adapt_r3_s4_candidate_for_execution
from rob974_r3_h4_s4_adapter import (
    SealedR3S4Terminal,
    seal_r3_s4_engine_input,
    seal_r3_s4_engine_output,
)
from rob974_r3_manifest import FROZEN_R3_ROSTER, R3S3Config, R3S4Config
from rob974_r3_plan import build_production_r3_plan
from rob974_r3_relaxation import PhaseLedgerEvidence
from rob974_r3_relaxation_h2_adapter import (
    R3H2CellFoldInput,
    normalize_r3_phase_ledgers,
)
from rob974_r3_s4_dtos import R3S4EngineResult, R3S4PairTrade
from rob974_r3_scorecard import (
    R3_SCORECARD_SCHEMA_VERSION,
    R3CellOOSInput,
    R3FoldOOSInput,
    R3ScorecardAccountingEvidence,
    R3ScorecardRelaxationEvidence,
    _common_full_gate_passed,
    _family_verdict,
    build_r3_artifact_pair,
    build_r3_scorecard,
    canonical_r3_json_bytes,
    hash_r3_canonical_bytes,
    issue_r3_all_cell_oos_ledger,
    issue_r3_fold_scenario_attribution,
    issue_r3_market_input_authority,
    issue_r3_scorecard_accounting,
    issue_r3_scorecard_relaxation_evidence,
    verify_r3_artifact_pair,
)


@lru_cache(maxsize=1)
def _production_context():
    return issue_r3_production_evidence_context(build_production_r3_plan())


@lru_cache(maxsize=1)
def _unit_market_authority():
    """Issue a unit-size authority without touching the production corpus."""

    from rob974_features import MinuteBar

    from app.services.rob974_h6b_materializer import ActualH4InputData
    from research_contracts.canonical_hash import canonical_sha256

    context = _production_context()
    component = scorecard_module._production_dataset_component(context)
    minute_ts = component["window_start_ms"]
    minute = MinuteBar(minute_ts, 1.0, 1.0, 1.0, 1.0, 1.0)
    actual_data = ActualH4InputData.from_mapping(
        dict.fromkeys(SYMBOLS, (minute,)),
        corpus_end_ts=component["window_end_ms"],
        persisted_corpus_hash=component["content_sha256"],
        persisted_feature_hash=canonical_sha256([]),
    )
    snapshots = ()
    sidecars = tuple(FundingSidecar.from_rows(symbol, ()) for symbol in SYMBOLS)
    funding_hashes = tuple((symbol, canonical_sha256([])) for symbol in SYMBOLS)
    original = scorecard_module._validate_market_input_and_derive

    def unit_market_derivation(**_kwargs):
        return (
            component["content_sha256"],
            canonical_sha256([]),
            snapshots,
            sidecars,
            funding_hashes,
        )

    scorecard_module._validate_market_input_and_derive = unit_market_derivation
    try:
        return issue_r3_market_input_authority(
            evidence_context=context,
            actual_h4_input_data=actual_data,
        )
    finally:
        scorecard_module._validate_market_input_and_derive = original


def _empty_source(config, fold) -> R3H2CellFoldInput:
    if type(config) is R3S3Config:
        result = S3EngineResult((), (), ())
        terminal = SealedS3Terminal(
            result,
            seal_s3_engine_input(
                (),
                corpus_end_ts=fold.oos_end_ms,
                horizon_end_ts=fold.oos_end_ms,
            ),
            seal_s3_engine_output(result),
        )
    else:
        result = R3S4EngineResult((), (), ())
        terminal = SealedR3S4Terminal(
            result,
            seal_r3_s4_engine_input(
                (),
                corpus_end_ts=fold.oos_end_ms,
                horizon_end_ts=fold.oos_end_ms,
            ),
            seal_r3_s4_engine_output(result),
        )
    return R3H2CellFoldInput(
        config=config,
        fold_id=fold.fold_id,
        h3_candidates=(),
        engine_intents=(),
        corpus_end_ts=fold.oos_end_ms,
        horizon_end_ts=fold.oos_end_ms,
        terminal=terminal,
    )


@lru_cache(maxsize=1)
def _empty_cells() -> tuple[R3CellOOSInput, ...]:
    plan = build_production_r3_plan()
    market_authority = _unit_market_authority()
    return tuple(
        R3CellOOSInput(
            config_id=config.config_id,
            folds=tuple(
                R3FoldOOSInput(
                    scenario_attributions=tuple(
                        issue_r3_fold_scenario_attribution(
                            path_scenario=scenario,
                            source=_empty_source(config, fold),
                            market_input_authority=market_authority,
                        )
                        for scenario in PATH_SCENARIOS
                    ),
                )
                for fold in plan.folds
            ),
        )
        for config in FROZEN_R3_ROSTER
    )


def _complete_accounting(context) -> R3ScorecardAccountingEvidence:
    from app.services.rob974_r3_h6a_bridge import R3AttemptBatchItem
    from research_contracts.canonical_hash import canonical_sha256

    attempts = []
    for row_id, experiment_id in context.ordered_mapping:
        cells = [
            {
                "phase": phase,
                "fold_id": fold.fold_id,
                "accepted_decision_units": 0,
                "path_evidence": [
                    {
                        "path_scenario": scenario,
                        "input_seal_sha256": canonical_sha256(
                            [row_id, phase, fold.fold_id, "input"]
                        ),
                        "output_seal_sha256": canonical_sha256(
                            [row_id, phase, fold.fold_id, "output"]
                        ),
                        "member_trade_keys": [],
                        "basket_trades": 0,
                        "no_trades": 0,
                        "incompletes": 0,
                        "terminal_incomplete_rows": [],
                    }
                    for scenario in PATH_SCENARIOS
                ],
            }
            for phase in ("TRAIN", "OOS")
            for fold in context.folds
        ]
        payload = {
            "schema_version": "rob974.r3.h6a.attempt_evidence.v1",
            "row_id": row_id,
            "phase_fold_paths": cells,
            "section5_gate_report_sha256": {
                "TRAIN": canonical_sha256([row_id, "TRAIN"]),
                "OOS": canonical_sha256([row_id, "OOS"]),
            },
            "primary_relaxation_path": PATH_SCENARIOS[1],
            "path_scenarios": list(PATH_SCENARIOS),
            "funding_gate_projection": "once_before_three_fresh_engines",
        }
        fold_hash = canonical_sha256(payload)
        attempts.append(
            R3AttemptBatchItem(
                row_id=row_id,
                experiment_id=experiment_id,
                retry_index=0,
                status="completed",
                reason_code=None,
                fold_evidence_hash=fold_hash,
                run_identity=canonical_sha256(
                    {
                        "full_campaign_hash": context.campaign_identity_sha256,
                        "campaign_run_id": context.campaign_run_id,
                        "row_id": row_id,
                        "experiment_id": experiment_id,
                        "fold_evidence_hash": fold_hash,
                    }
                ),
                evidence_payload=payload,
            )
        )
    return issue_r3_scorecard_accounting(
        evidence_context=context,
        attempts=tuple(attempts),
    )


def _complete_gate_evidence(context):
    reports = []
    for config in FROZEN_R3_ROSTER:
        for phase in ("TRAIN", "OOS"):
            scope = production_gate_audit_scope(
                evidence_context=context,
                config=config,
                phase=phase,
            )
            schema = S3_GATE_SCHEMA if type(config) is R3S3Config else S4_GATE_SCHEMA
            batches = tuple(
                GateAuditBatch(
                    scope=scope,
                    fold_id=fold.fold_id,
                    gate_schema=schema,
                    evaluated_decision_units=0,
                    context_valid_denominator=0,
                    required_context_failures=0,
                    units=(),
                )
                for fold in context.folds
            )
            reports.append(build_gate_audit(expected_scope=scope, batches=batches))
    return build_production_gate_campaign_evidence(
        evidence_context=context,
        reports=tuple(reports),
    )


def _complete_relaxation(context, ledger) -> R3ScorecardRelaxationEvidence:
    evidence = PhaseLedgerEvidence(
        phase="OOS",
        ledgers=tuple(
            fold.source_ledger for cell in ledger.cells for fold in cell.folds
        ),
        terminal_incompletes=(),
    )
    return issue_r3_scorecard_relaxation_evidence(
        evidence_context=context,
        oos_evidence=evidence,
    )


@lru_cache(maxsize=1)
def _complete_zero_scorecard():
    context = _production_context()
    ledger = issue_r3_all_cell_oos_ledger(
        evidence_context=context,
        cells=_empty_cells(),
    )
    accounting = _complete_accounting(context)
    gate_evidence = _complete_gate_evidence(context)
    relaxation = _complete_relaxation(context, ledger)
    scorecard = build_r3_scorecard(
        evidence_context=context,
        accounting=accounting,
        oos_ledger=ledger,
        gate_evidence=gate_evidence,
        relaxation_evidence=relaxation,
    )
    return context, ledger, accounting, gate_evidence, relaxation, scorecard


def _snapshot(decision_ts: int, market_return: float = 0.04) -> CommonSnapshot:
    features = tuple(
        SymbolFeature(
            symbol=symbol,
            decision_ts=decision_ts,
            r=0.0,
            tr=0.01,
            atr20=0.01,
            a=0.01,
            vwap12=1.0,
            vwap24=1.0,
            percentile_30d=50.0,
            range24=0.02,
        )
        for symbol in SYMBOLS
    )
    return CommonSnapshot(
        decision_ts=decision_ts,
        m=0.01,
        M=market_return,
        bplus=3,
        bminus=0,
        features=features,
    )


def _s4_candidate(config: R3S4Config, decision_ts: int) -> S4Candidate:
    return S4Candidate(
        strategy="S4",
        config_id=config.config_id,
        decision_ts=decision_ts,
        pair="XRP-DOGE",
        side="short_a_long_b",
        symbol_a="XRPUSDT",
        symbol_b="DOGEUSDT",
        side_a="short",
        side_b="long",
        beta_a=1.0,
        beta_b=1.0,
        weight_a=0.5,
        weight_b=0.5,
        mu=0.0,
        mad=0.01,
        effective_mad_scale=0.014826,
        observed_z=config.z_entry,
        prior_observed_z=config.z_entry + 0.2,
        D_fraction=0.02,
        D_bps=200.0,
        rho=0.60,
        half_life_4h_bars=2.0,
        beta_stability=0.20,
        sigma_pair_risk=0.01,
        observed_pair_return_fraction=-0.001,
        gross_notional_usd=12.0,
        notional_a_usd=6.0,
        notional_b_usd=6.0,
        d_SL=0.01,
        d_TP=0.015,
        historical_notional_assumption=HISTORICAL_NOTIONAL_ASSUMPTION,
        historical_eligibility=True,
        historical_eligibility_authority=(
            "rob974_h1_parent_manifest_selected_universe"
        ),
        volatility_percentile=None,
        volatility_percentile_provenance="not_defined_for_s4",
        entry_tick_ts=decision_ts,
        entry_deadline_ts=decision_ts + 60_000,
        max_hold_4h_bars=9,
        leg_a_order_id=None,
        leg_b_order_id=None,
        leg_a_fill_id=None,
        leg_b_fill_id=None,
        pair_executor_provenance="not_evaluated_h3_generator",
    )


def _s4_trade(candidate: S4Candidate, fold_id: str) -> R3S4PairTrade:
    entry_ts = candidate.decision_ts + 60_000
    return R3S4PairTrade(
        pair=(candidate.symbol_a, candidate.symbol_b),
        side_a="short",
        side_b="long",
        config_id=candidate.config_id,
        fold_id=fold_id,
        signal_ts=candidate.decision_ts,
        entry_ts=entry_ts,
        weight_a=0.5,
        weight_b=0.5,
        beta_a=1.0,
        beta_b=1.0,
        mu=0.0,
        sigma=candidate.effective_mad_scale,
        observed_z=candidate.observed_z,
        z_threshold=candidate.observed_z,
        gross_notional=12.0,
        entry_price_a=1.0,
        entry_price_b=1.0,
        exit_ts=entry_ts + 600 * 60_000,
        exit_price_a=0.99,
        exit_price_b=1.01,
        exit_reason="TP",
        mfe_bps=60.0,
        mae_bps=-30.0,
        gross_bps=50.0,
        order_id_a=None,
        order_id_b=None,
        pair_exec_status="historical_atomic_assumption",
        pair_executor_validated=False,
        demo_eligible=False,
        volatility_percentile=None,
        volatility_percentile_provenance="not_defined_for_s4",
    )


def _s4_source_and_snapshots(config: R3S4Config, fold, count: int = 1):
    candidates = tuple(
        _s4_candidate(config, fold.oos_start_ms + (index + 1) * 900_000)
        for index in range(count)
    )
    intents = tuple(
        adapt_r3_s4_candidate_for_execution(candidate, fold_id=fold.fold_id)
        for candidate in candidates
    )
    trades = tuple(_s4_trade(candidate, fold.fold_id) for candidate in candidates)
    result = R3S4EngineResult(trades, (), ())
    terminal = SealedR3S4Terminal(
        result=result,
        input_seal_sha256=seal_r3_s4_engine_input(
            intents,
            corpus_end_ts=fold.oos_end_ms,
            horizon_end_ts=fold.oos_end_ms,
        ),
        output_seal_sha256=seal_r3_s4_engine_output(result),
    )
    source = R3H2CellFoldInput(
        config=config,
        fold_id=fold.fold_id,
        h3_candidates=candidates,
        engine_intents=intents,
        corpus_end_ts=fold.oos_end_ms,
        horizon_end_ts=fold.oos_end_ms,
        terminal=terminal,
    )
    return source, tuple(_snapshot(candidate.decision_ts) for candidate in candidates)


def test_production_real_incomplete_scorecard_is_exact_12_and_research_null() -> None:
    context = _production_context()
    ledger = issue_r3_all_cell_oos_ledger(
        evidence_context=context,
        cells=_empty_cells(),
    )
    accounting = build_exact_12_accounting(
        campaign_run_id=context.campaign_run_id,
        ordered_mapping=context.ordered_mapping,
        registered_total=0,
        attempts=(),
    )

    scorecard = build_r3_scorecard(
        evidence_context=context,
        accounting=accounting,
        oos_ledger=ledger,
        gate_evidence=None,
        relaxation_evidence=None,
    )

    assert scorecard["schema_version"] == R3_SCORECARD_SCHEMA_VERSION
    assert [cell["config_id"] for cell in scorecard["cells"]] == [
        row.config_id for row in FROZEN_R3_ROSTER
    ]
    assert len(scorecard["cells"]) == 12
    assert scorecard["lineage"]["campaign_identity_sha256"] == (
        context.campaign_identity_sha256
    )
    assert len(scorecard["lineage"]["campaign_identity_sha256"]) == 64
    assert all(len(experiment_id) == 64 for _, experiment_id in context.ordered_mapping)
    assert len(context.folds) == 8
    assert scorecard["campaign_verdict"]["operational_status"] == "INCOMPLETE"
    assert scorecard["campaign_verdict"]["research_decision"] is None
    assert all(
        row["status"] == "INCOMPLETE" for row in scorecard["section3_falsification"]
    )


def test_issued_all_cell_ledger_rejects_reordered_production_cells() -> None:
    context = _production_context()
    cells = _empty_cells()
    with pytest.raises(ValueError, match="canonical exact-12 order"):
        issue_r3_all_cell_oos_ledger(
            evidence_context=context,
            cells=(cells[1], cells[0], *cells[2:]),
        )


def test_accepted_count_is_derived_only_from_sealed_h3_candidates() -> None:
    assert "accepted_count" not in {
        field.name for field in dataclasses.fields(R3FoldOOSInput)
    }
    with pytest.raises(TypeError, match="accepted_count"):
        R3FoldOOSInput(  # type: ignore[call-arg]
            scenario_attributions=(),
            accepted_count=1,
        )


def test_canonical_json_and_markdown_are_semantically_paired_in_memory() -> None:
    context = _production_context()
    scorecard = build_r3_scorecard(
        evidence_context=context,
        accounting=build_exact_12_accounting(
            campaign_run_id=context.campaign_run_id,
            ordered_mapping=context.ordered_mapping,
            registered_total=0,
            attempts=(),
        ),
        oos_ledger=issue_r3_all_cell_oos_ledger(
            evidence_context=context,
            cells=_empty_cells(),
        ),
        gate_evidence=None,
        relaxation_evidence=None,
    )

    canonical = canonical_r3_json_bytes(scorecard)
    semantic_sha256 = hash_r3_canonical_bytes(canonical)
    pair = build_r3_artifact_pair(scorecard)

    assert pair.json_bytes == canonical
    assert pair.semantic_sha256 == semantic_sha256
    assert len(pair.semantic_sha256) == len(pair.markdown_sha256) == 64
    assert (
        verify_r3_artifact_pair(
            json_bytes=pair.json_bytes,
            markdown_bytes=pair.markdown_bytes,
        )["schema_version"]
        == R3_SCORECARD_SCHEMA_VERSION
    )

    mismatched = dataclasses.replace(
        pair,
        markdown_bytes=pair.markdown_bytes.replace(
            b"research_decision: null", b"research_decision: CONTINUE"
        ),
    )
    assert mismatched.markdown_bytes != pair.markdown_bytes
    with pytest.raises(ValueError, match="Markdown semantic mismatch"):
        verify_r3_artifact_pair(
            json_bytes=mismatched.json_bytes,
            markdown_bytes=mismatched.markdown_bytes,
        )


def test_complete_sealed_zero_trade_scorecard_is_honest_and_terminates() -> None:
    _context, _ledger, accounting, gates, relaxation, scorecard = (
        _complete_zero_scorecard()
    )

    assert scorecard["operational"] == {
        "status": "COMPLETE",
        "incomplete_reasons": [],
    }
    assert scorecard["exact_12_accounting"]["attempt_authority"] == (
        "code_issued_exact_attempts"
    )
    assert len(scorecard["exact_12_accounting"]["attempts"]) == 12
    assert accounting.report.performance_usable is True
    assert gates.evidence_promoted is True
    assert relaxation.analysis.evidence_promoted is True
    assert scorecard["section5_gate_audit"]["status"] == "COMPLETE"
    assert len(scorecard["section5_gate_audit"]["reports"]) == 24
    assert len(scorecard["section5_gate_audit"]["evidence_cell_order"]) == 192
    assert scorecard["section7_relaxation"]["status"] == "COMPLETE"
    assert scorecard["section7_relaxation"]["oos"] is not None
    assert scorecard["campaign_verdict"]["research_decision"] == "TERMINATE"
    assert [row["research_decision"] for row in scorecard["family_verdicts"]] == [
        "TERMINATE",
        "TERMINATE",
    ]
    assert "safety" not in scorecard

    for cell in scorecard["cells"]:
        assert cell["operational_status"] == "COMPLETE"
        assert cell["research_eligible"] is True
        assert cell["accepted_total"] == 0
        assert cell["basket_trades_total"] == 0
        assert cell["positive_oos_folds"] == 0
        assert cell["full_gate_passed"] is False
        assert cell["pbo"] == {
            "value": None,
            "status": "NOT_OBSERVED",
            "reason": "pbo_not_available_from_frozen_r3_inputs",
        }
        for metric in cell["economics"].values():
            assert metric == {"value": None, "reason": "zero_oos_basket_trades"}
        assert cell["observed_win_rate"] == {
            "value": None,
            "reason": "zero_oos_basket_trades",
        }
        assert cell["weighted_p_be"] == {
            "value": None,
            "reason": "zero_oos_basket_trades",
        }


def test_markdown_exposes_every_preregistered_operator_surface() -> None:
    scorecard = _complete_zero_scorecard()[-1]
    markdown = build_r3_artifact_pair(scorecard).markdown_bytes.decode("utf-8")

    for heading in (
        "## Preregistered Per-Cell Diagnostics",
        "## Exit/Timeout and Symbol/Pair Attribution",
        "## §5 Gate Audit",
        "## §7 Relaxation",
        "## Family Verdicts",
        "## §8 Campaign Verdict",
    ):
        assert heading in markdown
    for label in (
        "accepted by fold",
        "trades by fold",
        "positive folds",
        "month concentration",
        "conversion",
        "observed win",
        "weighted pBE",
        "PBO",
        "report_count: 24",
        "evidence_cell_count: 192",
    ):
        assert label in markdown
    assert "empirical_runs" not in markdown
    assert "publication_calls" not in markdown


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({}, True),
        ({"minimum_fold_trades": 4}, False),
        ({"e0_bps": math.nextafter(25.0, -math.inf)}, False),
        ({"e17_bps": math.nextafter(5.0, -math.inf)}, False),
        ({"pf17_passed": False}, False),
        ({"positive_folds": 4}, False),
        ({"monthly_concentration": math.nextafter(0.50, math.inf)}, False),
        ({"e22_up_bps": 0.0}, False),
        ({"win_margin": math.nextafter(0.03, -math.inf)}, False),
        ({"strategy_passed": False}, False),
    ),
)
def test_full_gate_exact_thresholds_are_frozen(
    changes: dict[str, object], expected: bool
) -> None:
    values = {
        "minimum_fold_trades": 5,
        "e0_bps": 25.0,
        "e17_bps": 5.0,
        "pf17_passed": True,
        "positive_folds": 5,
        "monthly_concentration": 0.50,
        "e22_up_bps": math.nextafter(0.0, math.inf),
        "win_margin": 0.03,
        "strategy_passed": True,
    }
    values.update(changes)
    assert _common_full_gate_passed(**values) is expected  # type: ignore[arg-type]


def _verdict_cells() -> list[dict[str, object]]:
    return [
        {
            "config_id": config.config_id,
            "family": config.config_id[:2],
            "sample_and_e0_qualified": False,
            "full_gate_passed": False,
        }
        for config in FROZEN_R3_ROSTER
    ]


def _set_verdict_cell(
    cells: list[dict[str, object]],
    config_id: str,
    *,
    sample_and_e0: bool,
    full: bool,
) -> None:
    cell = next(row for row in cells if row["config_id"] == config_id)
    cell["sample_and_e0_qualified"] = sample_and_e0
    cell["full_gate_passed"] = full


def test_continue_requires_exact_manifest_adjacency_and_one_full_member() -> None:
    cells = _verdict_cells()
    _set_verdict_cell(cells, "S3-R3-00", sample_and_e0=True, full=True)
    _set_verdict_cell(cells, "S3-R3-02", sample_and_e0=True, full=False)

    verdict = _family_verdict(family="S3", cells=cells)  # type: ignore[arg-type]

    assert verdict["research_decision"] == "CONTINUE"
    assert verdict["qualifying_adjacent_pairs"] == [["S3-R3-00", "S3-R3-02"]]


def test_pruned_external_neighbor_allows_narrow_but_included_neighbor_does_not() -> (
    None
):
    isolated = _verdict_cells()
    _set_verdict_cell(isolated, "S3-R3-00", sample_and_e0=True, full=True)
    assert (
        _family_verdict(  # type: ignore[arg-type]
            family="S3", cells=isolated
        )["research_decision"]
        == "NARROW"
    )

    included_neighbor = _verdict_cells()
    _set_verdict_cell(included_neighbor, "S3-R3-00", sample_and_e0=True, full=True)
    _set_verdict_cell(included_neighbor, "S3-R3-02", sample_and_e0=True, full=False)
    assert (
        _family_verdict(  # type: ignore[arg-type]
            family="S3", cells=included_neighbor
        )["research_decision"]
        == "CONTINUE"
    )


def test_internal_isolated_full_winner_terminates_instead_of_narrowing() -> None:
    cells = _verdict_cells()
    _set_verdict_cell(cells, "S4-R3-02", sample_and_e0=True, full=True)

    verdict = _family_verdict(family="S4", cells=cells)  # type: ignore[arg-type]

    assert verdict["research_decision"] == "TERMINATE"
    assert verdict["reason_codes"] == ["isolated_or_internal_full_gate_winner"]


def test_pruned_boundary_authority_preserves_all_six_typed_external_edges() -> None:
    scorecard = _complete_zero_scorecard()[-1]
    observed = [
        (
            row["config_id"],
            tuple(
                (parameter["name"], parameter["value"])
                for parameter in row["external_parameters"]
            ),
        )
        for row in scorecard["pruned_boundary_neighbors"]
    ]
    assert observed == [
        ("S3-R3-00", (("S_min", 0.10), ("M_min_bp", 0))),
        ("S3-R3-00", (("S_min", 0.05), ("M_min_bp", 25))),
        ("S3-R3-01", (("S_min", 0.05), ("M_min_bp", 25))),
        ("S4-R3-00", (("z_entry", 1.20), ("d_min_bp", 140))),
        ("S4-R3-01", (("z_entry", 1.00), ("d_min_bp", 180))),
        ("S4-R3-03", (("z_entry", 1.00), ("d_min_bp", 180))),
    ]


def test_raw_unsealed_accounting_and_relaxation_cannot_complete_research() -> None:
    context, ledger, accounting, gates, relaxation, _scorecard = (
        _complete_zero_scorecard()
    )
    raw_accounting = build_r3_scorecard(
        evidence_context=context,
        accounting=accounting.report,
        oos_ledger=ledger,
        gate_evidence=gates,
        relaxation_evidence=relaxation,
    )
    raw_relaxation = build_r3_scorecard(
        evidence_context=context,
        accounting=accounting,
        oos_ledger=ledger,
        gate_evidence=gates,
        relaxation_evidence=relaxation.analysis,
    )

    assert (
        "accounting:unsealed_attempt_evidence"
        in raw_accounting["operational"]["incomplete_reasons"]
    )
    assert (
        "section7:unsealed_raw_cohort_evidence"
        in raw_relaxation["operational"]["incomplete_reasons"]
    )
    assert raw_accounting["campaign_verdict"]["research_decision"] is None
    assert raw_relaxation["campaign_verdict"]["research_decision"] is None


def test_operational_incomplete_fail_closes_an_otherwise_full_cell_mutant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _production_context()
    ledger = issue_r3_all_cell_oos_ledger(
        evidence_context=context,
        cells=_empty_cells(),
    )

    def otherwise_full(*, config, **_kwargs):
        return {
            "config_id": config.config_id,
            "family": config.config_id[:2],
            "sample_qualified": True,
            "gross_e0_qualified": True,
            "sample_and_e0_qualified": True,
            "full_gate_passed": True,
        }

    monkeypatch.setattr(scorecard_module, "_build_cell", otherwise_full)
    scorecard = build_r3_scorecard(
        evidence_context=context,
        accounting=build_exact_12_accounting(
            campaign_run_id=context.campaign_run_id,
            ordered_mapping=context.ordered_mapping,
            registered_total=0,
            attempts=(),
        ),
        oos_ledger=ledger,
        gate_evidence=None,
        relaxation_evidence=None,
    )

    assert scorecard["operational"]["status"] == "INCOMPLETE"
    assert all(
        cell["operational_status"] == "INCOMPLETE" for cell in scorecard["cells"]
    )
    assert all(cell["research_eligible"] is False for cell in scorecard["cells"])
    assert all(cell["full_gate_passed"] is False for cell in scorecard["cells"])


def test_code_issued_receipts_recompute_and_reject_forged_derivatives() -> None:
    _context, _ledger, accounting, _gates, relaxation, _scorecard = (
        _complete_zero_scorecard()
    )
    forged_report = dataclasses.replace(accounting.report, registered_total=11)
    with pytest.raises(ValueError, match="differs from exact attempts"):
        dataclasses.replace(accounting, report=forged_report)

    forged_analysis = dataclasses.replace(
        relaxation.analysis,
        evidence_promoted=False,
    )
    with pytest.raises(ValueError, match="differs from raw exact-ray evidence"):
        dataclasses.replace(relaxation, analysis=forged_analysis)


def test_source_horizon_and_input_seal_are_reauthenticated() -> None:
    config = FROZEN_R3_ROSTER[0]
    assert type(config) is R3S3Config
    fold = _production_context().folds[0]
    result = S3EngineResult((), (), ())
    terminal = SealedS3Terminal(
        result=result,
        input_seal_sha256=seal_s3_engine_input(
            (),
            corpus_end_ts=fold.oos_end_ms,
            horizon_end_ts=fold.oos_end_ms,
        ),
        output_seal_sha256=seal_s3_engine_output(result),
    )

    with pytest.raises(ValueError, match="input hash does not match"):
        R3H2CellFoldInput(
            config=config,
            fold_id=fold.fold_id,
            h3_candidates=(),
            engine_intents=(),
            corpus_end_ts=fold.oos_end_ms,
            horizon_end_ts=fold.oos_end_ms - 1,
            terminal=terminal,
        )


def test_duplicate_funding_timestamp_is_rejected_before_attribution() -> None:
    config = FROZEN_R3_ROSTER[0]
    assert type(config) is R3S3Config
    fold = _production_context().folds[0]
    duplicate = FundingRow(fold.oos_start_ms, 8, 0.0001)
    sidecars = tuple(
        FundingSidecar(
            symbol=symbol,
            rows=(duplicate, duplicate) if index == 0 else (),
        )
        for index, symbol in enumerate(SYMBOLS)
    )

    with pytest.raises(ValueError, match="strictly chronological"):
        scorecard_module._validate_funding_sidecars(sidecars)


def test_fold_attribution_requires_market_input_authority() -> None:
    config = FROZEN_R3_ROSTER[-1]
    assert type(config) is R3S4Config
    fold = _production_context().folds[0]
    source, _snapshots = _s4_source_and_snapshots(config, fold, count=2)

    with pytest.raises(TypeError, match="exact R3MarketInputAuthority"):
        issue_r3_fold_scenario_attribution(
            path_scenario=PATH_SCENARIOS[1],
            source=source,
            market_input_authority=None,  # type: ignore[arg-type]
        )


def test_fold_scenarios_cannot_mix_source_membership_authorities() -> None:
    config = FROZEN_R3_ROSTER[-1]
    assert type(config) is R3S4Config
    fold_one, fold_two = _production_context().folds[:2]
    source_one = _empty_source(config, fold_one)
    source_two = _empty_source(config, fold_two)
    authority = _unit_market_authority()
    receipts = (
        issue_r3_fold_scenario_attribution(
            path_scenario=PATH_SCENARIOS[0],
            source=source_one,
            market_input_authority=authority,
        ),
        issue_r3_fold_scenario_attribution(
            path_scenario=PATH_SCENARIOS[1],
            source=source_two,
            market_input_authority=authority,
        ),
        issue_r3_fold_scenario_attribution(
            path_scenario=PATH_SCENARIOS[2],
            source=source_one,
            market_input_authority=authority,
        ),
    )

    with pytest.raises(ValueError, match="differ from frozen membership"):
        R3FoldOOSInput(scenario_attributions=receipts)


def test_relaxation_receipt_is_cross_sealed_to_issued_primary_oos_ledger() -> None:
    context, ledger, accounting, gates, _relaxation, _scorecard = (
        _complete_zero_scorecard()
    )
    normalized = normalize_r3_phase_ledgers(
        phase="OOS",
        sources=tuple(fold.source for cell in ledger.cells for fold in cell.folds),
    )
    reversed_ledgers = tuple(reversed(normalized.ledgers))
    with pytest.raises(ValueError, match="canonical 12x8 order"):
        PhaseLedgerEvidence("OOS", reversed_ledgers, ())

    assert (
        build_r3_scorecard(
            evidence_context=context,
            accounting=accounting,
            oos_ledger=ledger,
            gate_evidence=gates,
            relaxation_evidence=None,
        )["campaign_verdict"]["research_decision"]
        is None
    )


def test_accounting_issuer_refuses_caller_projected_attempt_rows() -> None:
    context = _production_context()
    projected = tuple(
        AttemptAccountingRow(
            row_id=row_id,
            experiment_id=experiment_id,
            retry_index=0,
            status="completed",
            reason_code=None,
            fold_evidence_hash="a" * 64,
            run_identity="b" * 64,
        )
        for row_id, experiment_id in context.ordered_mapping
    )

    with pytest.raises(TypeError, match="R3AttemptBatchItem"):
        issue_r3_scorecard_accounting(
            evidence_context=context,
            attempts=projected,
        )


def test_accounting_reason_is_derived_from_terminal_evidence_not_family() -> None:
    from app.services.rob974_r3_h6a_bridge import R3AttemptBatchItem
    from research_contracts.canonical_hash import canonical_sha256

    context = _production_context()
    source_attempts = list(_complete_accounting(context).source_attempts)
    original = source_attempts[0]
    payload = scorecard_module._plain(original.evidence_payload)
    for path in payload["phase_fold_paths"][0]["path_evidence"]:
        path["incompletes"] = 1
        path["terminal_incomplete_rows"] = [{"reason": "fold_horizon_rejected"}]
    fold_hash = canonical_sha256(payload)
    authoritative = R3AttemptBatchItem(
        row_id=original.row_id,
        experiment_id=original.experiment_id,
        retry_index=0,
        status="rejected",
        reason_code="rejected:fold_horizon_rejected",
        fold_evidence_hash=fold_hash,
        run_identity=canonical_sha256(
            {
                "full_campaign_hash": context.campaign_identity_sha256,
                "campaign_run_id": context.campaign_run_id,
                "row_id": original.row_id,
                "experiment_id": original.experiment_id,
                "fold_evidence_hash": fold_hash,
            }
        ),
        evidence_payload=payload,
    )
    source_attempts[0] = authoritative

    issued = issue_r3_scorecard_accounting(
        evidence_context=context,
        attempts=tuple(source_attempts),
    )
    assert issued.attempts[0].reason_code == "rejected:fold_horizon_rejected"

    source_attempts[0] = dataclasses.replace(
        authoritative,
        reason_code="rejected:data_gap_in_position",
    )
    with pytest.raises(ValueError, match="differs from terminal evidence"):
        issue_r3_scorecard_accounting(
            evidence_context=context,
            attempts=tuple(source_attempts),
        )


def test_fold_scenario_issuer_has_no_raw_market_value_injection_surface() -> None:
    assert tuple(inspect.signature(issue_r3_fold_scenario_attribution).parameters) == (
        "path_scenario",
        "source",
        "market_input_authority",
    )


def test_canonical_json_rejects_reversed_nested_relaxation_rays() -> None:
    scorecard = json.loads(canonical_r3_json_bytes(_complete_zero_scorecard()[-1]))
    scorecard["section7_relaxation"]["oos"]["rays"].reverse()

    with pytest.raises(ValueError, match="nested.*ray"):
        canonical_r3_json_bytes(scorecard)


@pytest.mark.parametrize(
    ("field_name", "expected_message"),
    (
        ("fold_evidence_hash", "fold_evidence_hash"),
        ("run_identity", "run_identity"),
    ),
)
def test_accounting_recomputes_original_attempt_hashes(
    field_name: str,
    expected_message: str,
) -> None:
    context = _production_context()
    source_attempts = list(_complete_accounting(context).source_attempts)
    source_attempts[0] = dataclasses.replace(
        source_attempts[0],
        **{field_name: "0" * 64},
    )

    with pytest.raises(ValueError, match=expected_message):
        issue_r3_scorecard_accounting(
            evidence_context=context,
            attempts=tuple(source_attempts),
        )


def test_accounting_rejects_rehashed_but_reordered_payload() -> None:
    from research_contracts.canonical_hash import canonical_sha256

    context = _production_context()
    source_attempts = list(_complete_accounting(context).source_attempts)
    original = source_attempts[0]
    payload = scorecard_module._plain(original.evidence_payload)
    payload["phase_fold_paths"].reverse()
    fold_hash = canonical_sha256(payload)
    source_attempts[0] = dataclasses.replace(
        original,
        evidence_payload=payload,
        fold_evidence_hash=fold_hash,
        run_identity=canonical_sha256(
            {
                "full_campaign_hash": context.campaign_identity_sha256,
                "campaign_run_id": context.campaign_run_id,
                "row_id": original.row_id,
                "experiment_id": original.experiment_id,
                "fold_evidence_hash": fold_hash,
            }
        ),
    )

    with pytest.raises(ValueError, match="headers are unordered"):
        issue_r3_scorecard_accounting(
            evidence_context=context,
            attempts=tuple(source_attempts),
        )


def test_market_authority_rejects_stored_snapshot_and_funding_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rob974_features import MinuteBar

    from app.services.rob974_h6b_materializer import ActualH4InputData
    from research_contracts.canonical_hash import canonical_sha256

    context = _production_context()
    config = FROZEN_R3_ROSTER[-1]
    assert type(config) is R3S4Config
    fold = context.folds[0]
    source, snapshots = _s4_source_and_snapshots(config, fold, count=2)
    sidecars = tuple(FundingSidecar.from_rows(symbol, ()) for symbol in SYMBOLS)
    funding_hashes = tuple((symbol, canonical_sha256([])) for symbol in SYMBOLS)

    def unit_market_derivation(**_kwargs):
        return (
            "a" * 64,
            scorecard_module._feature_snapshot_hash(snapshots),
            snapshots,
            sidecars,
            funding_hashes,
        )

    monkeypatch.setattr(
        scorecard_module,
        "_validate_market_input_and_derive",
        unit_market_derivation,
    )
    minute_ts = fold.oos_start_ms
    minute = MinuteBar(minute_ts, 1.0, 1.0, 1.0, 1.0, 1.0)
    actual_data = ActualH4InputData.from_mapping(
        dict.fromkeys(SYMBOLS, (minute,)),
        corpus_end_ts=minute_ts + 60_000,
        persisted_corpus_hash="a" * 64,
        persisted_feature_hash="b" * 64,
    )

    snapshot_authority = issue_r3_market_input_authority(
        evidence_context=context,
        actual_h4_input_data=actual_data,
    )
    altered = dataclasses.replace(
        snapshot_authority.snapshots[0],
        M=snapshot_authority.snapshots[0].M + 0.01,
    )
    object.__setattr__(
        snapshot_authority,
        "snapshots",
        (altered, *snapshot_authority.snapshots[1:]),
    )
    with pytest.raises(ValueError, match="payload digest drifted"):
        issue_r3_fold_scenario_attribution(
            path_scenario=PATH_SCENARIOS[1],
            source=source,
            market_input_authority=snapshot_authority,
        )

    funding_authority = issue_r3_market_input_authority(
        evidence_context=context,
        actual_h4_input_data=actual_data,
    )
    mutated_sidecars = (
        FundingSidecar.from_rows(
            SYMBOLS[0],
            (FundingRow(fold.oos_start_ms, 8, 0.0001),),
        ),
        *funding_authority.funding_sidecars[1:],
    )
    object.__setattr__(funding_authority, "funding_sidecars", mutated_sidecars)
    with pytest.raises(ValueError, match="payload digest drifted"):
        issue_r3_fold_scenario_attribution(
            path_scenario=PATH_SCENARIOS[1],
            source=source,
            market_input_authority=funding_authority,
        )


def test_market_authority_does_not_trust_actual_input_string_pins_alone() -> None:
    from rob974_features import MinuteBar

    from app.services.rob974_h6b_materializer import ActualH4InputData

    context = _production_context()
    component = scorecard_module._production_dataset_component(context)
    minute_ts = component["window_start_ms"]
    minute = MinuteBar(minute_ts, 1.0, 1.0, 1.0, 1.0, 1.0)
    forged = ActualH4InputData.from_mapping(
        dict.fromkeys(SYMBOLS, (minute,)),
        corpus_end_ts=component["window_end_ms"],
        persisted_corpus_hash=component["content_sha256"],
        persisted_feature_hash="b" * 64,
    )

    with pytest.raises(ValueError, match="canonical manifest coverage"):
        issue_r3_market_input_authority(
            evidence_context=context,
            actual_h4_input_data=forged,
        )


def test_feature_snapshot_hash_is_launcher_payload_parity() -> None:
    from research_contracts.canonical_hash import canonical_sha256

    snapshots = (_snapshot(_production_context().folds[0].oos_start_ms),)
    expected = canonical_sha256(
        [
            {
                **snapshot.__dict__,
                "features": [feature.__dict__ for feature in snapshot.features],
            }
            for snapshot in snapshots
        ]
    )
    assert scorecard_module._feature_snapshot_hash(snapshots) == expected


def test_canonical_json_rejects_nested_step_fold_and_gate_report_reordering() -> None:
    canonical = canonical_r3_json_bytes(_complete_zero_scorecard()[-1])

    reversed_steps = json.loads(canonical)
    reversed_steps["section7_relaxation"]["oos"]["rays"][2]["steps"].reverse()
    with pytest.raises(ValueError, match="step/fold order"):
        canonical_r3_json_bytes(reversed_steps)

    reversed_folds = json.loads(canonical)
    reversed_folds["section7_relaxation"]["oos"]["rays"][0]["steps"][0][
        "folds"
    ].reverse()
    with pytest.raises(ValueError, match="step/fold order"):
        canonical_r3_json_bytes(reversed_folds)

    reversed_reports = json.loads(canonical)
    reversed_reports["section5_gate_audit"]["reports"].reverse()
    with pytest.raises(ValueError, match="nested report/fold order"):
        canonical_r3_json_bytes(reversed_reports)


def test_default_scorecard_import_keeps_app_service_graph_lazy() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import rob974_r3_scorecard; "
                "assert 'app.services.rob974_h6b_materializer' not in sys.modules; "
                "assert 'app.services.rob974_r3_h6a_bridge' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
