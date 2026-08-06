from __future__ import annotations

import hashlib

import pytest

from research.crypto_stage_b.registry import (
    ADMITTED_STRATEGY_IDS,
    CandidateParseError,
    CandidateRegistry,
)


def _candidate_block(strategy_id: str, *, history_days: int) -> str:
    return f"""- strategy_id: {strategy_id}
  family_id: TEST
  venue_scope: both
  signal: |
    source signal
  required_history: {history_days}개의 연속된 UTC 일봉
  entry: next_day_open_utc
  exit: entry day D close
  ranking: >
    source ranking
  sizing: >
    source sizing
  parameter_values:
    exit_D_plus_days: 3
    max_concurrent_positions_per_venue: 5
  expected_trade_frequency: |
    HARNESS_QUERY: source schema
  falsification: >
    source ablation
  empirical_status: UNTESTED
"""


def _source() -> bytes:
    return (
        "# verbatim fixture\n"
        + _candidate_block("CR-SPOT-ETR-01", history_days=252)
        + _candidate_block("CR-SPOT-TPR-01", history_days=120)
        + _candidate_block("CR-SPOT-CEB-01", history_days=122)
        + _candidate_block("CR-SPOT-HTA-01", history_days=251)
        + "요구 산출물 2\n"
    ).encode()


def test_registry_parses_raw_blocks_and_binds_each_contract_hash() -> None:
    source = _source()
    registry = CandidateRegistry.from_verbatim_bytes(
        source,
        expected_return_sha256=hashlib.sha256(source).hexdigest(),
    )

    assert (
        tuple(item.strategy_id for item in registry.admitted) == ADMITTED_STRATEGY_IDS
    )
    assert registry.preserved_not_implemented.strategy_id == "CR-SPOT-HTA-01"
    assert all(item.contract_hash for item in registry.definitions)
    assert all(
        item.to_dict()["contract_hash"] == item.contract_hash
        for item in registry.definitions
    )
    etr = registry.get("CR-SPOT-ETR-01")
    assert etr.labels == (
        "CR-S1 verdict = BLOCKED (B2 unresolved)",
        "ETR-01×Upbit PASS = exploratory, not promotable",
    )
    assert etr.to_dict()["labels"] == etr.labels
    assert registry.get("CR-SPOT-TPR-01").labels == ()


def test_registry_refuses_one_byte_drift_before_parsing() -> None:
    source = _source()
    with pytest.raises(CandidateParseError, match="SHA-256 mismatch"):
        CandidateRegistry.from_verbatim_bytes(
            source + b" ",
            expected_return_sha256=hashlib.sha256(source).hexdigest(),
        )
