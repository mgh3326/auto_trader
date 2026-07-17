"""ROB-945 (H5) -- scorecard JSON/Markdown assembly RED tests.

JSON is the sole source of truth; Markdown is a pure deterministic render
of the already-built JSON object. Verdict labels are exactly
``historical_pass|historical_fail|incomplete``; readiness is always
``historical_screen_only`` and this module creates no ROB-905 validated
gate artifact/path.
"""

from __future__ import annotations

import hashlib
import json
import math

import pytest
import rob941_frozen_scope as frozen_scope
import rob944_folds as foldmod
from rob944_frozen_campaign import (
    CANONICAL_ROW_ORDER,
    PRODUCTION_S1_STRATEGY_KEY,
    PRODUCTION_S2_STRATEGY_KEY,
    build_production_frozen_campaign_envelope,
)
from rob944_selection import (
    INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
    INSUFFICIENT_SYMBOL_EVIDENCE_REASON,
    ConfigSelectionOutcome,
    FoldSelectionTrace,
)
from rob944_walkforward import (
    ConfigAttemptResult,
    FoldWalkForwardResult,
    WalkForwardResult,
    summarize_config_attempts_for_h6,
)
from rob945_scenario_metrics import FoldStabilityRow, StrategyScenarioAggregate
from rob945_scorecard import (
    HASH_DRIFT_REASON,
    ScorecardInputError,
    build_scorecard,
    render_markdown,
)
from rob945_signal_concurrency import StrategyConcurrencyEvidence
from run_rob944_campaign import _summary_to_attempt_evidence

from research_contracts.canonical_hash import canonical_sha256

_SYMBOLS = ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")

# ROB-945 Task 1: ``build_scorecard`` now seals accounting/attempt-evidence
# against the REAL production frozen campaign (never a caller-self-consistent
# fake) -- fixtures below use the actual envelope/hash/run-id/experiment-ids
# rather than a fabricated ``exp-00..23`` roster, and derive REAL
# cross-bindable ``AttemptEvidence`` from a real (0-scenario-winner, no
# corpus) ``WalkForwardResult`` per strategy via the actual H6-build
# boundary -- mirrors ``test_rob945_accounting_seal.py``'s fixtures.
_ENVELOPE = build_production_frozen_campaign_envelope()
_REAL_FULL_CAMPAIGN_HASH = _ENVELOPE.full_campaign_hash()
_REAL_FULL_CAMPAIGN_PAYLOAD = _ENVELOPE.to_dict()
_REAL_FROZEN_EXPERIMENT_IDS = tuple(_REAL_FULL_CAMPAIGN_PAYLOAD["experiment_ids"])
_REAL_DATASET_MANIFEST_HASH = _ENVELOPE.dataset_manifest_hash
_REAL_SIGNAL_MANIFEST_HASH = _ENVELOPE.signal_manifest_hash

_EXPERIMENT_ID_TO_CONFIG_ID = dict(
    zip(_REAL_FROZEN_EXPERIMENT_IDS, CANONICAL_ROW_ORDER, strict=True)
)
_STRATEGY_KEY = {"S1": PRODUCTION_S1_STRATEGY_KEY, "S2": PRODUCTION_S2_STRATEGY_KEY}
_REAL_FOLDS = foldmod.generate_frozen_fold_schedule(
    frozen_scope.WINDOW_START_MS, frozen_scope.WINDOW_END_MS
)


def _symbol_metrics_all_present():
    from rob945_scenario_metrics import SymbolScenarioMetrics

    return tuple(
        SymbolScenarioMetrics(
            symbol=s,
            trade_count=5,
            signal_count=5,
            net_expectancy_bps=10.0,
            net_pnl_bps=50.0,
        )
        for s in _SYMBOLS
    )


def _scenario(
    scenario_name, net_expectancy_bps=10.0, profit_factor=2.0, trade_count=20
):
    return StrategyScenarioAggregate(
        strategy="S1",
        scenario_name=scenario_name,
        trade_count=trade_count,
        net_expectancy_bps=net_expectancy_bps,
        pooled_expectancy_bps=net_expectancy_bps,
        profit_factor=profit_factor,
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
    )


def _fold_rows(count=8, positive=8):
    rows = []
    for i in range(count):
        net = 10.0 if i < positive else -10.0
        rows.append(
            FoldStabilityRow(
                fold_id=f"fold-{i:02d}",
                selected_config_id="S1-03",
                trade_count=5,
                net_expectancy_bps=net / 5,
                net_pnl_bps=net,
                profit_factor=float("inf") if net > 0 else 0.0,
                positive=net > 0,
                net_pnl_class="positive" if net > 0 else "negative",
            )
        )
    return tuple(rows)


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


def _full_campaign_payload():
    return _REAL_FULL_CAMPAIGN_PAYLOAD


def _derive_campaign_run_id(full_campaign_hash):
    import base64

    digest_hex = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "primary_run"}
    )
    suffix = (
        base64.urlsafe_b64encode(bytes.fromhex(digest_hex)).decode("ascii").rstrip("=")
    )
    return f"rob944-primary-{suffix}"


def _hex64(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _config_ids_for(strategy):
    return tuple(f"{strategy}-{i:02d}" for i in range(12))


def _rejected_candidate(config_id, seed):
    return ConfigSelectionOutcome(
        config_id=config_id,
        eligible_symbols=(),
        excluded_symbols=tuple(
            (symbol, INSUFFICIENT_SYMBOL_EVIDENCE_REASON)
            for symbol in frozen_scope.UNIVERSE
        ),
        equal_weight_expectancy_bps=None,
        pooled_expectancy_bps=None,
        profit_factor=0.0,
        rejected=True,
        rejection_reason=INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
        train_input_hash=_hex64(f"train:{seed}"),
        no_trade_reason_counts={},
    )


def _build_walkforward_result(strategy, *, status_overrides=None):
    """A real, hand-assembled ``WalkForwardResult`` over the REAL 8-fold
    schedule (mirrors ``test_rob945_accounting_seal.py``) -- no corpus, no
    network. Every config defaults to attempt-level ``status="completed"``."""
    status_overrides = status_overrides or {}
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

    attempts = []
    for config_id in config_ids:
        status, reason_code = status_overrides.get(config_id, ("completed", None))
        attempts.append(
            ConfigAttemptResult(
                strategy=strategy,
                config_id=config_id,
                status=status,
                reason_code=reason_code,
                selected_in_folds=(),
                crash_log=(),
                gap_rejection_log=(),
            )
        )
    return WalkForwardResult(
        strategy=strategy,
        folds=tuple(fold_results),
        config_attempts=tuple(attempts),
        concatenated_oos_ledgers={},
    )


DEFAULT_WALKFORWARD_RESULTS = {
    "S1": _build_walkforward_result("S1"),
    "S2": _build_walkforward_result("S2"),
}


def _real_attempts_for(campaign_run_id, walkforward_results):
    """Derive the REAL 24 ``AttemptEvidence`` dicts (via the actual H6-build
    boundary) from a ``{"S1": WalkForwardResult, "S2": WalkForwardResult}``
    mapping."""
    attempts = []
    for strategy in ("S1", "S2"):
        wf_result = walkforward_results[strategy]
        for summary in summarize_config_attempts_for_h6(wf_result):
            experiment_id = next(
                eid
                for eid, cid in _EXPERIMENT_ID_TO_CONFIG_ID.items()
                if cid == summary.config_id
            )
            evidence = _summary_to_attempt_evidence(
                summary,
                strategy_key=_STRATEGY_KEY[strategy],
                experiment_id=experiment_id,
                full_campaign_hash=_REAL_FULL_CAMPAIGN_HASH,
                campaign_run_id=campaign_run_id,
            )
            attempts.append(evidence.model_dump())
    return attempts


def _sealed_24_attempts(campaign_run_id):
    return _real_attempts_for(campaign_run_id, DEFAULT_WALKFORWARD_RESULTS)


def _clean_accounting_report(campaign_run_id, **overrides):
    report = {
        "campaign_run_id": campaign_run_id,
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
    report.update(overrides)
    return report


def _base_kwargs():
    payload = _full_campaign_payload()
    full_campaign_hash = _REAL_FULL_CAMPAIGN_HASH
    campaign_run_id = _derive_campaign_run_id(full_campaign_hash)
    return {
        "full_campaign_hash": full_campaign_hash,
        "full_campaign_payload": payload,
        "campaign_run_id": campaign_run_id,
        "dataset_manifest_hash": _REAL_DATASET_MANIFEST_HASH,
        "signal_manifest_hash": _REAL_SIGNAL_MANIFEST_HASH,
        "accounting_report": _clean_accounting_report(campaign_run_id),
        "attempt_evidence": _sealed_24_attempts(campaign_run_id),
        "walkforward_results": DEFAULT_WALKFORWARD_RESULTS,
        "strategies": {"S1": _strategy_evidence(), "S2": _strategy_evidence()},
    }


def test_build_scorecard_returns_a_json_serializable_envelope_with_no_nan_or_inf():
    envelope = build_scorecard(**_base_kwargs())
    text = json.dumps(envelope, allow_nan=False)
    assert "NaN" not in text and "Infinity" not in text


def test_hash_drift_between_provided_and_recomputed_full_campaign_hash_fails_closed():
    kwargs = _base_kwargs()
    kwargs["full_campaign_hash"] = "0" * 64  # deliberately wrong
    with pytest.raises(ScorecardInputError) as exc_info:
        build_scorecard(**kwargs)
    assert HASH_DRIFT_REASON in str(exc_info.value)


def test_accounting_incomplete_verdict_propagates_to_incomplete_reason():
    """A genuinely well-formed but incomplete H6 accounting report (one
    frozen experiment ID never registered) is NOT a raise -- it seals as
    ``campaign_verdict == "incomplete"``, never ``historical_pass``/``fail``,
    per the malformed-vs-well-formed-incomplete distinction (ROB-945 Task 1,
    RED case 10)."""
    kwargs = _base_kwargs()
    campaign_run_id = kwargs["campaign_run_id"]
    missing_id = _REAL_FROZEN_EXPERIMENT_IDS[-1]
    kwargs["attempt_evidence"] = [
        a
        for a in _sealed_24_attempts(campaign_run_id)
        if a["attempt_key"]["experiment_id"] != missing_id
    ]
    kwargs["accounting_report"] = _clean_accounting_report(
        campaign_run_id,
        actual_registrations=23,
        primary_attempts=23,
        total_attempts=23,
        status_counts={"completed": 23, "rejected": 0, "crashed": 0, "timeout": 0},
        missing_experiment_ids=[missing_id],
        verdict="incomplete",
    )
    envelope = build_scorecard(**kwargs)
    body = envelope["scorecard_payload"]
    assert body["campaign_verdict"] == "incomplete"
    assert body["lineage"]["accounting_complete"] is False
    assert body["lineage"]["accounting_performance_usable"] is False


def test_missing_strategy_fails_closed():
    kwargs = _base_kwargs()
    del kwargs["strategies"]["S2"]
    with pytest.raises(ScorecardInputError):
        build_scorecard(**kwargs)


def test_wrong_number_of_folds_fails_closed():
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"]["fold_stability"] = _fold_rows(count=3, positive=3)
    with pytest.raises(ScorecardInputError):
        build_scorecard(**kwargs)


def test_missing_symbol_in_scenario_metrics_fails_closed():
    kwargs = _base_kwargs()
    from dataclasses import replace

    bad_scenario = replace(
        kwargs["strategies"]["S1"]["scenarios"]["base"],
        symbol_metrics=_symbol_metrics_all_present()[:3],
    )
    kwargs["strategies"]["S1"]["scenarios"]["base"] = bad_scenario
    with pytest.raises(ScorecardInputError):
        build_scorecard(**kwargs)


def test_verdict_is_wired_through_from_the_verdict_authority():
    envelope = build_scorecard(**_base_kwargs())
    body = envelope["scorecard_payload"]
    s1_verdict = body["strategies"]["S1"]["verdict"]
    assert s1_verdict["verdict"] in ("historical_pass", "historical_fail", "incomplete")
    assert s1_verdict["readiness"] == "historical_screen_only"


def test_btc_row_discloses_historical_only_and_demo_ineligible_reason():
    envelope = build_scorecard(**_base_kwargs())
    body = envelope["scorecard_payload"]
    btc_row = next(r for r in body["symbol_universe"] if r["symbol"] == "BTCUSDT")
    assert btc_row["historical_only"] is True
    assert btc_row["demo_execution_eligible"] is False
    assert btc_row["reason"] == "min_notional_50_exceeds_demo_cap_10"


def test_no_validated_gate_artifact_fields_anywhere_in_output():
    envelope = build_scorecard(**_base_kwargs())
    text = json.dumps(envelope)
    assert "validated_signal_gate.v1" not in text
    assert "BINANCE_DEMO_SCALPING_VALIDATED_GATE_PATH" not in text


def test_repeat_build_is_byte_identical():
    kwargs = _base_kwargs()
    e1 = build_scorecard(**kwargs)
    e2 = build_scorecard(**kwargs)
    assert json.dumps(e1, sort_keys=True) == json.dumps(e2, sort_keys=True)
    assert e1["scorecard_artifact_hash"] == e2["scorecard_artifact_hash"]


def test_mutation_of_a_scenario_metric_changes_the_artifact_hash():
    kwargs = _base_kwargs()
    e1 = build_scorecard(**kwargs)
    kwargs["strategies"]["S1"]["scenarios"]["base"] = _scenario(
        "base", net_expectancy_bps=99.0
    )
    e2 = build_scorecard(**kwargs)
    assert e1["scorecard_artifact_hash"] != e2["scorecard_artifact_hash"]


def test_markdown_is_a_pure_render_of_the_json_and_traces_every_verdict():
    envelope = build_scorecard(**_base_kwargs())
    markdown = render_markdown(envelope)
    body = envelope["scorecard_payload"]
    for _strategy_key, evidence in body["strategies"].items():
        assert evidence["verdict"]["verdict"] in markdown
    assert envelope["scorecard_artifact_hash"] in markdown
    assert "BTCUSDT" in markdown and "min_notional_50_exceeds_demo_cap_10" in markdown


def test_markdown_never_silently_omits_scenario_trade_count_divergence():
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"]["scenarios"]["upward_stress"] = _scenario(
        "upward_stress", net_expectancy_bps=1.0, trade_count=3
    )
    envelope = build_scorecard(**kwargs)
    markdown = render_markdown(envelope)
    assert "3" in markdown  # upward_stress trade_count must be visible


def test_required_behavioral_disclosures_are_present():
    envelope = build_scorecard(**_base_kwargs())
    body = envelope["scorecard_payload"]
    disclosures = body["disclosures"]
    assert disclosures["account_global_collision_unmodeled"]
    assert disclosures["demo_arbitration_required_no_rule_invented"]
    assert disclosures["no_spread_age_lot_gates_in_historical"]
    assert disclosures["s1_07_footnote"]
    assert disclosures["not_validated_signal_gate"] is True


def _fold_row(fold_id, net_pnl_bps, trade_count):
    if trade_count == 0:
        return FoldStabilityRow(
            fold_id=fold_id,
            selected_config_id=None,
            trade_count=0,
            net_expectancy_bps=None,
            net_pnl_bps=0.0,
            profit_factor=None,
            positive=None,
            net_pnl_class=None,
        )
    net_pnl_class = (
        "positive" if net_pnl_bps > 0 else "negative" if net_pnl_bps < 0 else "zero"
    )
    return FoldStabilityRow(
        fold_id=fold_id,
        selected_config_id="S1-03",
        trade_count=trade_count,
        net_expectancy_bps=net_pnl_bps / trade_count,
        net_pnl_bps=net_pnl_bps,
        profit_factor=math.inf if net_pnl_bps > 0 else 0.0,
        positive=net_pnl_bps > 0,
        net_pnl_class=net_pnl_class,
    )


def test_fold_counts_are_derived_from_net_pnl_class_not_the_coarser_positive_bool():
    """+ / 0 / - / no-trade must each be counted into their OWN bucket --
    ``row.positive`` alone conflates exactly-zero with negative, and
    conflates no-trade with nothing at all."""
    kwargs = _base_kwargs()
    mixed_rows = (
        _fold_row("fold-00", 10.0, 2),  # positive
        _fold_row("fold-01", 20.0, 2),  # positive
        _fold_row("fold-02", 30.0, 2),  # positive
        _fold_row("fold-03", 40.0, 2),  # positive
        _fold_row("fold-04", 50.0, 2),  # positive
        _fold_row("fold-05", 0.0, 2),  # zero (WITH trades)
        _fold_row("fold-06", -10.0, 2),  # negative
        _fold_row("fold-07", 0.0, 0),  # undefined (no trades at all)
    )
    kwargs["strategies"]["S1"]["fold_stability"] = mixed_rows
    envelope = build_scorecard(**kwargs)
    s1 = envelope["scorecard_payload"]["strategies"]["S1"]
    assert s1["positive_oos_fold_count"] == 5
    assert s1["zero_oos_fold_count"] == 1
    assert s1["negative_oos_fold_count"] == 1
    assert s1["undefined_oos_fold_count"] == 1


def test_reordered_fold_input_normalizes_to_the_same_canonical_output():
    kwargs = _base_kwargs()
    e1 = build_scorecard(**kwargs)
    kwargs["strategies"]["S1"]["fold_stability"] = tuple(
        reversed(kwargs["strategies"]["S1"]["fold_stability"])
    )
    e2 = build_scorecard(**kwargs)
    assert e1["scorecard_artifact_hash"] == e2["scorecard_artifact_hash"]


def test_capture_invalid_flag_makes_strategy_and_campaign_verdict_incomplete():
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"]["capture_valid"] = False
    envelope = build_scorecard(**kwargs)
    body = envelope["scorecard_payload"]
    assert body["strategies"]["S1"]["verdict"]["verdict"] == "incomplete"
    assert "capture_invalid" in body["strategies"]["S1"]["verdict"]["reason_codes"]
    assert body["campaign_verdict"] == "incomplete"


def test_pbo_grid_invalid_flag_makes_strategy_incomplete_but_pbo_result_alone_does_not():
    """Structural PBO grid invalidity (pbo_valid=False) is an evidence gap
    -> incomplete. A merely ambiguous/insufficient PBO *value* (pbo_valid
    still True) must NOT affect the verdict -- PBO stays auxiliary."""
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"]["pbo_valid"] = False
    envelope = build_scorecard(**kwargs)
    assert (
        envelope["scorecard_payload"]["strategies"]["S1"]["verdict"]["verdict"]
        == "incomplete"
    )

    kwargs2 = _base_kwargs()  # pbo_valid defaults True; pbo.value is a plain float here
    envelope2 = build_scorecard(**kwargs2)
    verdict2 = envelope2["scorecard_payload"]["strategies"]["S1"]["verdict"]["verdict"]
    assert verdict2 != "incomplete"


def test_campaign_verdict_aggregates_across_both_strategies():
    kwargs = _base_kwargs()
    envelope = build_scorecard(**kwargs)
    body = envelope["scorecard_payload"]
    s1v = body["strategies"]["S1"]["verdict"]["verdict"]
    s2v = body["strategies"]["S2"]["verdict"]["verdict"]
    if s1v == "incomplete" or s2v == "incomplete":
        assert body["campaign_verdict"] == "incomplete"
    elif s1v == "historical_fail" or s2v == "historical_fail":
        assert body["campaign_verdict"] == "historical_fail"
    else:
        assert body["campaign_verdict"] == "historical_pass"


def test_attempt_evidence_wrong_count_fails_closed():
    kwargs = _base_kwargs()
    kwargs["attempt_evidence"] = _sealed_24_attempts(kwargs["campaign_run_id"])[:23]
    with pytest.raises(ScorecardInputError):
        build_scorecard(**kwargs)


def test_attempt_evidence_duplicate_experiment_id_fails_closed():
    kwargs = _base_kwargs()
    attempts = _sealed_24_attempts(kwargs["campaign_run_id"])
    attempts[1] = dict(attempts[0])
    kwargs["attempt_evidence"] = attempts
    with pytest.raises(ScorecardInputError):
        build_scorecard(**kwargs)


def test_attempt_evidence_nonzero_retry_index_fails_closed():
    kwargs = _base_kwargs()
    attempts = _sealed_24_attempts(kwargs["campaign_run_id"])
    attempts[0]["attempt_key"]["retry_index"] = 1
    kwargs["attempt_evidence"] = attempts
    with pytest.raises(ScorecardInputError):
        build_scorecard(**kwargs)


def test_hash_format_rejects_non_hex_or_wrong_length():
    kwargs = _base_kwargs()
    kwargs["dataset_manifest_hash"] = "Z" * 64
    with pytest.raises(ScorecardInputError):
        build_scorecard(**kwargs)
    kwargs2 = _base_kwargs()
    kwargs2["dataset_manifest_hash"] = "b" * 63
    with pytest.raises(ScorecardInputError):
        build_scorecard(**kwargs2)


def test_dataset_manifest_hash_content_drift_from_the_real_frozen_value_fails_closed():
    """A well-FORMATTED (64 lowercase hex) but WRONG dataset_manifest_hash
    must fail closed -- previously only format was checked, never content
    against the real frozen H1 manifest hash embedded in the production
    envelope (the caller could pass ANY well-formed hex64 value)."""
    kwargs = _base_kwargs()
    kwargs["dataset_manifest_hash"] = "b" * 64  # well-formed, but not real
    with pytest.raises(ScorecardInputError) as exc_info:
        build_scorecard(**kwargs)
    assert "dataset_manifest_hash" in str(exc_info.value)


def test_signal_manifest_hash_content_drift_from_the_real_frozen_value_fails_closed():
    kwargs = _base_kwargs()
    kwargs["signal_manifest_hash"] = "c" * 64  # well-formed, but not real
    with pytest.raises(ScorecardInputError) as exc_info:
        build_scorecard(**kwargs)
    assert "signal_manifest_hash" in str(exc_info.value)


def test_campaign_run_id_drift_fails_closed():
    kwargs = _base_kwargs()
    kwargs["campaign_run_id"] = "rob944-primary-" + "z" * 43
    with pytest.raises(ScorecardInputError):
        build_scorecard(**kwargs)


def test_mapping_key_order_does_not_affect_the_hash():
    kwargs = _base_kwargs()
    reordered = dict(reversed(list(kwargs["strategies"].items())))
    kwargs_reordered = {**kwargs, "strategies": reordered}
    e1 = build_scorecard(**kwargs)
    e2 = build_scorecard(**kwargs_reordered)
    assert e1["scorecard_artifact_hash"] == e2["scorecard_artifact_hash"]
