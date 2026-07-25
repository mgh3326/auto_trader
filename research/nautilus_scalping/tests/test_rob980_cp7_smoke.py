from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def smoke_module():
    spec = importlib.util.find_spec("rob974_h3_smoke")
    assert spec is not None, (
        "ROB-980 CP7 persisted real-H1 generator smoke is not implemented"
    )
    return importlib.import_module("rob974_h3_smoke")


@pytest.fixture(scope="module")
def smoke_result(smoke_module, tmp_path_factory: pytest.TempPathFactory):
    return smoke_module.run_persisted_generator_smoke(
        tmp_path_factory.mktemp("rob980-cp7")
    )


def test_persisted_real_h1_smoke_is_non_vacuous_for_both_strategies(smoke_result):
    assert smoke_result.generator_smoke == "PASS"
    assert smoke_result.actual_h2_engine_integration == "NOT_EVALUATED"
    assert tuple(item.strategy for item in smoke_result.strategies) == ("S3", "S4")
    expected = {
        "S3": (
            576,
            574,
            2,
            1,
            1,
            "cd0367dcd1b3d0723e7f7815144d2120dba80f0f2603fe1a88eb3f7b234df7ac",
        ),
        "S4": (
            576,
            570,
            6,
            2,
            4,
            "778381ed58a245dce084a76a855966914afe9377264a75a82fcf70cccee0545c",
        ),
    }
    for strategy in smoke_result.strategies:
        evidence = strategy.evidence
        assert evidence.global_invocation_count == 1
        assert evidence.evaluated_decision_units > 0
        assert evidence.no_signal > 0
        assert evidence.candidate > 0
        assert evidence.generator_rejected > 0
        assert evidence.generator_accepted > 0
        assert (
            evidence.evaluated_decision_units == evidence.no_signal + evidence.candidate
        )
        assert evidence.candidate == (
            evidence.generator_rejected + evidence.generator_accepted
        )
        assert sum(value for _, value in evidence.no_signal_reason_histogram) == (
            evidence.no_signal
        )
        assert (
            sum(value for _, value in evidence.generator_rejection_reason_histogram)
            == evidence.generator_rejected
        )
        assert (
            dict(evidence.no_signal_reason_histogram)["missing_required_context"] >= 3
        )
        assert (
            evidence.evaluated_decision_units,
            evidence.no_signal,
            evidence.candidate,
            evidence.generator_rejected,
            evidence.generator_accepted,
            evidence.content_hash,
        ) == expected[strategy.strategy]
    assert smoke_result.feature_hash == (
        "5abc827a3a3d28c02d4ad5313a299dcf0bbf95397c66d80d5e359d95193e19a2"
    )
    assert smoke_result.strategies[0].accepted_payloads[0].symbol == "XRPUSDT"
    assert smoke_result.strategies[0].accepted_payloads[0].side == "long"
    assert smoke_result.strategies[1].accepted_payloads[0].pair == "XRP-SOL"
    assert smoke_result.strategies[1].accepted_payloads[0].side == "short_a_long_b"


def test_localized_gap_is_absent_only_at_its_close_and_later_recovers(smoke_result):
    assert smoke_result.gap_symbol == "SOLUSDT"
    assert smoke_result.gap_close_absent
    assert smoke_result.other_symbol_gap_close_present
    assert smoke_result.recovery_close_present
    assert smoke_result.recovery_close > smoke_result.gap_close


def test_smoke_hashes_are_mapping_order_invariant_and_payloads_are_historical_only(
    smoke_result,
):
    assert smoke_result.mapping_permutation_hashes_match
    assert len(smoke_result.feature_hash) == 64
    assert len(smoke_result.lineage_hash) == 64
    assert len(smoke_result.content_hash) == 64
    s4_strategy = smoke_result.strategies[1]
    for accepted in s4_strategy.accepted_payloads:
        assert accepted.volatility_percentile is None
        assert accepted.volatility_percentile_provenance == "not_defined_for_s4"
        assert accepted.historical_eligibility is True
        assert accepted.leg_a_order_id is None
        assert accepted.leg_b_order_id is None
        assert accepted.leg_a_fill_id is None
        assert accepted.leg_b_fill_id is None
        assert accepted.pair_executor_provenance == "not_evaluated_h3_generator"


def test_prepared_fold00_packet_is_absolute_bounded_and_never_executed(smoke_module):
    packet = smoke_module.prepared_fold00_packet()
    assert packet["status"] == "NOT_EXECUTED_AWAITING_ORCH_GO"
    command = packet["command"]
    assert command.startswith("env -i PATH=/Users/mgh3326/.local/bin:/usr/bin:/bin ")
    assert " /Users/mgh3326/.local/bin/uv run python " in command
    assert "--fold-id fold-00" in command
    assert "--phase selected_oos" in command
    assert "--manifest /Users/mgh3326/work/auto_trader.rob-980/" in command
    assert "--data-root /Users/mgh3326/work/auto_trader.rob-980/" in command
    assert "rob-979" not in command
    schema = json.loads(packet["json_schema"])
    assert schema["additionalProperties"] is False
    required = set(schema["required"])
    assert {
        "authorities",
        "window",
        "universe",
        "phase",
        "strategies",
    } <= required
    assert schema["properties"]["authorities"]["additionalProperties"] is False
    assert schema["properties"]["window"]["additionalProperties"] is False
    strategy_schemas = schema["properties"]["strategies"]["properties"]
    assert tuple(strategy_schemas) == ("S3", "S4")
    assert all(
        item["additionalProperties"] is False for item in strategy_schemas.values()
    )


def test_smoke_result_json_is_stable_and_contains_closed_zero_bins(smoke_result):
    rendered = smoke_result.to_json_bytes()
    assert rendered.endswith(b"\n")
    assert rendered == smoke_result.to_json_bytes()
    payload = json.loads(rendered)
    assert payload["generator_smoke"] == "PASS"
    assert payload["actual_h2_engine_integration"] == "NOT_EVALUATED"
    assert tuple(payload["strategies"]) == ("S3", "S4")
    for name, strategy in payload["strategies"].items():
        assert 0 in strategy["no_signal_reason_histogram"].values()
        assert tuple(strategy["generator_rejection_reason_histogram"]) == (
            "simultaneous_candidate_arbitration_loser"
            if name == "S3"
            else "simultaneous_pair_arbitration_loser",
        )
        assert (
            sum(strategy["generator_rejection_reason_histogram"].values())
            == (strategy["generator_rejected"])
        )
    assert Path(payload["persisted_root"]).is_absolute()
