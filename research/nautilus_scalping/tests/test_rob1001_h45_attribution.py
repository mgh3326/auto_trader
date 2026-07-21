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
    SelectedOOSAttributionEnvelope,
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

_RETIRED_FULL_CAMPAIGN_HASH = (
    "341a5a57ec14b7a499ea58d74de3b7d9c4b2c4e8bb514c789f6f528231a4045d"
)
_RETIRED_CAMPAIGN_RUN_ID = "rob974h6a-ReYDH4lJ8dDmDJxApTNIB7p72qAjTBI2Qited6Ni9Y0"
_ROB1012_FULL_CAMPAIGN_HASH = (
    "c8bb8e88e129e0072d0ea174adca5c4cce8158f2726c6397030d2ae6e4619f39"
)
_ROB1012_CAMPAIGN_RUN_ID = "rob974h6a-G4efMErFLrEyHWNztSKlo9j-ghlxQuPkwD0h1g6sQEw"
_ROB1012_RUNNER_SOURCE_SHA256 = (
    "09235b487e5436d2ca9899afeab89c4c1d2bd71db9d5b15e229c1b8d1be771d6"
)
_ROB1012_EXPERIMENT_IDS = (
    "d25fcec7187d664dc000ff792011e00c6f98142aaa48f4c2b60d8e773ed017ed",
    "4391e0cab744c93effc226392ed76b3290f84421b87118e4c4d13df1a1a956e1",
    "49629a555b6f419ffb8e8c98314a30015affa17ebbfb18c4fc8cff2257c2ca9e",
    "211eabb29c0f47f7f939d1191416320f7138721a485b91ccb56b0ae7cf9b4f67",
    "b90667b683b50b0f8f3dd2af242ab4c8dfff2a33902fab43aa44794b5033e7be",
    "4fcede2526f90569c77a5b636248caef29bd264d27c45b4e1a6f00043c99218c",
    "b83b24f7063c50ef20a56e0bdfa84ff001c73470c195a0cdf236592e68e51852",
    "15d4185e75012b332bb4ce99813ee6590354b5d42420e55d2a51026e0cfb8835",
    "1309bc20d053c0d31c7cb7229f436b412fc55208aaedf49c7abb8586db4be03a",
    "3b63b40ec775190f54d7a289d2c87da99becb72076ec59b93c0deab23b4d1995",
    "e3e00988e987c43a8afdfb58078398bd2931a37b7c2abb15d92ddc3bbce7d08d",
    "977d852283e57d816e00468cc0034bf9ff93e5eedaf851c1b45160c08186b474",
    "63b2525f34d48bf045a9e5f051696b49545b528d4c3a0b064cccb7e447454ef5",
    "1b3e181eed6591422156281e1c51e91391c4ecfb52fe58bebf06c8cb313cc0fe",
    "6ea65811d95b5b9729c081d34ac9de77208d729d35802b1495f8e16e189f66bb",
    "c3460c6d1c5d1fc547dc3f6e44ef97780ab88752e70eaa543b07c67e28691f04",
    "66164eea435508c56cb45e32cc83b3dd74ef65f9a08d931291bc1fbc75b4baa0",
    "f718945d51d7d9c02bd370909ab6b3828e5b0a70e8b597114981404bca0ede3b",
    "28d42ca3d9126f76488f4ec2c7c283d7dd63cffa621502367c266e5d7b93f22c",
    "15e9c7bf140191989dc614ef49e5c9301a1ab1add55c0f471fba3586fdc96750",
    "a4cca850865cebf7f587cbe45ddc56e231ae1b9ab10294a9a7118c6dbba46d5c",
    "07d2816d829f80f47243f724b291279185490c692712c00c6b06227319924d9a",
    "5e4806a04ee10100c5c8fea48afd744af49572a0389304d37ca2926adcfeb68c",
    "6ec46a6ca3b500d00d60ef9de6efc3f90aae5582905fe915af2610b9611ef521",
    "7a53d00145a5dae19cca35f9fd9d0cd3f55b10c419b0ad9cf6e54cdb9abb87b0",
    "27c21af0ba1312d8b9ff95ea8bebf53d5c407992b8a254c87b8fa6e3c8f1a966",
    "96c08362d09a02206d0dc8f58b822f0ed0257c56e95d2e96c5000290772499be",
    "57c5a6be74c513bce1c4f1647404510ffbafc8f77a5d0b4f432f591b930529e7",
    "eec2cb79a66b639c54910e7581465ea3ef79ae543c2632d3f0e632dbc55a580b",
    "3700fd15f0492fcc8caaf89169b8465c6b50a3438fa3a5160434b89f7de7e1ab",
    "ef9104f939e3c6be3e7a2565dc438c6a3f382c304ebb971382efe54d49c52420",
    "3f380a779b2eccf855d7b67ba0430f8255d1e96f4643721490b04da6c453c24d",
    "8f053ca42000dd3ad339b6ab7634c921c3c383d39b2dd4fdbb2613834a650131",
    "ae9bb0b91606ffc6d43e919832fdf91211b7ca502d7c7f27b9cfacc0acd567be",
    "17975759f72812429095dbf64a4f5cfa8c731678c832cdfe5edf3ec8b1cf926c",
    "85151f66d6ea0d45c5a2a508809a3c5cae73ee7a7be9538540a9601d0f04cb8e",
    "6e0dc81a088ebfd28e452ecfa90480e221919836dea96865d5357fec8cd1785b",
    "378ddb98408c5fa8c060aa04b5d1c38cbb852f4be10101e4aba8245d0983c8c2",
    "498594b65b95a24047a5a8be8b9a11ce9f1174db47b943375b86dfe9f5cd2576",
    "f55ccb7a801c754acd56958fd32c87c7d191266aa270b61164e787ba85a5a07d",
    "f5643f2caa56cdf71ee45a13069b5740ea89a31ba7b9fc0d71ea4c73aa4ef49d",
    "d02c9a7c54c77b62fe0ece45976d410c5ca62ba1a00b2eccc3ce6a0a7e5330cd",
    "51e88abaa874a9835203076ef32a0d56cf3700b725a4968c8a2b967d81c3368e",
    "4ceb18cf7941a023b43e3f4c0b5ce2a7f6357b974c47a22e6d9b6d0ec06faf0c",
    "902f007e8a0f8e55565e74e362dc6ca9f146dac25633533fd37cdbe8d73dbf67",
    "bedcc8c49fc2323588c501250d9bcfd43eb085ee56176de7f62f5037e33437dc",
    "b1a31efc59444599ec24bddc5d83702f1b2adabec3cc32563be5febe32eb4650",
    "b11a976a0e690f14c4438d2132134f4145cecda4304e588aa1abc9068cedcbc8",
)


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


def test_rob1012_generator_change_rederives_and_refreezes_campaign_identity(
    production_plan,
):
    assert production_plan.full_campaign_hash == _ROB1012_FULL_CAMPAIGN_HASH
    assert production_plan.campaign_run_id == _ROB1012_CAMPAIGN_RUN_ID
    assert production_plan.h4_source_pins.runner_bundle_sha256 == (
        _ROB1012_RUNNER_SOURCE_SHA256
    )
    assert tuple(row.experiment_id for row in production_plan.row_specs) == (
        _ROB1012_EXPERIMENT_IDS
    )
    assert production_plan.full_campaign_hash != _RETIRED_FULL_CAMPAIGN_HASH
    assert production_plan.campaign_run_id != _RETIRED_CAMPAIGN_RUN_ID


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


def test_deferred_envelope_rejects_nonempty_paths_and_tercile_authorities(
    production_plan, tercile_authority
):
    candidate = _s3_candidate()
    path = bind_s3_attribution_path(
        row_spec=production_plan.row_specs[0],
        fold_id="fold-00",
        path_scenario="base13",
        candidates=(candidate,),
        terminal=_s3_terminal(candidate),
        corpus_end_ts=_CORPUS_END_TS,
        horizon_end_ts=None,
        decision_snapshots=(_snapshot(_SIGNAL_TS, m=-0.4, M=0.02),),
        tercile_authority=tercile_authority,
    )

    with pytest.raises(
        H4AttributionError,
        match=r"^deferred attribution forces rows=\(\)$",
    ):
        SelectedOOSAttributionEnvelope(
            schema_version=ATTRIBUTION_SCHEMA_VERSION,
            contract_provenance="deferred",
            full_campaign_hash=production_plan.full_campaign_hash,
            campaign_run_id=production_plan.campaign_run_id,
            source_pins=production_plan.source_pins,
            h4_source_pins=production_plan.h4_source_pins,
            paths=(path,),
            tercile_authorities=(tercile_authority,),
            deferred_reason="h6b_empirical_materializer_pending",
            producer_seal_sha256="0" * 64,
        )
