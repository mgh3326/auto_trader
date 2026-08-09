"""Adversarial contract tests for the v2.1 re-registration."""

from __future__ import annotations

import inspect
import json
import re

import pytest

from research_contracts import dfc_2c_4h_v21 as contract
from research_contracts.dfc_2c_4h_v21_harness import (
    EvidenceCandle,
    assert_forward_only,
    assert_signal_execution_forward_only,
    build_epoch_manifest,
    epoch_features_from_evidence,
    evaluate_epoch_basket,
    inner_align_4h,
    raw_payload_sha256,
    validate_and_sort_candles,
)


def _epoch(symbol: str, *, current: float = 1.0):
    klines = []
    premiums = []
    for index in range(505):
        start = index * 14_400_000
        kline = (
            start,
            "1",
            "1",
            "1",
            "1",
            "2",
            start + 14_399_999,
            "10",
            "1",
            "1",
            "1",
            "0",
        )
        premium = (
            start,
            "0",
            "0",
            "0",
            str(current if index == 504 else 0.0),
            "0",
            start + 14_399_999,
            "0",
            "0",
            "0",
            "0",
            "0",
        )
        klines.append(
            EvidenceCandle(
                symbol,
                "kline",
                start,
                start + 14_400_000,
                kline,
                build_epoch_manifest(
                    source_id="binance_usdm.klines_4h",
                    endpoint_host="fapi.binance.com",
                    endpoint_path="/fapi/v1/klines",
                    endpoint_version="v1",
                    symbol=symbol,
                    epoch_start_utc="1970-01-01T00:00:00Z",
                    epoch_end_utc="1970-01-01T04:00:00Z",
                    payload=kline,
                    schema_version="binance.v1",
                ),
            )
        )
        premiums.append(
            EvidenceCandle(
                symbol,
                "premium",
                start,
                start + 14_400_000,
                premium,
                build_epoch_manifest(
                    source_id="binance_usdm.premium_index_klines_4h",
                    endpoint_host="fapi.binance.com",
                    endpoint_path="/fapi/v1/premiumIndexKlines",
                    endpoint_version="v1",
                    symbol=symbol,
                    epoch_start_utc="1970-01-01T00:00:00Z",
                    epoch_end_utc="1970-01-01T04:00:00Z",
                    payload=premium,
                    schema_version="binance.v1",
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


def test_new_identity_and_v2_preservation_are_explicit() -> None:
    payload = contract.contract_as_machine_data()
    assert contract.CONTRACT_ID == "DFC-2C-4H-v2.1"
    assert contract.CANONICAL_HASH == contract.canonical_contract_hash()
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload
    assert payload["predecessor_preservation"]["v2_row_mutation"] == "forbidden"
    assert (
        payload["implementation"]["module_source_sha256"]
        == contract.MODULE_SOURCE_SHA256
    )
    from research_contracts import dfc_2c_4h_v21_harness as harness

    assert (
        payload["implementation"]["harness_source_sha256"]
        == harness.HARNESS_SOURCE_SHA256
    )


def test_import_time_source_freeze_is_present_and_runtime_hash_is_current() -> None:
    source = inspect.getsource(contract)
    assert "RuntimeError" in source
    assert len(re.findall(r"^MODULE_SOURCE_SHA256: Final =", source, re.MULTILINE)) == 1
    assert len(contract.MODULE_SOURCE_SHA256) == 64


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


def test_lookback_rebinding_cannot_change_runtime_windows() -> None:
    original = contract.PIT_LOOKBACK
    contract.PIT_LOOKBACK = 300  # type: ignore[misc]
    try:
        assert contract.pit_rank(1.0, (0.0,) * 252) == pytest.approx(1.0)
        with pytest.raises(TypeError, match="missing 1 required positional argument"):
            contract.score_symbol(
                _epoch("AAAUSDT").__class__(
                    symbol="AAAUSDT",
                    integrity=contract.IntegrityState.COMPLETE,
                    current_ofi=1.0,
                    current_premium_close=1.0,
                    prior_ofi=(0.0,) * 300,
                    prior_premium_close=(0.0,) * 300,
                    prior_quote_volume_30d=100.0,
                )
            )
    finally:
        contract.PIT_LOOKBACK = original  # type: ignore[misc]


def test_pit_universe_is_dynamic_and_no_symbols_are_hardcoded() -> None:
    assert contract.select_universe(
        {"SOLUSDT": 3.0, "XRPUSDT": 2.0, "DOGEUSDT": 1.0, "AAAUSDT": 4.0}
    ) == ("AAAUSDT", "SOLUSDT", "XRPUSDT")
    assert contract.contract_as_machine_data()["universe"]["hardcoded_symbols"] is False


def test_control_a_is_symmetric_and_adjudication_is_fully_numeric() -> None:
    payload = contract.contract_as_machine_data()
    assert payload["estimand"]["control_choice"] == "A"
    assert payload["estimand"]["selection_symmetric"] is True
    assert (
        payload["estimand"]["selection_implementation"]
        == "evaluate_basket argmax over all three scores"
    )
    assert (
        payload["estimand"]["bootstrap_implementation"] == "stationary_block_bootstrap"
    )
    rule = payload["adjudication"]
    assert rule["delta_threshold_bps"] == 5.0
    assert rule["bootstrap"] == "stationary_block_percentile"
    assert rule["block_length_epochs"] == 24
    assert rule["repetitions"] == 10_000
    assert rule["confidence_level"] == 0.95
    assert rule["alpha"] == 0.05
    assert rule["minimum_candidate_epochs"] == 400
    assert rule["minimum_control_epochs"] == 400
    assert rule["power"]["target"] == 0.80


def test_adopted_backtest_window_is_non_overlapping_and_warmup_is_recomputed() -> None:
    payload = contract.contract_as_machine_data()
    window = payload["backtest_window"]
    assert window["holdout_start_utc"] == "2021-05-02T00:00:00Z"
    assert window["holdout_end_utc_exclusive"] == "2023-08-04T00:00:00Z"
    assert window["calendar_days"] == 824
    assert window["scheduled_epochs"] == 4_944
    assert window["warmup_start_utc"] == "2021-02-02T00:00:00Z"
    assert window["selection_basis"] == (
        "exclude the 1,632-epoch exploration overlap to remove the design-validation feedback loop"
    )
    isolation = payload["exploration_isolation"]
    assert (
        isolation["relationship"] == "no overlap remains in the adopted backtest window"
    )
    assert isolation["excluded_overlap_epochs"] == 1_632


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

    with pytest.raises(ValueError, match="SIGNAL_BAR_CLOSE_MISMATCH"):
        assert_signal_execution_forward_only(
            signal, execution, declared_signal_close_time_ms=14_400_000
        )


def test_control_arm_executes_same_selection_and_adjudication() -> None:
    basket = evaluate_epoch_basket(
        dict.fromkeys(("AAAUSDT", "BBBUSDT", "CCCUSDT"), 100.0),
        {
            symbol: _epoch(symbol, current=value)
            for symbol, value in (("AAAUSDT", 0.2), ("BBBUSDT", 0.1), ("CCCUSDT", 0.0))
        },
    )
    assert basket.winner == "AAAUSDT"
    observations = tuple(
        contract.OutcomeObservation(index % 2 == 0, 10.0 if index % 2 == 0 else 1.0)
        for index in range(800)
    )
    result = contract.adjudicate_outcomes(observations)
    assert result.status == "success"
    assert result.delta_bps == pytest.approx(9.0)
    assert contract.absolute_log_return_bps(100.0, 101.0) == pytest.approx(
        99.5033, rel=1e-4
    )
    assert contract.make_outcome_observation(True, 100.0, 101.0).candidate is True


def test_harness_rejects_short_raw_payload() -> None:
    payload = (0, 1)
    manifest = {
        "source_id": "binance_usdm.klines_4h",
        "endpoint_host": "fapi.binance.com",
        "endpoint_path": "/fapi/v1/klines",
        "endpoint_version": "v1",
        "symbol": "AAAUSDT",
        "interval": "4h",
        "epoch_start_utc": "1970-01-01T00:00:00Z",
        "epoch_end_utc": "1970-01-01T04:00:00Z",
        "complete": True,
        "gap_status": "none",
        "raw_payload_sha256": raw_payload_sha256(payload),
        "schema_version": "binance.v1",
    }
    with pytest.raises((ValueError, IndexError)):
        validate_and_sort_candles(
            [EvidenceCandle("AAAUSDT", "kline", 0, 14_400_000, payload, manifest)]
        )


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


def test_raw_mapping_values_are_bound_to_the_evidence_hash() -> None:
    left = {"open_time_ms": 0, "close_time_ms": 14_399_999, "volume": 1000.0}
    right = {"open_time_ms": 0, "close_time_ms": 14_399_999, "volume": 1.0}
    assert raw_payload_sha256(left) != raw_payload_sha256(right)


def test_feature_builder_rejects_future_prior_candle() -> None:
    feature = _epoch("AAAUSDT")
    # Rebuild the same clean window, but replace the first prior with a candle
    # after the explicitly supplied current candle.
    starts = [index * 14_400_000 for index in range(505)]
    current_start = starts[-1]
    future_start = starts[-1] + 14_400_000

    def candle(start: int, source: str, payload: tuple[object, ...]) -> EvidenceCandle:
        return EvidenceCandle(
            "AAAUSDT",
            source,
            start,
            start + 14_400_000,
            payload,
            build_epoch_manifest(
                source_id="binance_usdm.klines_4h"
                if source == "kline"
                else "binance_usdm.premium_index_klines_4h",
                endpoint_host="fapi.binance.com",
                endpoint_path="/fapi/v1/klines"
                if source == "kline"
                else "/fapi/v1/premiumIndexKlines",
                endpoint_version="v1",
                symbol="AAAUSDT",
                epoch_start_utc="1970-01-01T00:00:00Z",
                epoch_end_utc="1970-01-01T04:00:00Z",
                payload=payload,
                schema_version="binance.v1",
            ),
        )

    def kline(start: int) -> EvidenceCandle:
        return candle(
            start,
            "kline",
            (
                start,
                "1",
                "1",
                "1",
                "1",
                "2",
                start + 14_399_999,
                "10",
                "1",
                "1",
                "1",
                "0",
            ),
        )

    def premium(start: int) -> EvidenceCandle:
        return candle(
            start,
            "premium",
            (
                start,
                "0",
                "0",
                "0",
                "0",
                "0",
                start + 14_399_999,
                "0",
                "0",
                "0",
                "0",
                "0",
            ),
        )

    prior_kline = [kline(start) for start in starts[:-1]]
    prior_premium = [premium(start) for start in starts[:-1]]
    prior_kline[0] = kline(future_start)
    with pytest.raises(ValueError, match="latest|contiguous|strictly prior"):
        epoch_features_from_evidence(
            symbol="AAAUSDT",
            current_kline=kline(current_start),
            current_premium=premium(current_start),
            prior_klines=prior_kline,
            prior_premium=prior_premium,
            prior_30d_quote_volume=100.0,
        )
    assert feature.evidence.current_epoch_start_ms == current_start


def test_feature_builder_rejects_cross_endpoint_roles() -> None:
    feature = _epoch("AAAUSDT")
    with pytest.raises(ValueError, match="wrong source"):
        epoch_features_from_evidence(
            symbol="AAAUSDT",
            current_kline=EvidenceCandle(
                "AAAUSDT", "kline", 0, 14_400_000, (0,) * 12, {}
            ),
            current_premium=EvidenceCandle(
                "AAAUSDT", "premium", 0, 14_400_000, (0,) * 12, {}
            ),
            prior_klines=(),
            prior_premium=(),
            prior_30d_quote_volume=100.0,
        )
    with pytest.raises(TypeError, match="evidence-bound"):
        contract.score_symbol({})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="evidence-bound PIT"):
        contract.evaluate_basket({})  # type: ignore[arg-type]
    assert feature.symbol == "AAAUSDT"
