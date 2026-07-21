"""ROB-1001 CP-B2 -- H5 consumes only the typed H4.5 attribution seam."""

from __future__ import annotations

from dataclasses import replace

import pytest
import test_rob974_h5_cp6_canonical_json as cp6fx
import test_rob1001_h45_attribution as h4fx
from rob974_h4_runner import (
    bind_s3_attribution_path,
    bind_s4_attribution_path,
    build_actual_attribution_envelope,
    build_deferred_attribution_envelope,
    build_tercile_authority,
)
from rob974_h5_canonical import (
    StrategyCanonicalInputs,
    build_canonical_scorecard,
    canonical_json_bytes,
)
from rob974_h5_contracts import (
    CampaignEnvelope,
    H5InputError,
    consume_h4_attribution,
    fixture_h4_attribution_result,
)
from rob974_h5_dual_evidence import (
    PathInvocationEvidence,
    cross_check_h4_attribution_contract,
)
from rob974_h5_gates import evaluate_common_gates
from rob974_h5_markdown import render_markdown
from rob974_h5_s3 import (
    S3_ABS_M_BIN_ORDER,
    S3_ABS_S_BIN_ORDER,
    S3_Q_BIN_ORDER,
    S3_VOLATILITY_BIN_ORDER,
    evaluate_s3_falsification,
)
from rob974_h5_s4 import (
    S4_ABS_Z_BIN_ORDER,
    S4_D_BIN_ORDER,
    S4_HALF_LIFE_BIN_ORDER,
    S4_HISTORICAL_PAIR_EXECUTOR_STATE,
    S4_MARKET_RETURN_BIN_ORDER,
    S4_RHO_BIN_ORDER,
    compute_campaign_decision,
    compute_direct_verdict,
    evaluate_s4_falsification,
)


@pytest.fixture(scope="module")
def actual_h4_contract():
    plan = h4fx.build_production_h4_plan()
    authority = build_tercile_authority(
        fold_id="fold-00",
        train_start_ms=0,
        train_end_ms=h4fx._SIGNAL_TS,
        snapshots=(
            h4fx._snapshot(0, m=9.0, M=0.0),
            h4fx._snapshot(60_000, m=9.0, M=0.01),
            h4fx._snapshot(120_000, m=9.0, M=0.03),
        ),
    )
    s3_candidate = h4fx._s3_candidate()
    s4_candidate = h4fx._s4_candidate()
    s3_terminal = h4fx._s3_terminal(s3_candidate)
    s4_terminal = h4fx._s4_terminal(s4_candidate)
    paths = []
    for scenario in ("base13", "primary_stress17", "upward_stress22"):
        paths.append(
            bind_s3_attribution_path(
                row_spec=plan.row_specs[0],
                fold_id="fold-00",
                path_scenario=scenario,
                candidates=(s3_candidate,),
                terminal=s3_terminal,
                corpus_end_ts=h4fx._CORPUS_END_TS,
                horizon_end_ts=None,
                decision_snapshots=(h4fx._snapshot(h4fx._SIGNAL_TS, m=-0.4, M=0.02),),
                tercile_authority=authority,
            )
        )
        paths.append(
            bind_s4_attribution_path(
                row_spec=plan.row_specs[24],
                fold_id="fold-00",
                path_scenario=scenario,
                candidates=(s4_candidate,),
                terminal=s4_terminal,
                corpus_end_ts=h4fx._CORPUS_END_TS,
                horizon_end_ts=None,
                decision_snapshots=(h4fx._snapshot(h4fx._SIGNAL_TS, m=-0.4, M=0.04),),
            )
        )
    envelope = build_actual_attribution_envelope(
        plan=plan,
        paths=tuple(paths),
        tercile_authorities=(authority,),
    )
    return plan, envelope, consume_h4_attribution(envelope)


def test_actual_consumer_preserves_lineage_and_M_t_without_legacy_4h_alias(
    actual_h4_contract,
):
    plan, envelope, result = actual_h4_contract
    assert result.actual_h4_contract == "PASS"
    assert result.contract_provenance == "actual"
    assert result.incomplete_reasons == ()
    assert result.envelope is envelope
    assert len(result.trades) == 6

    s3 = next(
        trade
        for trade in result.trades
        if trade.strategy == "S3" and trade.path_scenario == "primary_stress17"
    )
    assert s3.config_id == s3.attribution.row_id == "S3-00"
    assert s3.attribution.experiment_id == plan.row_specs[0].experiment_id
    assert s3.attribution.S == 1.8
    assert s3.attribution.Q == 0.6
    assert s3.attribution.market_return == 0.02
    assert s3.attribution.market_return_tercile == "top"
    assert s3.holding_minutes == s3.attribution.realized_holding_minutes == 2.0
    assert s3.market_return_4h is None
    assert s3.net_bps == s3.attribution.e17_bps

    s4 = next(
        trade
        for trade in result.trades
        if trade.strategy == "S4" and trade.path_scenario == "primary_stress17"
    )
    assert s4.attribution.market_return == 0.04
    assert s4.market_return_4h is None
    assert s4.attribution.realized_pair_beta == 0.0
    assert s4.attribution.entry_z == 1.9
    assert s4.attribution.D == 200.0
    assert s4.attribution.correlation == 0.72
    assert s4.attribution.half_life == 4.0
    assert s4.attribution.beta_stability == 0.10


def test_fixture_and_deferred_can_never_claim_actual_pass(actual_h4_contract):
    plan, _, _ = actual_h4_contract
    fixture = fixture_h4_attribution_result()
    assert fixture.actual_h4_contract == "FIXTURE_ONLY"
    assert fixture.contract_provenance == "fixture"
    assert fixture.trades == ()

    deferred_envelope = build_deferred_attribution_envelope(
        plan=plan, reason="h6b_empirical_materializer_pending"
    )
    deferred = consume_h4_attribution(deferred_envelope)
    assert deferred.actual_h4_contract == "DEFERRED"
    assert deferred.contract_provenance == "deferred"
    assert deferred.trades == ()
    assert deferred.incomplete_reasons == ("h6b_empirical_materializer_pending",)


def test_h4_paths_bind_to_h5_dual_evidence_without_fabricating_member_keys(
    actual_h4_contract,
):
    _, envelope, result = actual_h4_contract
    path_evidence = {}
    for path in envelope.paths:
        key = (path.lineage.row_id, path.lineage.fold_id, path.path_scenario)
        path_evidence[key] = PathInvocationEvidence(
            strategy=path.strategy,
            config_id=path.lineage.row_id,
            fold_id=path.lineage.fold_id,
            path_scenario=path.path_scenario,
            unique_evidence_hash="a" * 64,
            unique_evidence_accepted_count=path.engine_input_count,
            engine_input_hash=path.terminal.input_seal_sha256,
            engine_input_count=path.engine_input_count,
            no_trade_reason_counts={},
            ledger_status="completed",
            trade_count=len(path.rows),
            artifact_hash="b" * 64,
        )
    cross = cross_check_h4_attribution_contract(
        h4_contract=result, paths_by_key=path_evidence
    )
    assert cross.ok is True
    assert cross.path_count == 6
    assert cross.trade_count == 6
    assert cross.raw_member_key_cross_seal == "DEFERRED_TO_H6B_INTEGRATION_E2E"

    missing_path_evidence = dict(path_evidence)
    missing_path_evidence.pop(next(iter(missing_path_evidence)))
    with pytest.raises(H5InputError, match="evidence_set_mismatch"):
        cross_check_h4_attribution_contract(
            h4_contract=result,
            paths_by_key=missing_path_evidence,
        )

    first_key = next(iter(path_evidence))
    path_evidence[first_key] = replace(path_evidence[first_key], engine_input_count=99)
    with pytest.raises(H5InputError, match="engine_input_count"):
        cross_check_h4_attribution_contract(
            h4_contract=result, paths_by_key=path_evidence
        )


def test_s3_registered_attribution_bins_carry_all_three_economics(
    actual_h4_contract,
):
    _, _, contract = actual_h4_contract
    primary = tuple(
        trade
        for trade in contract.trades
        if trade.strategy == "S3" and trade.path_scenario == "primary_stress17"
    )
    upward = tuple(
        trade
        for trade in contract.trades
        if trade.strategy == "S3" and trade.path_scenario == "upward_stress22"
    )
    result = evaluate_s3_falsification(primary_trades=primary, upward_trades=upward)
    assert tuple(result.attribution["by_abs_S_bin"]) == S3_ABS_S_BIN_ORDER
    assert tuple(result.attribution["by_pullback_Q_bin"]) == S3_Q_BIN_ORDER
    assert tuple(result.attribution["by_abs_M_bin"]) == S3_ABS_M_BIN_ORDER
    assert (
        tuple(result.attribution["by_volatility_percentile_bin"])
        == S3_VOLATILITY_BIN_ORDER
    )
    bucket = result.attribution["by_abs_S_bin"]["[1.75,2.50)"]
    trade = primary[0]
    assert bucket == {
        "trades": 1,
        "e0_bps": trade.gross_bps,
        "e13_bps": trade.attribution.e13_bps,
        "e17_bps": trade.attribution.e17_bps,
        "e22_bps": trade.attribution.e22_bps,
        "pf17": 0.0,
        "avg_holding_minutes": 2.0,
    }
    cross = result.attribution["top_tercile_by_direction_and_abs_M_bin"]
    assert cross["Long"]["[0.015,0.03)"]["trades"] == 1
    assert cross["Short"]["[0.015,0.03)"]["trades"] == 0

    drifted_upward = (
        replace(
            upward[0],
            attribution=replace(upward[0].attribution, experiment_id="f" * 64),
        ),
    )
    with pytest.raises(H5InputError, match="experiment_identity_mismatch"):
        evaluate_s3_falsification(primary_trades=primary, upward_trades=drifted_upward)


def test_s4_registered_bins_use_M_t_and_preserve_raw_beta_fields(actual_h4_contract):
    _, _, contract = actual_h4_contract
    primary = tuple(
        trade
        for trade in contract.trades
        if trade.strategy == "S4" and trade.path_scenario == "primary_stress17"
    )
    upward = tuple(
        trade
        for trade in contract.trades
        if trade.strategy == "S4" and trade.path_scenario == "upward_stress22"
    )
    result = evaluate_s4_falsification(primary_trades=primary, upward_trades=upward)
    assert tuple(result.attribution["by_abs_z_bin"]) == S4_ABS_Z_BIN_ORDER
    assert tuple(result.attribution["by_D_bps_bin"]) == S4_D_BIN_ORDER
    assert tuple(result.attribution["by_rho_bin"]) == S4_RHO_BIN_ORDER
    assert tuple(result.attribution["by_half_life_hours_bin"]) == S4_HALF_LIFE_BIN_ORDER
    assert tuple(result.attribution["by_M_24h_bin"]) == S4_MARKET_RETURN_BIN_ORDER
    assert result.attribution["by_abs_z_bin"]["[z_entry,2.2)"]["trades"] == 1
    assert result.attribution["by_D_bps_bin"]["[200,300)"]["trades"] == 1
    assert result.attribution["by_rho_bin"]["[0.70,0.80)"]["trades"] == 1
    assert result.attribution["by_half_life_hours_bin"]["[16,32)"]["trades"] == 1
    assert result.attribution["by_M_24h_bin"]["(0.03,inf)"]["trades"] == 1
    assert primary[0].attribution.realized_pair_beta == 0.0
    assert primary[0].attribution.beta_stability == 0.10


def _campaign_envelope_for_h4(h4_envelope, **overrides) -> CampaignEnvelope:
    fields = {
        "full_campaign_hash": h4_envelope.full_campaign_hash,
        "campaign_run_id": h4_envelope.campaign_run_id,
        "h4_runner_source_hash": h4_envelope.h4_source_pins.runner_bundle_sha256,
        "h4_pbo_source_hash": h4_envelope.h4_source_pins.pbo_source_sha256,
        "h2_engine_source_hash": h4_envelope.source_pins.engine_source_sha256,
    }
    fields.update(overrides)
    return cp6fx._envelope(**fields)


def _actual_canonical_inputs(contract):
    paths_by_strategy = {"S3": {}, "S4": {}}
    for path in contract.envelope.paths:
        paths_by_strategy[path.strategy][
            (path.lineage.row_id, path.lineage.fold_id, path.path_scenario)
        ] = PathInvocationEvidence(
            strategy=path.strategy,
            config_id=path.lineage.row_id,
            fold_id=path.lineage.fold_id,
            path_scenario=path.path_scenario,
            unique_evidence_hash="a" * 64,
            unique_evidence_accepted_count=path.engine_input_count,
            engine_input_hash=path.terminal.input_seal_sha256,
            engine_input_count=path.engine_input_count,
            no_trade_reason_counts={},
            ledger_status="completed",
            trade_count=len(path.rows),
            artifact_hash="b" * 64,
        )

    strategy_inputs = {}
    for strategy in ("S3", "S4"):
        primary = tuple(
            trade
            for trade in contract.trades
            if trade.strategy == strategy and trade.path_scenario == "primary_stress17"
        )
        upward = tuple(
            trade
            for trade in contract.trades
            if trade.strategy == strategy and trade.path_scenario == "upward_stress22"
        )
        common = evaluate_common_gates(primary_trades=primary, upward_trades=upward)
        if strategy == "S3":
            falsification = evaluate_s3_falsification(
                primary_trades=primary, upward_trades=upward
            )
            exit_order = ("TP", "SL", "THESIS_EXIT", "TIMEOUT")
            dimension_order = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
            executor_state = None
        else:
            falsification = evaluate_s4_falsification(
                primary_trades=primary, upward_trades=upward
            )
            exit_order = ("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT")
            dimension_order = ("XRP-DOGE", "XRP-SOL", "DOGE-SOL")
            executor_state = S4_HISTORICAL_PAIR_EXECUTOR_STATE
        direct = compute_direct_verdict(
            incomplete_reasons=falsification.incomplete_reasons,
            hard_gate_reasons=common.reasons + falsification.reasons,
        )
        strategy_inputs[strategy] = StrategyCanonicalInputs(
            strategy=strategy,
            common_gates=common,
            falsification=falsification,
            direct_verdict=direct,
            exit_reason_order=exit_order,
            dimension_order=dimension_order,
            unique_by_key={},
            paths_by_key=paths_by_strategy[strategy],
            pbo=None,
            pair_executor_state=executor_state,
        )
    return strategy_inputs["S3"], strategy_inputs["S4"]


def _build_actual_scorecard(actual_h4_contract, **envelope_overrides):
    _, h4_envelope, contract = actual_h4_contract
    s3_inputs, s4_inputs = _actual_canonical_inputs(contract)
    campaign = compute_campaign_decision(
        s3_direct_verdict=s3_inputs.direct_verdict,
        s4_direct_verdict=s4_inputs.direct_verdict,
    )
    return build_canonical_scorecard(
        envelope=_campaign_envelope_for_h4(h4_envelope, **envelope_overrides),
        h6a_seal=cp6fx._seal(),
        envelope_ok=True,
        envelope_incomplete_reasons=(),
        h4_attribution=contract,
        s3_inputs=s3_inputs,
        s4_inputs=s4_inputs,
        campaign_decision=campaign,
    )


def test_canonical_json_seals_actual_contract_raw_rows_and_registered_bins(
    actual_h4_contract,
):
    scorecard = _build_actual_scorecard(actual_h4_contract)
    contract = scorecard["h4_attribution_contract"]
    assert scorecard["schema_version"] == "h5_scorecard_v2"
    assert contract["actual_h4_contract"] == "PASS"
    assert contract["contract_provenance"] == "actual"
    assert contract["market_return_semantic"] == "M_t_24h_median_log_return"
    assert contract["typed_path_cross_check"] == "PASS"
    assert contract["fake_free_empirical_closure"] == (
        "DEFERRED_TO_H6B_INTEGRATION_E2E"
    )
    assert (
        contract["producer_seal_sha256"] == actual_h4_contract[1].producer_seal_sha256
    )
    assert contract["paths"][0]["h2_input_seal_sha256"]
    assert contract["source_pins"]["engine_source_sha256"]

    s3_rows = scorecard["strategies"]["S3"]["raw_attribution_rows"]
    s4_rows = scorecard["strategies"]["S4"]["raw_attribution_rows"]
    assert len(s3_rows) == len(s4_rows) == 3
    assert "entry_z" not in s3_rows[0]
    assert s3_rows[0]["market_return_semantic"] == "M_t_24h_median_log_return"
    assert s4_rows[0]["entry_z"] == 1.9
    assert s4_rows[0]["realized_pair_beta"] == 0.0
    assert (
        tuple(
            scorecard["strategies"]["S3"]["falsification"]["attribution"][
                "by_abs_S_bin"
            ]
        )
        == S3_ABS_S_BIN_ORDER
    )
    assert (
        tuple(
            scorecard["strategies"]["S4"]["falsification"]["attribution"][
                "by_M_24h_bin"
            ]
        )
        == S4_MARKET_RETURN_BIN_ORDER
    )
    canonical_json_bytes(scorecard)


def test_markdown_renders_only_canonical_attribution_contract(actual_h4_contract):
    markdown = render_markdown(_build_actual_scorecard(actual_h4_contract)).decode()
    assert "## H4 Attribution Contract" in markdown
    assert "actual_h4_contract: PASS" in markdown
    assert "contract_provenance: actual" in markdown
    assert "fake_free_empirical_closure: DEFERRED_TO_H6B_INTEGRATION_E2E" in markdown
    assert "### Raw Attribution Rows" in markdown
    assert "realized_pair_beta=0.0" in markdown


def test_fixture_canonical_status_is_explicit_and_has_no_raw_rows():
    s3_inputs = cp6fx._build_strategy_inputs("S3")
    s4_inputs = cp6fx._build_strategy_inputs("S4")
    fixture = fixture_h4_attribution_result()
    scorecard = build_canonical_scorecard(
        envelope=cp6fx._envelope(),
        h6a_seal=cp6fx._seal(),
        envelope_ok=True,
        envelope_incomplete_reasons=(),
        h4_attribution=fixture,
        s3_inputs=s3_inputs,
        s4_inputs=s4_inputs,
        campaign_decision=compute_campaign_decision(
            s3_direct_verdict=s3_inputs.direct_verdict,
            s4_direct_verdict=s4_inputs.direct_verdict,
        ),
    )
    assert scorecard["h4_attribution_contract"]["actual_h4_contract"] == "FIXTURE_ONLY"
    assert scorecard["h4_attribution_contract"]["typed_path_cross_check"] == (
        "NOT_APPLICABLE"
    )
    assert scorecard["strategies"]["S3"]["raw_attribution_rows"] == []
    assert scorecard["strategies"]["S4"]["raw_attribution_rows"] == []


def test_deferred_canonical_status_forces_zero_paths_and_rows(actual_h4_contract):
    plan, _, _ = actual_h4_contract
    deferred_envelope = build_deferred_attribution_envelope(
        plan=plan, reason="h6b_empirical_materializer_pending"
    )
    deferred = consume_h4_attribution(deferred_envelope)
    s3_inputs = cp6fx._build_strategy_inputs("S3")
    s4_inputs = cp6fx._build_strategy_inputs("S4")
    scorecard = build_canonical_scorecard(
        envelope=_campaign_envelope_for_h4(deferred_envelope),
        h6a_seal=cp6fx._seal(),
        envelope_ok=True,
        envelope_incomplete_reasons=(),
        h4_attribution=deferred,
        s3_inputs=s3_inputs,
        s4_inputs=s4_inputs,
        campaign_decision=compute_campaign_decision(
            s3_direct_verdict=s3_inputs.direct_verdict,
            s4_direct_verdict=s4_inputs.direct_verdict,
        ),
    )
    contract = scorecard["h4_attribution_contract"]
    assert contract["actual_h4_contract"] == "DEFERRED"
    assert contract["contract_provenance"] == "deferred"
    assert contract["path_count"] == contract["trade_count"] == 0
    assert contract["paths"] == []
    assert scorecard["strategies"]["S3"]["raw_attribution_rows"] == []


def test_canonical_rejects_stale_campaign_source_pin(actual_h4_contract):
    with pytest.raises(H5InputError, match="runner_source_pin_mismatch"):
        _build_actual_scorecard(actual_h4_contract, h4_runner_source_hash="f" * 64)


def test_canonical_rejects_aggregate_not_derived_from_h4_rows(actual_h4_contract):
    _, h4_envelope, contract = actual_h4_contract
    s3_inputs, s4_inputs = _actual_canonical_inputs(contract)
    forged_s3 = replace(
        s3_inputs,
        common_gates=replace(s3_inputs.common_gates, pooled_e17_bps=999.0),
    )
    campaign = compute_campaign_decision(
        s3_direct_verdict=s3_inputs.direct_verdict,
        s4_direct_verdict=s4_inputs.direct_verdict,
    )
    with pytest.raises(H5InputError, match="s3_h4_common_gates_mismatch"):
        build_canonical_scorecard(
            envelope=_campaign_envelope_for_h4(h4_envelope),
            h6a_seal=cp6fx._seal(),
            envelope_ok=True,
            envelope_incomplete_reasons=(),
            h4_attribution=contract,
            s3_inputs=forged_s3,
            s4_inputs=s4_inputs,
            campaign_decision=campaign,
        )
