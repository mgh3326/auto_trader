"""ROB-1001 CP-B1 -- H4.5 selected-OOS raw attribution seam.

The seam binds exact merged H3 candidates, exact merged H2 terminal/scenario
rows, and exact H6-A production row identity without a downstream join.  All
fixtures are hermetic; this module performs no corpus, DB, broker, or network
operation.
"""

from __future__ import annotations

from dataclasses import fields, replace

import pytest
from rob974_features import CommonSnapshot, SymbolFeature
from rob974_h2_dtos import S3EngineResult, S3Trade, S4EngineResult, S4PairTrade
from rob974_h3_h2_adapter import adapt_s3_candidate, adapt_s4_candidate
from rob974_h3_manifest import SYMBOLS
from rob974_h3_s3 import S3Candidate
from rob974_h3_s4 import HISTORICAL_NOTIONAL_ASSUMPTION, S4Candidate
from rob974_h4_adapter import (
    SealedS3Terminal,
    SealedS4Terminal,
    seal_s3_engine_input,
    seal_s3_engine_output,
    seal_s4_engine_input,
    seal_s4_engine_output,
)
from rob974_h4_contracts import (
    ATTRIBUTION_SCHEMA_VERSION,
    MARKET_RETURN_SEMANTIC,
    attribution_contract,
)
from rob974_h4_h6a_adapter import build_production_h4_plan
from rob974_h4_runner import (
    H4AttributionError,
    S3SelectedOOSAttribution,
    S4SelectedOOSAttribution,
    assign_market_return_tercile,
    bind_s3_attribution_path,
    bind_s4_attribution_path,
    build_actual_attribution_envelope,
    build_deferred_attribution_envelope,
    build_tercile_authority,
    validate_attribution_envelope,
)

_MINUTE_MS = 60_000
_SIGNAL_TS = 240_000
_CORPUS_END_TS = 10_000_000_000


def _snapshot(ts: int, *, m: float, M: float) -> CommonSnapshot:
    features = tuple(
        SymbolFeature(symbol, ts, None, None, None, None, None, None, None, None)
        for symbol in SYMBOLS
    )
    return CommonSnapshot(ts, m, M, 2, 1, features)


def _s3_candidate() -> S3Candidate:
    return S3Candidate(
        strategy="S3",
        config_id="S3-00",
        decision_ts=_SIGNAL_TS,
        symbol="XRPUSDT",
        side="long",
        R=0.03,
        S=1.8,
        ER=0.5,
        Q=0.6,
        A=0.01,
        atr20=0.01,
        close=1.0,
        vwap12=0.99,
        vwap24=0.98,
        market_return_24h=0.02,
        current_market_return_4h=-0.4,
        volatility_percentile=55.0,
        volatility_percentile_provenance="h1_percentile_30d",
        range24=0.10,
        d_SL=0.01,
        d_TP=0.015,
        entry_tick_ts=_SIGNAL_TS,
        entry_deadline_ts=_SIGNAL_TS + _MINUTE_MS,
        max_hold_4h_bars=12,
    )


def _s4_candidate() -> S4Candidate:
    return S4Candidate(
        strategy="S4",
        config_id="S4-00",
        decision_ts=_SIGNAL_TS,
        pair="XRP-DOGE",
        side="short_a_long_b",
        symbol_a="XRPUSDT",
        symbol_b="DOGEUSDT",
        side_a="short",
        side_b="long",
        beta_a=1.2,
        beta_b=0.8,
        weight_a=0.4,
        weight_b=0.6,
        mu=0.0,
        mad=0.03,
        effective_mad_scale=0.05,
        observed_z=1.9,
        prior_observed_z=2.2,
        D_fraction=0.02,
        D_bps=200.0,
        rho=0.72,
        half_life_4h_bars=4.0,
        beta_stability=0.10,
        sigma_pair_risk=0.01,
        observed_pair_return_fraction=0.02,
        gross_notional_usd=15.0,
        notional_a_usd=6.0,
        notional_b_usd=9.0,
        d_SL=0.01,
        d_TP=0.015,
        historical_notional_assumption=HISTORICAL_NOTIONAL_ASSUMPTION,
        historical_eligibility=True,
        historical_eligibility_authority=(
            "rob974_h1_parent_manifest_selected_universe"
        ),
        volatility_percentile=None,
        volatility_percentile_provenance="not_defined_for_s4",
        entry_tick_ts=_SIGNAL_TS,
        entry_deadline_ts=_SIGNAL_TS + _MINUTE_MS,
        max_hold_4h_bars=9,
        leg_a_order_id=None,
        leg_b_order_id=None,
        leg_a_fill_id=None,
        leg_b_fill_id=None,
        pair_executor_provenance="not_evaluated_h3_generator",
    )


def _s3_terminal(candidate: S3Candidate) -> SealedS3Terminal:
    intent = adapt_s3_candidate(candidate, fold_id="fold-00")
    trade = S3Trade(
        symbol=candidate.symbol,
        side=candidate.side,
        config_id=candidate.config_id,
        fold_id="fold-00",
        signal_ts=candidate.decision_ts,
        entry_ts=candidate.decision_ts,
        entry_price=1.0,
        exit_ts=candidate.decision_ts + 2 * _MINUTE_MS,
        exit_price=1.001,
        exit_reason="TP",
        mfe_bps=12.0,
        mae_bps=-2.0,
        gross_bps=10.0,
        volatility_percentile=candidate.volatility_percentile,
    )
    result = S3EngineResult((trade,), (), ())
    return SealedS3Terminal(
        result=result,
        input_seal_sha256=seal_s3_engine_input(
            (intent,), corpus_end_ts=_CORPUS_END_TS, horizon_end_ts=None
        ),
        output_seal_sha256=seal_s3_engine_output(result),
    )


def _s4_terminal(candidate: S4Candidate) -> SealedS4Terminal:
    intent = adapt_s4_candidate(candidate, fold_id="fold-00")
    trade = S4PairTrade(
        pair=(candidate.symbol_a, candidate.symbol_b),
        side_a=candidate.side_a,
        side_b=candidate.side_b,
        config_id=candidate.config_id,
        fold_id="fold-00",
        signal_ts=candidate.decision_ts,
        entry_ts=candidate.decision_ts,
        weight_a=candidate.weight_a,
        weight_b=candidate.weight_b,
        beta_a=candidate.beta_a,
        beta_b=candidate.beta_b,
        mu=candidate.mu,
        sigma=candidate.effective_mad_scale,
        z_entry=candidate.observed_z,
        gross_notional=candidate.gross_notional_usd,
        entry_price_a=1.0,
        entry_price_b=1.0,
        exit_ts=candidate.decision_ts + 2 * _MINUTE_MS,
        exit_price_a=0.999,
        exit_price_b=1.001,
        exit_reason="MEAN_EXIT",
        mfe_bps=15.0,
        mae_bps=-3.0,
        gross_bps=10.0,
        order_id_a=None,
        order_id_b=None,
        pair_exec_status="historical_atomic_assumption",
        pair_executor_validated=False,
        demo_eligible=False,
        volatility_percentile=None,
        volatility_percentile_provenance="not_defined_for_s4",
    )
    result = S4EngineResult((trade,), (), ())
    return SealedS4Terminal(
        result=result,
        input_seal_sha256=seal_s4_engine_input(
            (intent,), corpus_end_ts=_CORPUS_END_TS, horizon_end_ts=None
        ),
        output_seal_sha256=seal_s4_engine_output(result),
    )


@pytest.fixture(scope="module")
def production_plan():
    return build_production_h4_plan()


@pytest.fixture(scope="module")
def tercile_authority():
    return build_tercile_authority(
        fold_id="fold-00",
        train_start_ms=0,
        train_end_ms=_SIGNAL_TS,
        snapshots=(
            _snapshot(0, m=9.0, M=0.0),
            _snapshot(60_000, m=9.0, M=0.01),
            _snapshot(120_000, m=9.0, M=0.03),
        ),
    )


def test_attribution_policy_is_identity_visible_and_uses_only_M_t():
    contract = attribution_contract()
    assert contract["schema_version"] == ATTRIBUTION_SCHEMA_VERSION
    assert contract["market_return"]["semantic"] == MARKET_RETURN_SEMANTIC
    assert contract["market_return"]["semantic"] == "M_t_24h_median_log_return"
    assert contract["market_return"]["m_t_allowed"] is False
    assert contract["S3"]["entry_z"] == "absent_not_defined"
    assert contract["S4"]["entry_z"] == "S4Candidate.observed_z"


def test_train_midrank_tercile_ties_and_exact_two_thirds_boundary():
    authority = build_tercile_authority(
        fold_id="fold-00",
        train_start_ms=0,
        train_end_ms=10,
        snapshots=(
            _snapshot(0, m=-9.0, M=0.0),
            _snapshot(1, m=-9.0, M=0.0),
            _snapshot(2, m=-9.0, M=1.0),
            _snapshot(3, m=-9.0, M=2.0),
            _snapshot(4, m=-9.0, M=3.0),
            _snapshot(5, m=-9.0, M=3.0),
        ),
    )
    tied = [assign_market_return_tercile(authority, 3.0) for _ in range(4)]
    assert {row.bin_name for row in tied} == {"top"}
    assert {row.percentile for row in tied} == {5.0 / 6.0}

    exact_boundary = build_tercile_authority(
        fold_id="fold-01",
        train_start_ms=0,
        train_end_ms=10,
        snapshots=(
            _snapshot(0, m=99.0, M=0.0),
            _snapshot(1, m=99.0, M=1.0),
            _snapshot(2, m=99.0, M=3.0),
        ),
    )
    assignment = assign_market_return_tercile(exact_boundary, 2.0)
    assert assignment.percentile == 2.0 / 3.0
    assert assignment.bin_name == "top"


def test_empty_train_authority_is_explicitly_incomplete():
    authority = build_tercile_authority(
        fold_id="fold-00",
        train_start_ms=0,
        train_end_ms=10,
        snapshots=(),
    )
    assert authority.complete is False
    assert authority.reference_count == 0
    assignment = assign_market_return_tercile(authority, 0.1)
    assert assignment.complete is False
    assert assignment.bin_name is None
    assert assignment.incomplete_reason == "tercile_train_reference_empty"


def test_s3_binding_preserves_candidate_s_q_M_and_has_no_entry_z(
    production_plan, tercile_authority
):
    candidate = _s3_candidate()
    path = bind_s3_attribution_path(
        row_spec=production_plan.row_specs[0],
        fold_id="fold-00",
        path_scenario="primary_stress17",
        candidates=(candidate,),
        terminal=_s3_terminal(candidate),
        corpus_end_ts=_CORPUS_END_TS,
        horizon_end_ts=None,
        decision_snapshots=(_snapshot(_SIGNAL_TS, m=-0.4, M=0.02),),
        tercile_authority=tercile_authority,
    )
    row = path.rows[0]
    assert type(row) is S3SelectedOOSAttribution
    assert "entry_z" not in {field.name for field in fields(row)}
    assert row.S == candidate.S
    assert row.Q == candidate.Q
    assert row.market_return == candidate.market_return_24h == 0.02
    assert row.market_return_tercile == "top"
    assert row.realized_holding_minutes == 2.0
    assert row.lineage.row_id == "S3-00"
    assert row.lineage.experiment_id == production_plan.row_specs[0].experiment_id
    assert row.scenario_row.e17_bps != row.scenario_row.e13_bps


def test_s4_binding_captures_snapshot_M_not_m_and_signed_applied_beta(production_plan):
    candidate = _s4_candidate()
    path = bind_s4_attribution_path(
        row_spec=production_plan.row_specs[24],
        fold_id="fold-00",
        path_scenario="primary_stress17",
        candidates=(candidate,),
        terminal=_s4_terminal(candidate),
        corpus_end_ts=_CORPUS_END_TS,
        horizon_end_ts=None,
        decision_snapshots=(_snapshot(_SIGNAL_TS, m=-0.4, M=0.04),),
    )
    row = path.rows[0]
    assert type(row) is S4SelectedOOSAttribution
    assert row.market_return == 0.04
    assert row.market_return != -0.4
    assert row.entry_z == candidate.observed_z
    assert row.D == candidate.D_bps
    assert row.correlation == candidate.rho
    assert row.half_life == candidate.half_life_4h_bars
    assert row.beta_stability == candidate.beta_stability
    assert row.realized_pair_beta == (
        -candidate.weight_a * candidate.beta_a + candidate.weight_b * candidate.beta_b
    )
    assert row.realized_holding_minutes == 2.0


def test_input_seal_cannot_be_replaced_by_a_provenance_string(
    production_plan, tercile_authority
):
    candidate = _s3_candidate()
    terminal = replace(_s3_terminal(candidate), input_seal_sha256="f" * 64)
    with pytest.raises(H4AttributionError, match="input seal"):
        bind_s3_attribution_path(
            row_spec=production_plan.row_specs[0],
            fold_id="fold-00",
            path_scenario="base13",
            candidates=(candidate,),
            terminal=terminal,
            corpus_end_ts=_CORPUS_END_TS,
            horizon_end_ts=None,
            decision_snapshots=(_snapshot(_SIGNAL_TS, m=-0.4, M=0.02),),
            tercile_authority=tercile_authority,
        )


def test_actual_envelope_requires_exact_three_paths_and_production_plan(
    production_plan, tercile_authority
):
    candidate = _s3_candidate()
    terminal = _s3_terminal(candidate)
    paths = tuple(
        bind_s3_attribution_path(
            row_spec=production_plan.row_specs[0],
            fold_id="fold-00",
            path_scenario=scenario,
            candidates=(candidate,),
            terminal=terminal,
            corpus_end_ts=_CORPUS_END_TS,
            horizon_end_ts=None,
            decision_snapshots=(_snapshot(_SIGNAL_TS, m=-0.4, M=0.02),),
            tercile_authority=tercile_authority,
        )
        for scenario in ("base13", "primary_stress17", "upward_stress22")
    )
    envelope = build_actual_attribution_envelope(
        plan=production_plan,
        paths=paths,
        tercile_authorities=(tercile_authority,),
    )
    validate_attribution_envelope(envelope)
    assert envelope.schema_version == ATTRIBUTION_SCHEMA_VERSION
    assert envelope.contract_provenance == "actual"
    assert len(envelope.rows) == 3
    assert envelope.full_campaign_hash == production_plan.full_campaign_hash

    with pytest.raises(H4AttributionError, match="three canonical scenarios"):
        build_actual_attribution_envelope(
            plan=production_plan,
            paths=paths[:2],
            tercile_authorities=(tercile_authority,),
        )


def test_deferred_envelope_forces_empty_rows(production_plan):
    envelope = build_deferred_attribution_envelope(
        plan=production_plan,
        reason="h6b_empirical_materializer_pending",
    )
    validate_attribution_envelope(envelope)
    assert envelope.contract_provenance == "deferred"
    assert envelope.paths == ()
    assert envelope.rows == ()
    assert envelope.deferred_reason == "h6b_empirical_materializer_pending"
