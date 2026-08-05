from __future__ import annotations

import hashlib
import inspect
from datetime import date

import pytest

from research.us_stage_b.contracts import (
    ExplorationWindowError,
    USCostLiteral,
    USStageBRunContract,
)
from research.us_stage_b.registry import (
    FROZEN_CANDIDATES_SHA256,
    US_CANDIDATE_ORDER,
    CandidateRegistry,
    RegistryStartRejected,
)

from .conftest import FROZEN_YAML, candidate, sequential_sessions


def test_registry_parses_original_packet_bytes_and_stamps_raw_contract_hashes(
    registry: CandidateRegistry,
) -> None:
    source = FROZEN_YAML.read_bytes()
    assert hashlib.sha256(source).hexdigest() == FROZEN_CANDIDATES_SHA256
    assert tuple(item.strategy_id for item in registry.admitted) == US_CANDIDATE_ORDER
    assert [item.contract_hash for item in registry.admitted] == [
        "25d06a5e81c03a254948ee20b9180d466e5531a63715dd1ba53b7160ce032509",
        "7052f69ae1ae20de53750276c006959694941a1b2a430c99c8dbbe616cba9836",
        "43ff9a4a99ba3717c2d5563aa58c8a482800082c4fa4c41330712c4460848b0f",
    ]
    for item in registry.admitted:
        payload = item.to_dict()
        assert payload["strategy_id"] == item.strategy_id
        assert payload["contract_hash"] == item.contract_hash
        assert "EXPLORATORY_FALSIFICATION_ONLY" in payload["labels"]
        assert "SURVIVORSHIP_BIASED=TRUE" in payload["labels"]


def test_registry_refuses_any_packet_byte_drift_before_parsing() -> None:
    source = FROZEN_YAML.read_bytes()
    with pytest.raises(RegistryStartRejected, match="SHA mismatch"):
        CandidateRegistry.from_verbatim_bytes(
            source + b" ", expected_sha256=FROZEN_CANDIDATES_SHA256
        )


def test_registry_refuses_unknown_strategy_instead_of_shared_fallback(
    registry: CandidateRegistry,
) -> None:
    with pytest.raises(RegistryStartRejected, match="shared fallback"):
        registry.binding_for("us_momentum")


def test_cost_literals_are_explicit_and_holdout_intersection_is_refused(
    registry: CandidateRegistry,
) -> None:
    signature = inspect.signature(USCostLiteral)
    assert signature.parameters["base_bp_per_side"].default is inspect.Parameter.empty
    assert (
        signature.parameters["sensitivity_bp_per_side"].default
        is inspect.Parameter.empty
    )
    with pytest.raises(ValueError, match="exactly 10bp/side"):
        USCostLiteral(base_bp_per_side=9, sensitivity_bp_per_side=5)

    with pytest.raises(ExplorationWindowError, match="2025"):
        USStageBRunContract(
            candidate=candidate(registry, US_CANDIDATE_ORDER[0]),
            exploration_start=date(2024, 12, 30),
            exploration_end=date(2025, 1, 1),
            cost=USCostLiteral(base_bp_per_side=10, sensitivity_bp_per_side=5),
        )


def test_run_contract_is_stamped_and_does_not_inject_cost_defaults(
    registry: CandidateRegistry,
) -> None:
    sessions = sequential_sessions(3)
    binding = candidate(registry, US_CANDIDATE_ORDER[1])
    run = USStageBRunContract(
        candidate=binding,
        exploration_start=sessions[0],
        exploration_end=sessions[-1],
        cost=USCostLiteral(base_bp_per_side=10, sensitivity_bp_per_side=5),
    )
    payload = run.to_dict()
    assert payload["strategy_id"] == binding.strategy_id
    assert payload["contract_hash"] == binding.contract_hash
    assert payload["cost_literal"] == {
        "base_bp_per_side": 10,
        "base_round_trip_bp": 20,
        "sensitivity_bp_per_side": 5,
        "sensitivity_round_trip_bp": 10,
    }
