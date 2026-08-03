"""Contract tests for the isolated DFC-2C-4H-v2 registration."""

from __future__ import annotations

import inspect
import json
import math
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from research_contracts import dfc_2c_4h_v2 as contract


def _score_input(
    symbol: str,
    *,
    integrity: contract.IntegrityState = contract.IntegrityState.COMPLETE,
    history_size: int = contract.PIT_LOOKBACK,
) -> contract.SymbolEpochInput:
    return contract.SymbolEpochInput(
        symbol=symbol,
        integrity=integrity,
        current_ofi=1.0,
        current_premium_close=1.0,
        prior_ofi=(0.0,) * history_size,
        prior_premium_close=(0.0,) * history_size,
        prior_abs_composite=(0.0,) * history_size,
    )


def _all_inputs() -> dict[str, contract.SymbolEpochInput]:
    return {symbol: _score_input(symbol) for symbol in contract.SIGNAL_SYMBOLS}


def test_contract_identity_canonical_hash_and_legacy_seal_preservation_are_fixed():
    payload = contract.contract_as_machine_data()

    assert contract.CONTRACT_ID == "DFC-2C-4H-v2"
    assert contract.CANONICAL_HASH == (
        "85673c730555816e3c2c6759a0489ed5543396e5ad588aadef4624198a74b99f"
    )
    assert contract.canonical_contract_hash() == contract.CANONICAL_HASH
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload
    assert payload["predecessor_preservation"] == {
        "old_identity": "dfc-4h",
        "old_row_mutation": "forbidden",
        "old_seal_mutation": "forbidden",
        "old_supersede_or_hide": "forbidden",
    }


def test_features_are_exact_base_volume_ofi_and_complete_4h_premium_close():
    assert contract.ofi_from_base_volumes(10.0, 6.0) == pytest.approx(math.log(1.5))
    assert contract.premium_index_close_from_complete_4h(
        0.00125, is_complete=True
    ) == pytest.approx(0.00125)

    assert list(inspect.signature(contract.ofi_from_base_volumes).parameters) == [
        "total_base_volume",
        "taker_buy_base_volume",
    ]
    with pytest.raises(ValueError, match="strictly positive"):
        contract.ofi_from_base_volumes(10.0, 10.0)
    with pytest.raises(ValueError, match="must be complete"):
        contract.premium_index_close_from_complete_4h(0.00125, is_complete=False)

    features = contract.contract_as_machine_data()["features"]
    assert features["ofi"]["endpoint"]["path"] == "/fapi/v1/klines"
    assert features["premium"]["value"] == "complete_4h_candle_close"
    assert features["deprecated"] == [
        "quote_volume_proxy",
        "five_minute_premium_average",
    ]


def test_current_excluded_pit_ranks_and_fixed_linear_q75_are_exact():
    history = tuple(float(index) for index in range(contract.PIT_LOOKBACK))

    assert contract.pit_rank(100.0, history) == pytest.approx(
        2.0 * (101.0 / 252.0) - 1.0
    )
    assert contract.tail_threshold_q75(history) == pytest.approx(188.25)
    assert contract.tail_threshold_q75((0.0,) * 252) == 0.0

    score = contract.score_symbol(_score_input("XRPUSDT"))
    assert score.ofi_rank == 1.0
    assert score.premium_rank == 1.0
    assert score.composite == 1.0
    assert score.threshold == 0.0
    assert score.is_candidate is True


def test_quantile_sweep_is_structurally_unavailable():
    history = tuple(float(index) for index in range(contract.PIT_LOOKBACK))

    assert list(inspect.signature(contract.tail_threshold_q75).parameters) == [
        "prior_abs_composites"
    ]
    assert list(inspect.signature(contract.score_symbol).parameters) == ["inputs"]
    with pytest.raises(TypeError):
        contract.tail_threshold_q75(history, 0.70)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        contract.score_symbol(_score_input("XRPUSDT"), quantile=0.70)  # type: ignore[call-arg]

    threshold = contract.contract_as_machine_data()["score"]["tail_threshold"]
    assert threshold["quantile"] == {"numerator": 3, "denominator": 4}
    assert threshold["runtime_quantile_parameter"] is False
    assert threshold["quantile_sweep"] == "forbidden"


def test_basket_is_inner_aligned_any_candidate_with_one_pre_fixed_winner():
    decision = contract.evaluate_basket(_all_inputs())

    assert decision.state is contract.BasketEvaluationState.CANDIDATE
    assert decision.candidate_any is True
    assert decision.winner == "XRPUSDT"
    assert len(decision.scores) == 3

    missing_symbol = _all_inputs()
    missing_symbol.pop("SOLUSDT")
    with pytest.raises(ValueError, match="inner-aligned"):
        contract.evaluate_basket(missing_symbol)

    gapped = _all_inputs()
    gapped["DOGEUSDT"] = replace(
        gapped["DOGEUSDT"], integrity=contract.IntegrityState.GAP
    )
    no_evaluation = contract.evaluate_basket(gapped)
    assert no_evaluation.state is contract.BasketEvaluationState.NOT_EVALUABLE_INTEGRITY
    assert no_evaluation.candidate_any is None
    assert no_evaluation.winner is None


def test_reference_not_ready_never_becomes_a_false_candidate():
    inputs = _all_inputs()
    inputs["SOLUSDT"] = _score_input("SOLUSDT", history_size=251)

    decision = contract.evaluate_basket(inputs)
    assert decision.state is contract.BasketEvaluationState.REFERENCE_NOT_READY
    assert decision.candidate_any is None
    assert decision.winner is None


def test_evidence_manifest_requires_complete_gap_free_versioned_raw_provenance():
    evidence = {
        "source_id": "binance_usdm.klines_4h",
        "endpoint_host": "fapi.binance.com",
        "endpoint_path": "/fapi/v1/klines",
        "endpoint_version": "v1",
        "symbol": "XRPUSDT",
        "interval": "4h",
        "epoch_start_utc": "2021-05-02T00:00:00Z",
        "epoch_end_utc": "2021-05-02T04:00:00Z",
        "complete": True,
        "gap_status": "none",
        "raw_payload_sha256": "a" * 64,
        "schema_version": "binance-usdm-kline.v1",
    }

    contract.validate_evidence_manifest(evidence)
    with pytest.raises(ValueError, match="gapped"):
        contract.validate_evidence_manifest({**evidence, "gap_status": "detected"})
    with pytest.raises(ValueError, match="raw_payload_sha256"):
        contract.validate_evidence_manifest({**evidence, "raw_payload_sha256": "BAD"})


def test_exploration_isolation_budget_demo_boundary_and_oi_disposition_are_explicit():
    payload = contract.contract_as_machine_data()

    assert payload["exploration_isolation"]["label"] == "design_only_exploration"
    assert payload["exploration_isolation"]["forbidden_uses"] == [
        "v2_performance_claim",
        "v2_promotion_evidence",
        "v2_incidence_adjudication",
    ]
    budget = payload["promotion_budget"]
    assert budget["legacy_608_effective_outcomes_inherited"] is False
    assert budget["legacy_365_day_cap_inherited"] is False
    assert budget["historical_backtest"]["calendar_days"] == 180
    assert budget["prospective_no_order_shadow"]["calendar_days"] == 28
    warmup_start = datetime.fromisoformat(
        contract.HISTORICAL_WARMUP_START_UTC.replace("Z", "+00:00")
    )
    holdout_start = datetime.fromisoformat(
        contract.HISTORICAL_HOLDOUT_START_UTC.replace("Z", "+00:00")
    )
    holdout_end = datetime.fromisoformat(
        contract.HISTORICAL_HOLDOUT_END_UTC.replace("Z", "+00:00")
    )
    assert holdout_start - warmup_start >= timedelta(days=84)
    assert (holdout_end - holdout_start).days == 180
    assert (holdout_end - holdout_start).total_seconds() / (
        4 * 60 * 60
    ) == contract.HISTORICAL_HOLDOUT_SCHEDULED_EPOCHS
    assert (
        contract.PROSPECTIVE_SHADOW_DAYS * 6
        == contract.PROSPECTIVE_SHADOW_SCHEDULED_EPOCHS
    )
    assert budget["orders"] == 0
    assert budget["binance_demo_contact"] == 0
    assert budget["account_assignments"] == 0
    assert budget["automatic_promotion"] is False
    assert payload["binance_demo_boundary"]["automatic_successor_assignment"] is False
    assert payload["oi_collector_disposition"] == {
        "recommendation": "stop_subject_to_operator_execution",
        "execution_performed": False,
        "rationale": "v2 is fixed to OFI plus premium only; retaining OI for this lane would require a different three-component contract and formal long-history source or prospective accumulation, which this proxy cannot shorten.",
    }


def test_contract_is_offline_and_does_not_import_runtime_or_transport_code():
    source = inspect.getsource(contract)
    for forbidden in (
        "import app",
        "from app",
        "httpx",
        "requests",
        "socket",
        "sqlite",
        "subprocess",
    ):
        assert forbidden not in source
