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
    scenario_name,
    net_expectancy_bps=10.0,
    profit_factor=2.0,
    trade_count=20,
    strategy="S1",
    no_trade_reason_counts=None,
):
    return StrategyScenarioAggregate(
        strategy=strategy,
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
        no_trade_reason_counts=no_trade_reason_counts or {},
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


def test_zero_denominator_concurrency_renders_as_json_zero_never_null():
    """I-1 final ruling regression (orch-fable-answer-rob945c-20260718.md,
    Q1=A FINAL): a zero-signal-minute strategy must render
    ``denominator: 0`` (not JSON ``null``) with ``rate: null`` in both the
    JSON payload and the Markdown render."""
    kwargs = _base_kwargs()
    zero_concurrency = StrategyConcurrencyEvidence(
        strategy="S1",
        numerator=0,
        denominator=0,
        rate=None,
        reason="no_entry_signal_minutes",
        distinct_symbol_count_histogram={1: 0, 2: 0, 3: 0, 4: 0},
    )
    kwargs["strategies"]["S1"]["signal_concurrency"] = zero_concurrency
    envelope = build_scorecard(**kwargs)
    concurrency_json = envelope["scorecard_payload"]["strategies"]["S1"][
        "signal_concurrency"
    ]
    assert concurrency_json["denominator"] == 0
    assert concurrency_json["rate"] is None
    assert concurrency_json["reason"] == "no_entry_signal_minutes"
    overall = envelope["scorecard_payload"]["signal_concurrency_overall"]
    assert overall["denominator"] == 2  # S1:0 + S2:2 (S2 keeps the default fixture)
    markdown = render_markdown(envelope)
    assert "denominator=0" in markdown


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


def test_diagnostic_evidence_and_overflow_never_alter_campaign_verdict_or_scorecard_hash():
    """ROB-970 R1 Important-5: a DIRECT assertion (not merely an inferred
    code-path separation) that diagnostic content -- present, absent, or
    differently-worded -- never changes campaign_verdict, the scorecard's
    own semantic artifact hash, the H5 six-key accounting seal/trial
    accounting hash, or the full campaign identity/run ID. Also exercises
    the ROB-970 R1 divergence-observer seam: emitting a diagnostic-replay-
    divergence observation has zero effect on any of these values either.
    """
    baseline_kwargs = _base_kwargs()
    baseline = build_scorecard(**baseline_kwargs)

    with_diagnostics_kwargs = _base_kwargs()
    attempts = with_diagnostics_kwargs["attempt_evidence"]
    attempts[0] = dict(
        attempts[0],
        diagnostic_evidence=[dict(_DIAGNOSTIC_ROW)],
        diagnostic_overflow={
            "truncated": True,
            "omitted_distinct_signatures": 2,
            "omitted_occurrences": 5,
        },
    )
    attempts[1] = dict(
        attempts[1],
        diagnostic_evidence=[
            dict(_DIAGNOSTIC_ROW, message="a totally different secret-bearing message")
        ],
    )
    with_diagnostics = build_scorecard(**with_diagnostics_kwargs)

    assert (
        baseline["scorecard_payload"]["campaign_verdict"]
        == with_diagnostics["scorecard_payload"]["campaign_verdict"]
    )
    assert (
        baseline["scorecard_artifact_hash"]
        == with_diagnostics["scorecard_artifact_hash"]
    )
    assert (
        baseline["scorecard_payload"]["lineage"]["trial_accounting_hash"]
        == with_diagnostics["scorecard_payload"]["lineage"]["trial_accounting_hash"]
    )
    assert (
        baseline["scorecard_payload"]["lineage"]["full_campaign_hash"]
        == with_diagnostics["scorecard_payload"]["lineage"]["full_campaign_hash"]
    )
    assert (
        baseline["scorecard_payload"]["lineage"]["campaign_run_id"]
        == with_diagnostics["scorecard_payload"]["lineage"]["campaign_run_id"]
    )

    # The divergence-observer seam itself (app.services.research_campaign_
    # bridge._emit_diagnostic_replay_divergence) is a side-effecting stderr
    # write with no return value and no mutation of any evidence object --
    # simulated here (without an app.* import, staying within this pure H5
    # module's own boundary) to prove the SHAPE of that guarantee:
    # rebuilding the scorecard from the SAME kwargs after an arbitrary
    # stderr write must be byte-identical to the pre-observation build.
    import sys as _sys

    _sys.stderr.write(
        '{"event": "diagnostic_replay_divergence", "idempotency_key": '
        '"camp1:exp-observer-effect-0:0"}\n'
    )
    after_observation = build_scorecard(**with_diagnostics_kwargs)
    assert (
        after_observation["scorecard_artifact_hash"]
        == with_diagnostics["scorecard_artifact_hash"]
    )


_DIAGNOSTIC_ROW = {
    "transport": "in_process",
    "stage": "generator",
    "exception_type": "RuntimeError",
    "message": "boom",
    "traceback_text": "Traceback...\nRuntimeError: boom\n",
    "stderr": None,
    "strategy": "S1",
    "config_id": "S1-00",
    "symbol": "BTCUSDT",
    "fold_id": "fold-00",
    "scenario_name": None,
    "signature": "a" * 64,
    "occurrence_count": 1,
    "truncated": False,
}


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


# ===========================================================================
# Final-fix -- I-4: daily-stop counts, 3/3/2 deltas, ex-BTC subtotal, S2
# spec-deviation register (mechanically derived, machine-visible, rendered
# in both JSON and Markdown; never a new pass threshold).
# ===========================================================================


def _strategy_evidence_with(
    strategy="S1",
    *,
    trade_counts=None,
    no_trade_reason_counts_by_scenario=None,
):
    trade_counts = trade_counts or {}
    no_trade_reason_counts_by_scenario = no_trade_reason_counts_by_scenario or {}
    return {
        "scenarios": {
            name: _scenario(
                name,
                strategy=strategy,
                trade_count=trade_counts.get(name, 20),
                no_trade_reason_counts=no_trade_reason_counts_by_scenario.get(name, {}),
            )
            for name in ("base", "primary_stress", "upward_stress")
        },
        "fold_stability": _fold_rows(),
        "signal_concurrency": _concurrency(),
        "pbo": _pbo(),
    }


def test_daily_stop_active_count_is_derived_from_no_trade_reason_counts():
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"] = _strategy_evidence_with(
        "S1",
        no_trade_reason_counts_by_scenario={
            "primary_stress": {"daily_stop_active": 4, "cooldown_active": 2}
        },
    )
    envelope = build_scorecard(**kwargs)
    scenario_json = envelope["scorecard_payload"]["strategies"]["S1"]["scenarios"][
        "primary_stress"
    ]
    assert scenario_json["daily_stop_active_count"] == 4
    assert scenario_json["no_trade_reason_counts"] == {
        "daily_stop_active": 4,
        "cooldown_active": 2,
    }
    base_json = envelope["scorecard_payload"]["strategies"]["S1"]["scenarios"]["base"]
    assert base_json["daily_stop_active_count"] == 0
    markdown = render_markdown(envelope)
    assert "daily_stop_active_count" in markdown or "daily_stop" in markdown


def test_full_no_trade_reason_counts_histogram_renders_in_markdown_and_changes_with_json():
    """Render EVERY new field from JSON in Markdown, not just the derived
    daily_stop_active_count -- the full canonical histogram too, without
    Markdown recomputing anything. Non-vacuous: mutating the underlying
    counts changes both JSON and Markdown consistently."""
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"] = _strategy_evidence_with(
        "S1",
        no_trade_reason_counts_by_scenario={
            "primary_stress": {"daily_stop_active": 4, "cooldown_active": 2}
        },
    )
    envelope = build_scorecard(**kwargs)
    scenario_json = envelope["scorecard_payload"]["strategies"]["S1"]["scenarios"][
        "primary_stress"
    ]
    markdown = render_markdown(envelope)
    assert str(scenario_json["no_trade_reason_counts"]) in markdown
    assert "cooldown_active" in markdown

    kwargs2 = _base_kwargs()
    kwargs2["strategies"]["S1"] = _strategy_evidence_with(
        "S1",
        no_trade_reason_counts_by_scenario={
            "primary_stress": {"daily_stop_active": 9, "tp_below_min_distance": 1}
        },
    )
    envelope2 = build_scorecard(**kwargs2)
    markdown2 = render_markdown(envelope2)
    assert markdown2 != markdown
    assert "tp_below_min_distance" in markdown2
    assert "cooldown_active" not in markdown2


def test_scenario_trade_count_deltas_preserve_the_independent_3_3_2_vector():
    """The known independent H2 fixture (3/3/2 trade counts across base/
    primary_stress/upward_stress) must remain VISIBLY 3/3/2 in the deltas --
    never path-equalized/silently revalued to match one scenario."""
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"] = _strategy_evidence_with(
        "S1", trade_counts={"base": 3, "primary_stress": 3, "upward_stress": 2}
    )
    envelope = build_scorecard(**kwargs)
    strategy_json = envelope["scorecard_payload"]["strategies"]["S1"]
    deltas = strategy_json["scenario_trade_count_deltas"]
    assert deltas["base_minus_primary_stress"] == 0
    assert deltas["primary_stress_minus_upward_stress"] == 1
    assert deltas["base_minus_upward_stress"] == 1
    markdown = render_markdown(envelope)
    assert "scenario_trade_count_deltas" in markdown or "trade_count_delta" in markdown


def test_scenario_trade_count_deltas_change_when_underlying_counts_change():
    """Non-vacuous mutation proof: changing ONLY the underlying trade
    counts must change the emitted deltas."""
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"] = _strategy_evidence_with(
        "S1", trade_counts={"base": 10, "primary_stress": 10, "upward_stress": 10}
    )
    envelope_equal = build_scorecard(**kwargs)
    deltas_equal = envelope_equal["scorecard_payload"]["strategies"]["S1"][
        "scenario_trade_count_deltas"
    ]
    assert deltas_equal == {
        "base_minus_primary_stress": 0,
        "primary_stress_minus_upward_stress": 0,
        "base_minus_upward_stress": 0,
    }

    kwargs2 = _base_kwargs()
    kwargs2["strategies"]["S1"] = _strategy_evidence_with(
        "S1", trade_counts={"base": 10, "primary_stress": 8, "upward_stress": 5}
    )
    envelope_diverged = build_scorecard(**kwargs2)
    deltas_diverged = envelope_diverged["scorecard_payload"]["strategies"]["S1"][
        "scenario_trade_count_deltas"
    ]
    assert deltas_diverged != deltas_equal
    assert deltas_diverged == {
        "base_minus_primary_stress": 2,
        "primary_stress_minus_upward_stress": 3,
        "base_minus_upward_stress": 5,
    }


def test_ex_btc_reference_subtotal_excludes_btc_and_is_reference_only():
    kwargs = _base_kwargs()
    envelope = build_scorecard(**kwargs)
    scenario_json = envelope["scorecard_payload"]["strategies"]["S1"]["scenarios"][
        "base"
    ]
    subtotal = scenario_json["ex_btc_reference_subtotal"]
    assert "BTCUSDT" not in subtotal["symbols"]
    assert set(subtotal["symbols"]) == {"XRPUSDT", "DOGEUSDT", "SOLUSDT"}
    assert subtotal["trade_count"] == 15  # 3 symbols x 5 trades each (fixture)
    assert subtotal["signal_count"] == 15
    assert subtotal["net_pnl_bps"] == pytest.approx(150.0)
    assert subtotal["pooled_expectancy_bps"] == pytest.approx(10.0)
    assert subtotal["reference_only"] is True
    assert subtotal["has_pass_rule"] is False
    markdown = render_markdown(envelope)
    assert (
        "ex_btc_reference_subtotal" in markdown
        or "ex-BTC" in markdown.lower()
        or "ex_btc" in markdown.lower()
    )


def test_ex_btc_reference_subtotal_changes_when_underlying_symbol_rows_change():
    """Non-vacuous mutation proof: the subtotal must reflect a changed
    symbol row, not a frozen/cached value."""

    def _symbol_metrics_with_xrp_zero_trades():
        from rob945_scenario_metrics import SymbolScenarioMetrics

        rows = []
        for s in _SYMBOLS:
            if s == "XRPUSDT":
                rows.append(
                    SymbolScenarioMetrics(
                        symbol=s,
                        trade_count=0,
                        signal_count=1,
                        net_expectancy_bps=None,
                        net_pnl_bps=0.0,
                    )
                )
            else:
                rows.append(
                    SymbolScenarioMetrics(
                        symbol=s,
                        trade_count=5,
                        signal_count=5,
                        net_expectancy_bps=10.0,
                        net_pnl_bps=50.0,
                    )
                )
        return tuple(rows)

    kwargs = _base_kwargs()
    mutated_scenario = StrategyScenarioAggregate(
        strategy="S1",
        scenario_name="base",
        trade_count=20,
        net_expectancy_bps=None,
        pooled_expectancy_bps=10.0,
        profit_factor=2.0,
        win_rate=0.6,
        net_pnl_bps=200.0,
        timeout_ratio=0.1,
        mdd_r=1.5,
        mdd_reason=None,
        monthly_concentration=0.3,
        monthly_concentration_reason=None,
        symbol_metrics=_symbol_metrics_with_xrp_zero_trades(),
        incomplete=True,
        incomplete_reason="insufficient_oos_symbol_evidence",
    )
    kwargs["strategies"]["S1"]["scenarios"]["base"] = mutated_scenario
    envelope = build_scorecard(**kwargs)
    subtotal = envelope["scorecard_payload"]["strategies"]["S1"]["scenarios"]["base"][
        "ex_btc_reference_subtotal"
    ]
    assert subtotal["trade_count"] == 10  # DOGE+SOL only now (5 each), XRP zeroed
    assert subtotal["net_pnl_bps"] == pytest.approx(100.0)


def test_s2_spec_deviation_register_present_only_for_s2_with_rejection_counts():
    kwargs = _base_kwargs()
    kwargs["strategies"]["S2"] = _strategy_evidence_with(
        "S2",
        no_trade_reason_counts_by_scenario={
            "primary_stress": {"target_direction_invalid": 7, "tp_above_max": 2},
            "upward_stress": {"target_direction_invalid": 3},
        },
    )
    envelope = build_scorecard(**kwargs)
    s1_json = envelope["scorecard_payload"]["strategies"]["S1"]
    s2_json = envelope["scorecard_payload"]["strategies"]["S2"]
    # Captain precision: S1 must OMIT the key entirely, never carry it as None.
    assert "spec_deviation_register" not in s1_json
    register = s2_json["spec_deviation_register"]
    assert "direction-validity gate" in register["statement"]
    assert "label contamination" in register["statement"]
    assert (
        register["rejection_counts_by_scenario"]["primary_stress"][
            "target_direction_invalid"
        ]
        == 7
    )
    assert (
        register["rejection_counts_by_scenario"]["primary_stress"]["tp_above_max"] == 2
    )
    assert register["total_rejection_counts"]["target_direction_invalid"] == 10
    markdown = render_markdown(envelope)
    assert "target_direction_invalid" in markdown


def test_symbol_metrics_duplicate_symbol_fails_closed():
    """Captain precision: exact frozen symbol ORDER/no-duplicates -- set
    equality alone would accept a duplicate BTCUSDT row silently replacing
    SOLUSDT."""
    from rob945_scenario_metrics import SymbolScenarioMetrics

    kwargs = _base_kwargs()
    duplicated_symbol_metrics = tuple(
        SymbolScenarioMetrics(
            symbol="BTCUSDT" if s == "SOLUSDT" else s,
            trade_count=5,
            signal_count=5,
            net_expectancy_bps=10.0,
            net_pnl_bps=50.0,
        )
        for s in _SYMBOLS
    )
    mutated = StrategyScenarioAggregate(
        strategy="S1",
        scenario_name="base",
        trade_count=20,
        net_expectancy_bps=10.0,
        pooled_expectancy_bps=10.0,
        profit_factor=2.0,
        win_rate=0.6,
        net_pnl_bps=200.0,
        timeout_ratio=0.1,
        mdd_r=1.5,
        mdd_reason=None,
        monthly_concentration=0.3,
        monthly_concentration_reason=None,
        symbol_metrics=duplicated_symbol_metrics,
        incomplete=False,
        incomplete_reason=None,
    )
    kwargs["strategies"]["S1"]["scenarios"]["base"] = mutated
    with pytest.raises(ScorecardInputError):
        build_scorecard(**kwargs)


def test_symbol_metrics_out_of_order_fails_closed():
    """Same frozen SET, wrong order -- must still fail closed (set
    equality alone is not enough)."""
    from rob945_scenario_metrics import SymbolScenarioMetrics

    kwargs = _base_kwargs()
    reversed_symbol_metrics = tuple(
        SymbolScenarioMetrics(
            symbol=s,
            trade_count=5,
            signal_count=5,
            net_expectancy_bps=10.0,
            net_pnl_bps=50.0,
        )
        for s in reversed(_SYMBOLS)
    )
    mutated = StrategyScenarioAggregate(
        strategy="S1",
        scenario_name="base",
        trade_count=20,
        net_expectancy_bps=10.0,
        pooled_expectancy_bps=10.0,
        profit_factor=2.0,
        win_rate=0.6,
        net_pnl_bps=200.0,
        timeout_ratio=0.1,
        mdd_r=1.5,
        mdd_reason=None,
        monthly_concentration=0.3,
        monthly_concentration_reason=None,
        symbol_metrics=reversed_symbol_metrics,
        incomplete=False,
        incomplete_reason=None,
    )
    kwargs["strategies"]["S1"]["scenarios"]["base"] = mutated
    with pytest.raises(ScorecardInputError):
        build_scorecard(**kwargs)


def test_ex_btc_reference_subtotal_symbols_are_exactly_xrp_doge_sol_in_frozen_order():
    kwargs = _base_kwargs()
    envelope = build_scorecard(**kwargs)
    subtotal = envelope["scorecard_payload"]["strategies"]["S1"]["scenarios"]["base"][
        "ex_btc_reference_subtotal"
    ]
    assert subtotal["symbols"] == ["XRPUSDT", "DOGEUSDT", "SOLUSDT"]


def test_scenarios_and_rejection_counts_render_in_frozen_scenario_order_regardless_of_input():
    """Captain precision: emit scenarios/rejection_counts_by_scenario in
    _REQUIRED_SCENARIOS order, independent of the caller Mapping's own
    insertion order."""
    kwargs = _base_kwargs()
    reversed_evidence = _strategy_evidence_with(
        "S2",
        no_trade_reason_counts_by_scenario={
            "primary_stress": {"target_direction_invalid": 7},
            "upward_stress": {"target_direction_invalid": 3},
        },
    )
    # Reverse the caller's own scenarios mapping insertion order.
    reversed_evidence["scenarios"] = dict(
        reversed(list(reversed_evidence["scenarios"].items()))
    )
    kwargs["strategies"]["S2"] = reversed_evidence
    envelope = build_scorecard(**kwargs)
    s2_json = envelope["scorecard_payload"]["strategies"]["S2"]
    assert list(s2_json["scenarios"].keys()) == [
        "base",
        "primary_stress",
        "upward_stress",
    ]
    assert list(
        s2_json["spec_deviation_register"]["rejection_counts_by_scenario"].keys()
    ) == ["base", "primary_stress", "upward_stress"]


# ===========================================================================
# Final-fix -- I-5 / Task 6.1: top-level campaign_reason_codes
# ===========================================================================


def test_campaign_reason_codes_empty_for_a_genuine_clean_historical_pass():
    kwargs = _base_kwargs()
    envelope = build_scorecard(**kwargs)
    body = envelope["scorecard_payload"]
    assert body["campaign_verdict"] == "historical_pass"
    assert body["campaign_reason_codes"] == []
    markdown = render_markdown(envelope)
    assert "campaign_reason_codes" in markdown


def test_campaign_reason_codes_dedupe_when_both_strategies_share_a_reason():
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"]["capture_valid"] = False
    kwargs["strategies"]["S2"]["capture_valid"] = False
    envelope = build_scorecard(**kwargs)
    body = envelope["scorecard_payload"]
    assert body["campaign_verdict"] == "incomplete"
    assert body["campaign_reason_codes"] == ["capture_invalid"]


def test_campaign_reason_codes_sorted_when_strategies_have_distinct_reasons():
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"]["capture_valid"] = False
    kwargs["strategies"]["S2"]["pbo_valid"] = False
    envelope = build_scorecard(**kwargs)
    body = envelope["scorecard_payload"]
    assert body["campaign_verdict"] == "incomplete"
    assert body["campaign_reason_codes"] == ["capture_invalid", "pbo_grid_invalid"]


def test_campaign_reason_codes_incomplete_precedence_only_driving_incomplete_reasons():
    """When campaign_verdict is 'incomplete' (S1 incomplete, S2 would
    otherwise historical_fail), ONLY the driving INCOMPLETE strategy's
    reason codes are included -- S2's fail reasons never leak in, since
    incomplete precedence means they never drove the campaign verdict."""
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"]["capture_valid"] = False
    kwargs["strategies"]["S2"]["scenarios"]["primary_stress"] = _scenario(
        "primary_stress", net_expectancy_bps=1.0, strategy="S2"
    )
    envelope = build_scorecard(**kwargs)
    body = envelope["scorecard_payload"]
    assert body["campaign_verdict"] == "incomplete"
    assert body["campaign_reason_codes"] == ["capture_invalid"]
    assert "expectancy_below_5bp_threshold" not in body["campaign_reason_codes"]


def test_campaign_reason_codes_include_accounting_incomplete_reason():
    kwargs = _base_kwargs()
    kwargs["accounting_report"] = _clean_accounting_report(
        _derive_campaign_run_id(_REAL_FULL_CAMPAIGN_HASH),
        actual_registrations=25,
        extra_experiment_ids=["some-unexpected-registered-id"],
        verdict="incomplete",
    )
    envelope = build_scorecard(**kwargs)
    body = envelope["scorecard_payload"]
    assert body["campaign_verdict"] == "incomplete"
    assert set(body["campaign_reason_codes"]) & set(
        body["lineage"]["accounting_reason_codes"]
    )


def test_campaign_reason_codes_historical_fail_only_from_failing_strategies():
    """Both strategies accounting-complete; S1 fails one criterion, S2
    passes cleanly -- campaign_verdict is historical_fail and
    campaign_reason_codes carries ONLY S1's fail reasons."""
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"]["scenarios"]["primary_stress"] = _scenario(
        "primary_stress", net_expectancy_bps=1.0, strategy="S1"
    )
    envelope = build_scorecard(**kwargs)
    body = envelope["scorecard_payload"]
    assert body["campaign_verdict"] == "historical_fail"
    assert body["campaign_reason_codes"] == ["expectancy_below_5bp_threshold"]
    markdown = render_markdown(envelope)
    assert "expectancy_below_5bp_threshold" in markdown


def test_campaign_reason_codes_union_accounting_incomplete_plus_strategy_incomplete():
    """Captain precision: sealed accounting incomplete AND a strategy ALSO
    incomplete -> the union of BOTH sets of reasons, never just one side.
    A THIRD strategy that would otherwise have been historical_fail (S2,
    low expectancy) must still be excluded entirely -- incomplete
    precedence means its fail reasons never drove the campaign verdict."""
    kwargs = _base_kwargs()
    kwargs["accounting_report"] = _clean_accounting_report(
        _derive_campaign_run_id(_REAL_FULL_CAMPAIGN_HASH),
        actual_registrations=25,
        extra_experiment_ids=["some-unexpected-registered-id"],
        verdict="incomplete",
    )
    kwargs["strategies"]["S1"]["capture_valid"] = False
    kwargs["strategies"]["S2"]["scenarios"]["primary_stress"] = _scenario(
        "primary_stress", net_expectancy_bps=1.0, strategy="S2"
    )
    envelope = build_scorecard(**kwargs)
    body = envelope["scorecard_payload"]
    assert body["campaign_verdict"] == "incomplete"
    accounting_reasons = set(body["lineage"]["accounting_reason_codes"])
    assert accounting_reasons  # sanity: the forged report really is incomplete
    expected = sorted(accounting_reasons | {"capture_invalid"})
    assert body["campaign_reason_codes"] == expected
    assert "expectancy_below_5bp_threshold" not in body["campaign_reason_codes"]


def test_campaign_reason_codes_deterministic_regardless_of_strategies_mapping_order():
    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"]["capture_valid"] = False
    kwargs["strategies"]["S2"]["pbo_valid"] = False
    forward = build_scorecard(**kwargs)

    kwargs_reversed = _base_kwargs()
    kwargs_reversed["strategies"]["S1"]["capture_valid"] = False
    kwargs_reversed["strategies"]["S2"]["pbo_valid"] = False
    kwargs_reversed["strategies"] = dict(
        reversed(list(kwargs_reversed["strategies"].items()))
    )
    reversed_envelope = build_scorecard(**kwargs_reversed)

    assert (
        forward["scorecard_payload"]["campaign_reason_codes"]
        == reversed_envelope["scorecard_payload"]["campaign_reason_codes"]
        == ["capture_invalid", "pbo_grid_invalid"]
    )


def test_markdown_renders_the_exact_json_campaign_reason_codes_array():
    import json

    kwargs = _base_kwargs()
    kwargs["strategies"]["S1"]["capture_valid"] = False
    kwargs["strategies"]["S2"]["pbo_valid"] = False
    envelope = build_scorecard(**kwargs)
    body = envelope["scorecard_payload"]
    markdown = render_markdown(envelope)
    expected_json_array = json.dumps(body["campaign_reason_codes"])
    assert '"capture_invalid"' in expected_json_array  # sanity: real JSON syntax
    assert f"campaign_reason_codes: {expected_json_array}" in markdown
