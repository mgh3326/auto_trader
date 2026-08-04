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
    inner_align_4h,
    raw_payload_sha256,
    validate_and_sort_candles,
)


def _epoch(symbol: str, *, current: float = 1.0) -> contract.EpochFeatures:
    return contract.EpochFeatures(
        symbol=symbol,
        integrity=contract.IntegrityState.COMPLETE,
        current_ofi=current,
        current_premium_close=current,
        prior_ofi=(0.0,) * 504,
        prior_premium_close=(0.0,) * 504,
        prior_quote_volume_30d=100.0,
    )


def test_new_identity_and_v2_preservation_are_explicit() -> None:
    payload = contract.contract_as_machine_data()
    assert contract.CONTRACT_ID == "DFC-2C-4H-v2.1"
    assert contract.CANONICAL_HASH == contract.canonical_contract_hash()
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload
    assert payload["predecessor_preservation"]["v2_row_mutation"] == "forbidden"
    assert payload["implementation"]["module_source_sha256"] == contract.MODULE_SOURCE_SHA256


def test_import_time_source_freeze_is_present_and_runtime_hash_is_current() -> None:
    source = inspect.getsource(contract)
    assert "RuntimeError" in source
    assert len(re.findall(r"^MODULE_SOURCE_SHA256: Final =", source, re.MULTILINE)) == 1
    assert len(contract.MODULE_SOURCE_SHA256) == 64


def test_composite_tail_is_derived_and_input_has_no_prior_composite_argument() -> None:
    assert "prior_abs_composite" not in inspect.signature(contract.EpochFeatures).parameters
    assert list(inspect.signature(contract.tail_threshold_q75).parameters) == [
        "prior_ofi", "prior_premium_close"
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
        with pytest.raises(ValueError, match="exactly 504"):
            contract.score_symbol(_epoch("AAAUSDT").__class__(
                symbol="AAAUSDT", integrity=contract.IntegrityState.COMPLETE,
                current_ofi=1.0, current_premium_close=1.0,
                prior_ofi=(0.0,) * 300, prior_premium_close=(0.0,) * 300,
                prior_quote_volume_30d=100.0,
            ))
    finally:
        contract.PIT_LOOKBACK = original  # type: ignore[misc]


def test_pit_universe_is_dynamic_and_no_symbols_are_hardcoded() -> None:
    assert contract.select_universe({"SOLUSDT": 3.0, "XRPUSDT": 2.0, "DOGEUSDT": 1.0, "AAAUSDT": 4.0}) == (
        "AAAUSDT", "SOLUSDT", "XRPUSDT"
    )
    assert contract.contract_as_machine_data()["universe"]["hardcoded_symbols"] is False


def test_control_a_is_symmetric_and_adjudication_is_fully_numeric() -> None:
    payload = contract.contract_as_machine_data()
    assert payload["estimand"]["control_choice"] == "A"
    assert payload["estimand"]["selection_symmetric"] is True
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


def test_harness_hashes_validates_aligns_and_rejects_future_windows() -> None:
    payload = ["x", 1]
    manifest = {
        "source_id": "binance_usdm.klines_4h", "endpoint_host": "fapi.binance.com",
        "endpoint_path": "/fapi/v1/klines", "endpoint_version": "v1", "symbol": "AAAUSDT",
        "interval": "4h", "epoch_start_utc": "2021-05-02T00:00:00Z",
        "epoch_end_utc": "2021-05-02T04:00:00Z", "complete": True, "gap_status": "none",
        "raw_payload_sha256": raw_payload_sha256(payload), "schema_version": "binance.v1",
    }
    left = EvidenceCandle("AAAUSDT", "kline", 0, 14_400_000, tuple(payload), manifest)
    right = EvidenceCandle("AAAUSDT", "premium", 0, 14_400_000, tuple(payload), {**manifest, "endpoint_path": "/fapi/v1/premiumIndexKlines"})
    assert inner_align_4h([left], [right]) == (("AAAUSDT", 0),)
    assert validate_and_sort_candles([left]) == (left,)
    with pytest.raises(ValueError, match="future"):
        assert_forward_only([100], {100: tuple(range(252)) + (100,) + tuple(range(252, 503))})


def test_contract_remains_offline_only() -> None:
    source = inspect.getsource(contract)
    for forbidden in ("import app", "from app", "httpx", "requests", "socket", "subprocess"):
        assert forbidden not in source
