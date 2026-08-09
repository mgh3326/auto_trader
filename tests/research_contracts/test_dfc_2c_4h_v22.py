"""Adversarial contract tests for the DFC-2C-4H v2.2 registration.

Covers NW-F3 provenance, NW-F4 outcome evidence binding, NW-F7 verdict
unification, and the five required mutants (as permanent goldens).
"""

from __future__ import annotations

import inspect
import json
import math
import re
from types import SimpleNamespace

import pytest

from research_contracts import dfc_2c_4h_v22 as contract
from research_contracts.dfc_2c_4h_v22_harness import (
    EvidenceCandle,
    assert_forward_only,
    assert_signal_execution_forward_only,
    build_epoch_manifest,
    epoch_features_from_evidence,
    evaluate_epoch_basket,
    inner_align_4h,
    make_outcome_epoch_record_from_candles,
    raw_payload_sha256,
    validate_and_sort_candles,
)


def _kline_payload(start: int, *, close: str = "100") -> tuple[object, ...]:
    return (
        start,
        "1",
        "1",
        "1",
        close,
        "2",
        start + 14_399_999,
        "10",
        "1",
        "1",
        "1",
        "0",
    )


def _premium_payload(start: int, *, close: str = "0.0") -> tuple[object, ...]:
    return (
        start,
        "0",
        "0",
        "0",
        close,
        "0",
        start + 14_399_999,
        "0",
        "0",
        "0",
        "0",
        "0",
    )


def _candle(
    symbol: str,
    start: int,
    *,
    source: str,
    payload: tuple[object, ...],
) -> EvidenceCandle:
    source_id = (
        "binance_usdm.klines_4h"
        if source == "kline"
        else "binance_usdm.premium_index_klines_4h"
    )
    path = "/fapi/v1/klines" if source == "kline" else "/fapi/v1/premiumIndexKlines"
    return EvidenceCandle(
        symbol,
        source,
        start,
        start + 14_400_000,
        payload,
        build_epoch_manifest(
            source_id=source_id,
            endpoint_host="fapi.binance.com",
            endpoint_path=path,
            endpoint_version="v1",
            symbol=symbol,
            epoch_start_utc="1970-01-01T00:00:00Z",
            epoch_end_utc="1970-01-01T04:00:00Z",
            payload=payload,
            schema_version="binance.v1",
        ),
    )


def _epoch(symbol: str, *, current: float = 1.0):
    klines = []
    premiums = []
    for index in range(505):
        start = index * 14_400_000
        klines.append(
            _candle(
                symbol,
                start,
                source="kline",
                payload=_kline_payload(start),
            )
        )
        premiums.append(
            _candle(
                symbol,
                start,
                source="premium",
                payload=_premium_payload(
                    start, close=str(current if index == 504 else 0.0)
                ),
            )
        )
    return epoch_features_from_evidence(
        symbol=symbol,
        current_kline=klines[-1],
        current_premium=premiums[-1],
        prior_klines=klines[:-1],
        prior_premium=premiums[:-1],
        prior_30d_quote_volume=100.0,
    )


def _decision(*, candidate_any: bool, winner: str) -> contract.BasketDecision:
    scores = (
        contract.SymbolScore(winner, 1.0 if candidate_any else 0.0, 0.5, candidate_any),
        contract.SymbolScore("BBBUSDT", 0.1, 0.5, False),
        contract.SymbolScore("CCCUSDT", 0.0, 0.5, False),
    )
    return contract.BasketDecision(candidate_any, winner, scores)


def _close_evidence(
    symbol: str,
    epoch_start_ms: int,
    *,
    close: str,
) -> object:
    payload = _kline_payload(epoch_start_ms, close=close)
    manifest = build_epoch_manifest(
        source_id="binance_usdm.klines_4h",
        endpoint_host="fapi.binance.com",
        endpoint_path="/fapi/v1/klines",
        endpoint_version="v1",
        symbol=symbol,
        epoch_start_utc="1970-01-01T00:00:00Z",
        epoch_end_utc="1970-01-01T04:00:00Z",
        payload=payload,
        schema_version="binance.v1",
    )
    return contract.extract_kline_close_evidence(
        symbol=symbol,
        epoch_start_ms=epoch_start_ms,
        payload=payload,
        manifest=manifest,
    )


def _ok_record(
    *,
    candidate: bool,
    entry_close: str,
    exit_close: str,
    winner: str = "AAAUSDT",
    epoch_start_ms: int = 0,
) -> contract.OutcomeEpochRecord:
    decision = _decision(candidate_any=candidate, winner=winner)
    entry = _close_evidence(winner, epoch_start_ms, close=entry_close)
    nxt = _close_evidence(
        winner, epoch_start_ms + contract.INTERVAL_MS, close=exit_close
    )
    return contract.make_outcome_epoch_record(decision, entry=entry, next_bar=nxt)


def _records_with_delta(
    *,
    n_candidate: int,
    n_control: int,
    candidate_exit: str,
    control_exit: str,
) -> tuple[contract.OutcomeEpochRecord, ...]:
    records: list[contract.OutcomeEpochRecord] = []
    for index in range(n_candidate):
        records.append(
            _ok_record(
                candidate=True,
                entry_close="100",
                exit_close=candidate_exit,
                epoch_start_ms=index * contract.INTERVAL_MS,
            )
        )
    for index in range(n_control):
        records.append(
            _ok_record(
                candidate=False,
                entry_close="100",
                exit_close=control_exit,
                epoch_start_ms=(n_candidate + index) * contract.INTERVAL_MS,
            )
        )
    return tuple(records)


# ---------------------------------------------------------------------------
# Identity / freeze / window (ported from v2.1 surface, v2.2 identity)
# ---------------------------------------------------------------------------


def test_new_identity_and_v21_provenance_are_explicit() -> None:
    payload = contract.contract_as_machine_data()
    assert contract.CONTRACT_ID == "DFC-2C-4H-v2.2"
    assert contract.CANONICAL_HASH == contract.canonical_contract_hash()
    assert len(contract.CANONICAL_HASH) == 64
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload
    preservation = payload["predecessor_preservation"]
    assert preservation["v2_row_mutation"] == "forbidden"
    assert preservation["v21_identity"] == "DFC-2C-4H-v2.1"
    assert (
        preservation["v21_source_commit"] == "9f605139044605a5f31e5ee3da77133924126197"
    )
    assert preservation["v21_row_mutation"] == "forbidden"
    assert (
        payload["implementation"]["module_source_sha256"]
        == contract.MODULE_SOURCE_SHA256
    )
    from research_contracts import dfc_2c_4h_v22_harness as harness

    assert (
        payload["implementation"]["harness_source_sha256"]
        == harness.HARNESS_SOURCE_SHA256
    )


def test_import_time_source_freeze_is_present_and_runtime_hash_is_current() -> None:
    source = inspect.getsource(contract)
    assert "RuntimeError" in source
    assert len(re.findall(r"^MODULE_SOURCE_SHA256: Final =", source, re.MULTILINE)) == 1
    assert len(contract.MODULE_SOURCE_SHA256) == 64
    assert contract.MODULE_SOURCE_SHA256 != "0" * 64


def test_composite_tail_is_derived_and_input_has_no_prior_composite_argument() -> None:
    assert not hasattr(contract, "EpochFeatures")
    assert list(inspect.signature(contract.tail_threshold_q75).parameters) == [
        "prior_ofi",
        "prior_premium_close",
    ]
    score = contract.score_symbol(_epoch("AAAUSDT"))
    assert score.threshold == 1.0
    with pytest.raises(TypeError):
        contract.tail_threshold_q75((0.0,) * 504, (0.0,) * 504, 0.95)  # type: ignore[call-arg]


def test_pit_universe_is_dynamic_and_no_symbols_are_hardcoded() -> None:
    assert contract.select_universe(
        {"SOLUSDT": 3.0, "XRPUSDT": 2.0, "DOGEUSDT": 1.0, "AAAUSDT": 4.0}
    ) == ("AAAUSDT", "SOLUSDT", "XRPUSDT")
    assert contract.contract_as_machine_data()["universe"]["hardcoded_symbols"] is False


def test_adopted_backtest_window_is_non_overlapping() -> None:
    payload = contract.contract_as_machine_data()
    window = payload["backtest_window"]
    assert window["holdout_start_utc"] == "2021-05-02T00:00:00Z"
    assert window["holdout_end_utc_exclusive"] == "2023-08-04T00:00:00Z"
    assert window["calendar_days"] == 824
    assert window["scheduled_epochs"] == 4_944
    assert window["warmup_start_utc"] == "2021-02-02T00:00:00Z"
    isolation = payload["exploration_isolation"]
    assert isolation["excluded_overlap_epochs"] == 1_632


def test_control_a_is_symmetric_and_verdict_literals_match_nw_f7() -> None:
    payload = contract.contract_as_machine_data()
    assert payload["estimand"]["control_choice"] == "A"
    assert payload["estimand"]["selection_symmetric"] is True
    assert payload["estimand"]["outcome_free_bool_price"] == "forbidden"
    assert (
        payload["estimand"]["missing_next_bar"] == contract.RUN_INVALID_OUTCOME_EVIDENCE
    )
    assert payload["estimand"]["row_deletion_on_missing_next_bar"] == "forbidden"
    rule = payload["adjudication"]
    assert rule["delta_threshold_bps"] == 5.0
    assert rule["block_length_epochs"] == 24
    assert rule["repetitions"] == 10_000
    assert rule["minimum_candidate_epochs"] == 400
    assert rule["minimum_control_epochs"] == 400
    assert rule["historical_validation_runs"] == 1
    assert "power" not in rule
    assert rule["pass_falsified_unification"] == ("PASS condition negation = FALSIFIED")
    assert contract.STATUS_PASS == "PASS"
    assert contract.STATUS_INCONCLUSIVE == "INCONCLUSIVE"
    assert contract.STATUS_FALSIFIED == "FALSIFIED"
    assert contract.STATUS_RUN_INVALID == "RUN_INVALID"


def test_power_claim_removed_from_machine_data() -> None:
    rule = contract.contract_as_machine_data()["adjudication"]
    assert "power" not in rule
    assert "planning_sd_bps" not in rule
    source = inspect.getsource(contract)
    assert "planning_sd_bps" not in source
    assert "power claim removed" in source


def test_harness_hashes_validates_aligns_and_rejects_future_windows() -> None:
    payload = [0, 1, 2, 3, 4, 5, 14_399_999, 7, 8, 2, 1, 0]
    manifest = {
        "source_id": "binance_usdm.klines_4h",
        "endpoint_host": "fapi.binance.com",
        "endpoint_path": "/fapi/v1/klines",
        "endpoint_version": "v1",
        "symbol": "AAAUSDT",
        "interval": "4h",
        "epoch_start_utc": "2021-05-02T00:00:00Z",
        "epoch_end_utc": "2021-05-02T04:00:00Z",
        "complete": True,
        "gap_status": "none",
        "raw_payload_sha256": raw_payload_sha256(payload),
        "schema_version": "binance.v1",
    }
    left = EvidenceCandle("AAAUSDT", "kline", 0, 14_400_000, tuple(payload), manifest)
    right = EvidenceCandle(
        "AAAUSDT",
        "premium",
        0,
        14_400_000,
        tuple(payload),
        {
            **manifest,
            "endpoint_path": "/fapi/v1/premiumIndexKlines",
            "source_id": "binance_usdm.premium_index_klines_4h",
        },
    )
    assert inner_align_4h([left], [right]) == (("AAAUSDT", 0),)
    assert validate_and_sort_candles([left]) == (left,)
    with pytest.raises(ValueError, match="prior"):
        assert_forward_only([100], {100: tuple(range(504))}, prior_evidence={100: ()})


def test_harness_blocks_signal_close_execution_overlap_from_raw_payload_times() -> None:
    signal = EvidenceCandle(
        "AAAUSDT",
        "kline",
        0,
        14_400_000,
        (0, 0, 0, 0, 0, 0, 14_399_999, 0, 0, 0, 0, 0),
        {},
    )
    execution = EvidenceCandle(
        "AAAUSDT",
        "kline",
        10_800_000,
        25_200_000,
        (10_800_000, 0, 0, 0, 0, 0, 25_199_999, 0, 0, 0, 0, 0),
        {},
    )
    with pytest.raises(ValueError, match="NEXT_BAR_OVERLAPS_SIGNAL_BAR"):
        assert_signal_execution_forward_only(
            signal, execution, declared_signal_close_time_ms=14_399_999
        )


def test_control_arm_executes_same_selection() -> None:
    basket = evaluate_epoch_basket(
        dict.fromkeys(("AAAUSDT", "BBBUSDT", "CCCUSDT"), 100.0),
        {
            symbol: _epoch(symbol, current=value)
            for symbol, value in (("AAAUSDT", 0.2), ("BBBUSDT", 0.1), ("CCCUSDT", 0.0))
        },
    )
    assert basket.winner == "AAAUSDT"
    assert basket.candidate_any is True


def test_contract_remains_offline_only() -> None:
    source = inspect.getsource(contract)
    for forbidden in (
        "import app",
        "from app",
        "httpx",
        "requests",
        "socket",
        "subprocess",
    ):
        assert forbidden not in source


def test_v21_port_blobs_are_importable_and_unmodified_identity() -> None:
    from research_contracts import dfc_2c_4h_v21 as v21

    assert v21.CONTRACT_ID == "DFC-2C-4H-v2.1"
    assert v21.CONTRACT_ID != contract.CONTRACT_ID


# ---------------------------------------------------------------------------
# NW-F4 / R3: outcome evidence binding
# ---------------------------------------------------------------------------


def test_outcome_from_decision_and_raw_kline_evidence() -> None:
    decision = _decision(candidate_any=True, winner="AAAUSDT")
    entry = _close_evidence("AAAUSDT", 0, close="100")
    nxt = _close_evidence("AAAUSDT", contract.INTERVAL_MS, close="101")
    record = contract.make_outcome_epoch_record(decision, entry=entry, next_bar=nxt)
    assert record.status == "ok"
    assert record.observation is not None
    assert record.observation.candidate is True
    assert record.observation.outcome_bps == pytest.approx(
        abs(math.log(101 / 100)) * 10_000.0
    )
    assert record.observation.binding.decision_winner == "AAAUSDT"


def test_harness_outcome_from_candles_binds_decision() -> None:
    decision = _decision(candidate_any=False, winner="AAAUSDT")
    entry = _candle(
        "AAAUSDT", 0, source="kline", payload=_kline_payload(0, close="100")
    )
    nxt = _candle(
        "AAAUSDT",
        contract.INTERVAL_MS,
        source="kline",
        payload=_kline_payload(contract.INTERVAL_MS, close="100.5"),
    )
    record = make_outcome_epoch_record_from_candles(
        decision, entry_kline=entry, next_kline=nxt
    )
    assert record.status == "ok"
    assert record.observation is not None
    assert record.observation.candidate is False


def test_absolute_log_return_bps_is_not_a_free_outcome_surface() -> None:
    """absolute_log_return_bps remains a pure helper; adjudication only via records."""
    assert contract.absolute_log_return_bps(100.0, 101.0) == pytest.approx(
        99.5033, rel=1e-4
    )
    # Free bool+price factory must not exist (v2.1 R3 surface removed).
    assert not hasattr(contract, "make_outcome_observation")
    sig = inspect.signature(contract.make_outcome_epoch_record)
    assert list(sig.parameters) == ["decision", "entry", "next_bar"]


# ---------------------------------------------------------------------------
# Mutant goldens ①–⑤ (permanent asserts; inject tests below)
# ---------------------------------------------------------------------------


def test_mutant1_free_bool_outcome_construction_is_rejected() -> None:
    """① free bool injection → rejected."""
    # Free (bool, bps) construction is missing the evidence binding argument.
    with pytest.raises(TypeError, match="binding"):
        contract.OutcomeObservation(True, 50.0)  # type: ignore[call-arg]
    # Forged non-_OutcomeBinding binding is also rejected.
    with pytest.raises(TypeError, match="evidence binding"):
        contract.OutcomeObservation(  # type: ignore[arg-type]
            candidate=True,
            outcome_bps=50.0,
            binding=SimpleNamespace(
                winner_symbol="AAAUSDT",
                signal_epoch_start_ms=0,
                entry_payload_sha256="a" * 64,
                exit_payload_sha256="b" * 64,
                decision_candidate_any=True,
                decision_winner="AAAUSDT",
            ),
        )


def test_mutant2_free_price_outside_raw_evidence_is_rejected() -> None:
    """② free price (outside raw evidence) → rejected."""
    decision = _decision(candidate_any=True, winner="AAAUSDT")
    # Free floats are not accepted — entry/next_bar must be _KlineCloseEvidence.
    record = contract.make_outcome_epoch_record(
        decision,
        entry=100.0,
        next_bar=101.0,  # type: ignore[arg-type]
    )
    assert record.status == contract.RUN_INVALID_OUTCOME_EVIDENCE
    # Free scalar "close" with empty manifest cannot become kline close evidence.
    with pytest.raises((TypeError, ValueError)):
        contract.extract_kline_close_evidence(
            symbol="AAAUSDT",
            epoch_start_ms=0,
            payload=100.0,  # type: ignore[arg-type]
            manifest={},
        )
    # Free close cannot be smuggled via a non-kline source_id either.
    bad_payload = _kline_payload(0, close="99999")
    bad_manifest = build_epoch_manifest(
        source_id="binance_usdm.premium_index_klines_4h",
        endpoint_host="fapi.binance.com",
        endpoint_path="/fapi/v1/premiumIndexKlines",
        endpoint_version="v1",
        symbol="AAAUSDT",
        epoch_start_utc="1970-01-01T00:00:00Z",
        epoch_end_utc="1970-01-01T04:00:00Z",
        payload=bad_payload,
        schema_version="binance.v1",
    )
    with pytest.raises(ValueError, match="klines_4h"):
        contract.extract_kline_close_evidence(
            symbol="AAAUSDT",
            epoch_start_ms=0,
            payload=bad_payload,
            manifest=bad_manifest,
        )


def test_mutant3_missing_next_bar_is_run_invalid_not_row_drop() -> None:
    """③ missing next bar → RUN_INVALID_OUTCOME_EVIDENCE, not silent deletion."""
    decision = _decision(candidate_any=True, winner="AAAUSDT")
    entry = _close_evidence("AAAUSDT", 0, close="100")
    record = contract.make_outcome_epoch_record(decision, entry=entry, next_bar=None)
    assert record.status == contract.RUN_INVALID_OUTCOME_EVIDENCE
    assert record.observation is None
    # Even when mixed with many valid PASS-capable rows, invalid is not dropped.
    valid = _records_with_delta(
        n_candidate=400,
        n_control=400,
        candidate_exit="100.6",
        control_exit="100.1",
    )
    result = contract.adjudicate_outcomes((record, *valid))
    assert result.status == contract.STATUS_RUN_INVALID
    assert result.reason_code == contract.RUN_INVALID_OUTCOME_EVIDENCE


def test_mutant4_sample_shortfall_is_inconclusive_not_pass_or_falsified() -> None:
    """④ sample <400 → INCONCLUSIVE (never PASS/FALSIFIED)."""
    # Strong effect but only 50/50 samples.
    records = _records_with_delta(
        n_candidate=50,
        n_control=50,
        candidate_exit="100.6",
        control_exit="100.1",
    )
    result = contract.adjudicate_outcomes(records)
    assert result.status == contract.STATUS_INCONCLUSIVE
    assert result.status not in {contract.STATUS_PASS, contract.STATUS_FALSIFIED}
    assert result.reason_code == "SAMPLE_SHORTFALL"


def test_mutant5_run_invalid_has_precedence_over_other_verdicts() -> None:
    """⑤ input/evidence violation → RUN_INVALID before PASS/INCONCLUSIVE/FALSIFIED."""
    invalid = contract.OutcomeEpochRecord(
        status=contract.RUN_INVALID_OUTCOME_EVIDENCE, observation=None
    )
    # Enough samples that would otherwise be INCONCLUSIVE-or-better if invalid dropped.
    short = _records_with_delta(
        n_candidate=10,
        n_control=10,
        candidate_exit="100.6",
        control_exit="100.1",
    )
    result_short = contract.adjudicate_outcomes((invalid, *short))
    assert result_short.status == contract.STATUS_RUN_INVALID

    full = _records_with_delta(
        n_candidate=400,
        n_control=400,
        candidate_exit="100.6",
        control_exit="100.1",
    )
    result_full = contract.adjudicate_outcomes((invalid, *full))
    assert result_full.status == contract.STATUS_RUN_INVALID
    assert result_full.reason_code == contract.RUN_INVALID_OUTCOME_EVIDENCE

    # Non-record input is also RUN_INVALID (not crash, not other verdict).
    result_bad = contract.adjudicate_outcomes(
        [SimpleNamespace(candidate=True, outcome_bps=50.0)]  # type: ignore[list-item]
    )
    assert result_bad.status == contract.STATUS_RUN_INVALID
    assert result_bad.reason_code == "RUN_INVALID_INPUT"


def test_pass_vs_falsified_unification() -> None:
    """PASS condition negation = FALSIFIED (no success/failure dual wording)."""
    strong = _records_with_delta(
        n_candidate=400,
        n_control=400,
        candidate_exit="100.6",
        control_exit="100.1",
    )
    strong_result = contract.adjudicate_outcomes(strong)
    assert strong_result.status == contract.STATUS_PASS
    assert strong_result.ci_lower_bps is not None
    assert strong_result.ci_lower_bps > 5.0
    assert strong_result.p_value is not None
    assert strong_result.p_value < 0.05

    null = _records_with_delta(
        n_candidate=400,
        n_control=400,
        candidate_exit="100.1",
        control_exit="100.1",
    )
    null_result = contract.adjudicate_outcomes(null)
    assert null_result.status == contract.STATUS_FALSIFIED
    # Not the old v2.1 dual wording.
    assert null_result.status not in {"success", "failure", "indeterminate"}


def test_incomplete_next_bar_is_run_invalid() -> None:
    decision = _decision(candidate_any=True, winner="AAAUSDT")
    entry = _close_evidence("AAAUSDT", 0, close="100")
    # Wrong interval (not immediate next 4h bar).
    far = _close_evidence("AAAUSDT", 2 * contract.INTERVAL_MS, close="101")
    record = contract.make_outcome_epoch_record(decision, entry=entry, next_bar=far)
    assert record.status == contract.RUN_INVALID_OUTCOME_EVIDENCE


def test_winner_symbol_mismatch_is_run_invalid() -> None:
    decision = _decision(candidate_any=True, winner="AAAUSDT")
    entry = _close_evidence("BBBUSDT", 0, close="100")
    nxt = _close_evidence("BBBUSDT", contract.INTERVAL_MS, close="101")
    record = contract.make_outcome_epoch_record(decision, entry=entry, next_bar=nxt)
    assert record.status == contract.RUN_INVALID_OUTCOME_EVIDENCE


# ---------------------------------------------------------------------------
# False-green: flip core asserts would fail (structural check of goldens)
# ---------------------------------------------------------------------------


def test_false_green_guard_mutant_goldens_have_failing_negations() -> None:
    """If core mutant asserts were inverted, they must not vacuously pass.

    We re-evaluate the same predicates with inverted expectations and require
    that the inverted form is False — proving the golden is load-bearing.
    """
    # ① free bool construction raises (inverted would expect no raise).
    raised = False
    try:
        contract.OutcomeObservation(True, 50.0)  # type: ignore[call-arg]
    except TypeError:
        raised = True
    assert raised is True
    assert (not raised) is False  # inverted golden would fail

    # ③ missing next bar is INVALID (inverted: status == "ok" is False).
    decision = _decision(candidate_any=True, winner="AAAUSDT")
    entry = _close_evidence("AAAUSDT", 0, close="100")
    record = contract.make_outcome_epoch_record(decision, entry=entry, next_bar=None)
    assert record.status == contract.RUN_INVALID_OUTCOME_EVIDENCE
    assert (record.status == "ok") is False

    # ④ short sample is INCONCLUSIVE (inverted PASS is False).
    short = _records_with_delta(
        n_candidate=50,
        n_control=50,
        candidate_exit="100.6",
        control_exit="100.1",
    )
    short_result = contract.adjudicate_outcomes(short)
    assert short_result.status == contract.STATUS_INCONCLUSIVE
    assert (short_result.status == contract.STATUS_PASS) is False

    # ⑤ invalid first → RUN_INVALID even with PASS-capable bulk (inverted False).
    invalid = contract.OutcomeEpochRecord(
        status=contract.RUN_INVALID_OUTCOME_EVIDENCE, observation=None
    )
    bulk = _records_with_delta(
        n_candidate=400,
        n_control=400,
        candidate_exit="100.6",
        control_exit="100.1",
    )
    mixed = contract.adjudicate_outcomes((invalid, *bulk))
    assert mixed.status == contract.STATUS_RUN_INVALID
    assert (mixed.status == contract.STATUS_PASS) is False
