import ast
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.us_upside_instrumentation import (
    CONTRACT_SHA256,
    FROZEN_ARMS,
    InstrumentationInput,
    append_session_jsonl,
    evaluate_instrumentation,
    load_session_jsonl,
    read_three_completed_sessions,
)
from scripts.run_us_upside_instrumentation import main, parse_args


def _payload(*, session_id: str = "2026-08-14-rth") -> dict[str, object]:
    return {
        "session_id": session_id,
        "contract_sha": CONTRACT_SHA256,
        "policy_sha": "a" * 64,
        "code_sha": "b" * 40,
        "source_corpus_as_of": "2026-08-14T22:35:00+09:00",
        "decision_cutoff": "2026-08-14T22:35:00+09:00",
        "universe_hash": "c" * 64,
        "sources": [
            {
                "source_id": "analyst-feed",
                "upstream_total_known": 1,
                "upstream_total_unknown": False,
                "returned_count": 1,
                "timeout_or_error_count": 0,
                "unqueried_count": 0,
                "top_n_cap": None,
                "outside_top_n_count": 0,
                "deduped_unique_count": 1,
            },
            {
                "source_id": "support-feed",
                "upstream_total_known": 1,
                "upstream_total_unknown": False,
                "returned_count": 1,
                "timeout_or_error_count": 0,
                "unqueried_count": 0,
                "top_n_cap": None,
                "outside_top_n_count": 0,
                "deduped_unique_count": 1,
            },
        ],
        "candidate_array_coverage": {
            "deduped_unique_count": 1,
            "recorded_candidate_count": 1,
            "truncated_candidate_count": 0,
            "candidate_array_cap": None,
            "candidate_truncation_reason": None,
        },
        "candidates": [
            {
                "symbol": "ACME",
                "matched_sources": [
                    {"source_id": "analyst-feed", "rank": 1},
                    {"source_id": "support-feed", "rank": 1},
                ],
                "freshness": "fresh",
                "consensus_status": "value",
                "target_honesty": "honest",
                "target_as_of": "2026-08-14T22:30:00+09:00",
                "analyst_count": 4,
                "current_price": 100.0,
                "target": 130.0,
                "rsi": 42.0,
                "support_price": 90.0,
                "support_strength": "strong",
                "independent_support_families": [
                    "price_structure",
                    "volume_profile",
                    "moving_average",
                ],
                "non_upside_gate_bits": {
                    "source": "pass",
                    "freshness": "pass",
                    "support": "pass",
                },
                "proposed_limit": 87.0,
                "tick_handling": {
                    "rule": "US-cent rounding down to the displayed limit",
                    "raw_limit": 87.004,
                    "snapped_limit": 87.0,
                    "direction": "down",
                },
                "feasibility": {
                    "sector": "Industrials",
                    "sector_feasibility": "pass",
                    "dedupe_feasibility": "pass",
                    "cash_feasibility": "pass",
                    "whole_share_feasibility": "pass",
                    "would_size": 1.0,
                    "required_cash": 87.0,
                },
                "hypothetical_limit_touch": {
                    "next_session_high": 94.0,
                    "next_session_low": 86.0,
                    "limit_touched": True,
                },
            }
        ],
    }


def _record(*, session_id: str = "2026-08-14-rth"):
    return evaluate_instrumentation(
        InstrumentationInput.model_validate(_payload(session_id=session_id)),
        input_hash="d" * 64,
    )


def test_arms_are_exactly_frozen_and_shadow_only():
    assert [arm.model_dump() for arm in FROZEN_ARMS] == [
        {
            "arm_id": "A40",
            "upside_min_pct": 40,
            "required_support_strength": None,
            "independent_family_min": None,
            "final_discount_min_pct": None,
            "final_discount_max_pct": None,
            "diagnostic_only": False,
            "shadow_only": True,
        },
        {
            "arm_id": "B30",
            "upside_min_pct": 30,
            "required_support_strength": "strong",
            "independent_family_min": 3,
            "final_discount_min_pct": 12,
            "final_discount_max_pct": 15,
            "diagnostic_only": False,
            "shadow_only": True,
        },
        {
            "arm_id": "C25",
            "upside_min_pct": 25,
            "required_support_strength": "strong",
            "independent_family_min": 3,
            "final_discount_min_pct": 11,
            "final_discount_max_pct": 15,
            "diagnostic_only": True,
            "shadow_only": True,
        },
    ]


def test_cli_has_no_threshold_or_arm_override():
    with pytest.raises(SystemExit):
        parse_args(
            [
                "record",
                "--input",
                "capture.json",
                "--output",
                "records.jsonl",
                "--threshold",
                "25",
            ]
        )


def test_record_preserves_every_q4_field_and_is_read_only():
    record = _record()
    candidate = record.candidates[0]
    source = record.sources[0]

    assert record.contract_sha == CONTRACT_SHA256
    assert record.policy_sha == "a" * 64
    assert record.code_sha == "b" * 40
    assert record.source_corpus_as_of == "2026-08-14T22:35:00+09:00"
    assert record.decision_cutoff == "2026-08-14T22:35:00+09:00"
    assert record.universe_hash == "c" * 64
    assert record.input_hash == "d" * 64
    assert source.model_dump() == {
        "source_id": "analyst-feed",
        "upstream_total_known": 1,
        "upstream_total_unknown": False,
        "returned_count": 1,
        "timeout_or_error_count": 0,
        "unqueried_count": 0,
        "top_n_cap": None,
        "outside_top_n_count": 0,
        "deduped_unique_count": 1,
    }
    assert record.candidate_array_coverage.model_dump() == {
        "deduped_unique_count": 1,
        "recorded_candidate_count": 1,
        "truncated_candidate_count": 0,
        "candidate_array_cap": None,
        "candidate_truncation_reason": None,
    }
    assert candidate.matched_sources[0].model_dump() == {
        "source_id": "analyst-feed",
        "rank": 1,
    }
    assert candidate.freshness == "fresh"
    assert candidate.target_honesty == "honest"
    assert candidate.target_as_of == "2026-08-14T22:30:00+09:00"
    assert candidate.analyst_count == 4
    assert candidate.upside_pct == 30.0
    assert candidate.support_distance_pct == 10.0
    assert candidate.final_discount_from_current_pct == 13.0
    assert candidate.arithmetic_limit_basis_upside_pct == 49.425287
    assert candidate.independent_support_families == (
        "price_structure",
        "volume_profile",
        "moving_average",
    )
    assert candidate.independent_support_family_count == 3
    assert candidate.hypothetical_limit_touch is not None
    assert candidate.hypothetical_limit_touch.limit_touched is True
    assert record.arm_shadow_counts == {"A40": 0, "B30": 1, "C25": 1}
    assert record.read_only_safety.model_dump() == {
        "eligibility_connections": 0,
        "proposals_created": 0,
        "orders_created": 0,
        "broker_calls": 0,
        "database_writes": 0,
        "scheduler_registrations": 0,
        "threshold_overrides": 0,
    }


def test_exact_candidate_log_shape_keeps_hypothetical_touch_separate():
    payload = _record().model_dump(mode="json")
    assert set(payload["candidates"][0]) == {
        "symbol",
        "matched_sources",
        "freshness",
        "consensus_status",
        "target_honesty",
        "target_as_of",
        "analyst_count",
        "current_price",
        "target",
        "rsi",
        "support_price",
        "support_strength",
        "independent_support_families",
        "non_upside_gate_bits",
        "proposed_limit",
        "tick_handling",
        "feasibility",
        "hypothetical_limit_touch",
        "upside_pct",
        "support_distance_pct",
        "independent_support_family_count",
        "final_discount_from_current_pct",
        "arithmetic_limit_basis_upside_pct",
        "arm_results",
    }
    assert payload["candidates"][0]["hypothetical_limit_touch"] == {
        "next_session_high": 94.0,
        "next_session_low": 86.0,
        "limit_touched": True,
    }


def test_serialized_jsonl_keeps_caps_and_hypothetical_touch_name(tmp_path: Path):
    output = tmp_path / "session.jsonl"
    append_session_jsonl(output, _record())
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert set(payload["sources"][0]) == {
        "source_id",
        "upstream_total_known",
        "upstream_total_unknown",
        "returned_count",
        "timeout_or_error_count",
        "unqueried_count",
        "top_n_cap",
        "outside_top_n_count",
        "deduped_unique_count",
    }
    assert "hypothetical_limit_touch" in payload["candidates"][0]
    assert payload["candidates"][0]["hypothetical_limit_touch"]["limit_touched"]
    assert payload["candidate_array_coverage"] == {
        "deduped_unique_count": 1,
        "recorded_candidate_count": 1,
        "truncated_candidate_count": 0,
        "candidate_array_cap": None,
        "candidate_truncation_reason": None,
    }


def test_dominant_constraint_rule_is_bounded_and_never_a_tuning_signal():
    interpretation = _record().interpretation

    assert interpretation.conclusion == "upside_dominant_constraint_bounded_cohort"
    assert interpretation.passes_all_non_upside_gates == 1
    assert interpretation.survivor_consensus_statuses == ("value",)
    assert interpretation.upside_band_counts == {
        "below_25": 0,
        "25_to_40": 1,
        "ge_40": 0,
        "upside_missing": 0,
    }
    assert interpretation.fanout_performance_or_alpha_inferred is False
    assert interpretation.threshold_tuning_permitted is False


def test_arm_gate_bits_and_reject_reasons_are_exact_per_counterfactual():
    payload = _payload()
    candidate = payload["candidates"][0]
    assert isinstance(candidate, dict)
    candidate["support_strength"] = "weak"
    candidate["independent_support_families"] = ["price_structure", "volume"]
    candidate["proposed_limit"] = 90.0
    tick_handling = candidate["tick_handling"]
    assert isinstance(tick_handling, dict)
    tick_handling["raw_limit"] = 90.0
    tick_handling["snapped_limit"] = 90.0

    record = evaluate_instrumentation(
        InstrumentationInput.model_validate(payload), input_hash="d" * 64
    )
    a40, b30, c25 = record.candidates[0].arm_results

    assert a40.gate_bits["upside_minimum"] is False
    assert a40.reject_reasons == ("upside_below_40pct",)
    assert b30.gate_bits["support_strength_strong"] is False
    assert b30.gate_bits["independent_support_family_minimum"] is False
    assert b30.gate_bits["final_discount_distance"] is False
    assert b30.reject_reasons == (
        "support_strength_not_strong",
        "independent_support_families_below_3",
        "final_discount_outside_12_to_15_pct",
    )
    assert c25.reject_reasons == (
        "support_strength_not_strong",
        "independent_support_families_below_3",
        "final_discount_outside_11_to_15_pct",
    )
    assert not b30.would_select
    assert not c25.would_select


def test_zero_pre_upside_survivors_is_an_earlier_constraint():
    payload = _payload()
    candidate = payload["candidates"][0]
    assert isinstance(candidate, dict)
    candidate["non_upside_gate_bits"] = {
        "source": "pass",
        "freshness": "fail",
        "support": "pass",
    }

    record = evaluate_instrumentation(
        InstrumentationInput.model_validate(payload), input_hash="d" * 64
    )

    assert (
        record.interpretation.conclusion
        == "earlier_source_freshness_or_support_constraint"
    )
    assert record.interpretation.passes_all_non_upside_gates == 0
    assert record.candidates[0].arm_results[0].reject_reasons == (
        "non_upside_gate_freshness_fail",
        "upside_below_40pct",
    )


def test_unknown_timeout_or_unqueried_coverage_has_no_threshold_conclusion():
    payload = _payload()
    source = payload["sources"][0]
    assert isinstance(source, dict)
    source["upstream_total_known"] = None
    source["upstream_total_unknown"] = True
    source["timeout_or_error_count"] = 1
    source["unqueried_count"] = 2

    record = evaluate_instrumentation(
        InstrumentationInput.model_validate(payload), input_hash="d" * 64
    )

    assert record.coverage.model_dump() == {
        "upstream_total_unknown_source_count": 1,
        "timeout_or_error_count": 1,
        "unqueried_count": 2,
        "outside_top_n_count": 0,
        "candidate_array_truncated_count": 0,
        "candidate_array_cap": None,
        "candidate_truncation_reason": None,
        "candidate_consensus_status_counts": {
            "value": 1,
            "missing": 0,
            "stale": 0,
            "error": 0,
            "timeout": 0,
            "unknown": 0,
            "unqueried": 0,
        },
        "coverage_complete": False,
    }
    assert (
        record.interpretation.conclusion
        == "coverage_insufficient_no_threshold_conclusion"
    )


def test_missing_top_n_census_is_rejected_before_recording():
    payload = _payload()
    source = payload["sources"][0]
    assert isinstance(source, dict)
    source.pop("outside_top_n_count")

    with pytest.raises(ValidationError, match="outside_top_n_count"):
        InstrumentationInput.model_validate(payload)


def test_top_n_capped_runbook_capture_is_coverage_insufficient(tmp_path: Path, capsys):
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "us_upside_instrumentation"
        / "runbook_top_n_capped_capture.json"
    )
    output = tmp_path / "top-n-capped.jsonl"

    assert main(["record", "--input", str(fixture), "--output", str(output)]) == 0

    stdout = capsys.readouterr().out
    record = load_session_jsonl(output)[0]
    assert record.coverage.outside_top_n_count == 7
    assert record.coverage.coverage_complete is False
    assert (
        record.interpretation.conclusion
        == "coverage_insufficient_no_threshold_conclusion"
    )
    assert '"coverage_complete": false' in stdout
    assert "coverage_insufficient_no_threshold_conclusion" in stdout


def test_known_source_total_requires_every_loss_to_be_declared():
    payload = _payload()
    source = payload["sources"][0]
    assert isinstance(source, dict)
    source["upstream_total_known"] = 10

    with pytest.raises(ValidationError, match="declared non-returned count"):
        InstrumentationInput.model_validate(payload)


def test_candidate_array_truncation_is_recorded_and_blocks_threshold_conclusion():
    payload = _payload()
    coverage = payload["candidate_array_coverage"]
    assert isinstance(coverage, dict)
    coverage.update(
        {
            "deduped_unique_count": 4,
            "recorded_candidate_count": 1,
            "truncated_candidate_count": 3,
            "candidate_array_cap": 1,
            "candidate_truncation_reason": "bounded_candidate_array",
        }
    )

    record = evaluate_instrumentation(
        InstrumentationInput.model_validate(payload), input_hash="d" * 64
    )

    assert record.candidate_array_coverage.truncated_candidate_count == 3
    assert record.candidate_array_coverage.candidate_truncation_reason == (
        "bounded_candidate_array"
    )
    assert record.coverage.candidate_array_truncated_count == 3
    assert record.coverage.coverage_complete is False
    assert (
        record.interpretation.conclusion
        == "coverage_insufficient_no_threshold_conclusion"
    )


def test_candidate_array_unrecorded_loss_is_rejected_before_recording():
    payload = _payload()
    coverage = payload["candidate_array_coverage"]
    assert isinstance(coverage, dict)
    coverage["deduped_unique_count"] = 4

    with pytest.raises(ValidationError, match="truncated_candidate_count"):
        InstrumentationInput.model_validate(payload)


def test_candidate_array_truncation_requires_cap_and_reason():
    payload = _payload()
    coverage = payload["candidate_array_coverage"]
    assert isinstance(coverage, dict)
    coverage.update(
        {
            "deduped_unique_count": 2,
            "truncated_candidate_count": 1,
            "candidate_array_cap": None,
            "candidate_truncation_reason": None,
        }
    )

    with pytest.raises(ValidationError, match="candidate_array_cap"):
        InstrumentationInput.model_validate(payload)


def test_missing_candidate_evidence_is_rejected_instead_of_defaulting_to_unknown():
    payload = _payload()
    candidate = payload["candidates"][0]
    assert isinstance(candidate, dict)
    candidate.pop("current_price")

    with pytest.raises(ValidationError, match="current_price"):
        InstrumentationInput.model_validate(payload)


def test_matched_source_rank_must_be_declared_even_when_unknown():
    payload = _payload()
    candidate = payload["candidates"][0]
    assert isinstance(candidate, dict)
    matches = candidate["matched_sources"]
    assert isinstance(matches, list)
    matches[0].pop("rank")

    with pytest.raises(ValidationError, match="rank"):
        InstrumentationInput.model_validate(payload)


def test_three_sessions_with_zero_b30_and_c25_only_diagnose_contracts():
    records = []
    for index in range(3):
        payload = _payload(session_id=f"2026-08-{14 + index}-rth")
        candidate = payload["candidates"][0]
        assert isinstance(candidate, dict)
        candidate["target"] = 120.0
        records.append(
            evaluate_instrumentation(
                InstrumentationInput.model_validate(payload), input_hash=str(index) * 64
            )
        )

    reading = read_three_completed_sessions(records)

    assert reading.a40_shadow_count == 0
    assert reading.b30_shadow_count == 0
    assert reading.c25_shadow_count == 0
    assert reading.all_sessions_coverage_complete is True
    assert reading.conclusion == "three_session_reading_complete"
    assert reading.next_step == "diagnose_target_coverage_or_support_source_contract"
    assert reading.threshold_tuning_permitted is False


def test_three_session_reading_propagates_any_incomplete_coverage_to_unknown():
    records = []
    for index in range(3):
        payload = _payload(session_id=f"2026-08-{14 + index}-rth")
        if index == 1:
            source = payload["sources"][0]
            assert isinstance(source, dict)
            source.update(
                {
                    "upstream_total_known": 10,
                    "returned_count": 3,
                    "top_n_cap": 3,
                    "outside_top_n_count": 7,
                    "deduped_unique_count": 1,
                }
            )
        records.append(
            evaluate_instrumentation(
                InstrumentationInput.model_validate(payload), input_hash=str(index) * 64
            )
        )

    reading = read_three_completed_sessions(records)

    assert reading.all_sessions_coverage_complete is False
    assert reading.conclusion == "coverage_insufficient_no_threshold_conclusion"
    assert reading.next_step == "coverage_insufficient_no_threshold_conclusion"
    assert reading.threshold_tuning_permitted is False
    assert [coverage.coverage_complete for coverage in reading.session_coverage] == [
        True,
        False,
        True,
    ]
    assert reading.session_coverage[1].outside_top_n_count == 7


def test_three_session_read_rejects_a_mid_observation_code_change():
    records = [_record(session_id=f"2026-08-{14 + index}-rth") for index in range(3)]
    changed = records[-1].model_copy(update={"code_sha": "e" * 40})

    with pytest.raises(ValueError, match="one code_sha"):
        read_three_completed_sessions((records[0], records[1], changed))


def test_service_source_has_no_runtime_mutation_connection():
    service_path = (
        Path(__file__).resolve().parents[2]
        / "app"
        / "services"
        / "us_upside_instrumentation.py"
    )
    source = service_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert not any(
        module == "app" or module.startswith("app.") for module in imported_modules
    )
    assert imported_modules.isdisjoint(
        {"httpx", "requests", "sqlalchemy", "redis", "taskiq", "subprocess"}
    )
    assert ".environ" not in source
    assert called_names.isdisjoint(
        {"submit_eligibility", "create_proposal", "place_order"}
    )
    assert called_attributes.isdisjoint({"submit", "create_proposal", "place_order"})


def test_cli_appends_records_and_reads_exactly_three(tmp_path: Path, capsys):
    output = tmp_path / "sessions.jsonl"
    for index in range(3):
        input_path = tmp_path / f"session-{index}.json"
        input_path.write_text(
            json.dumps(_payload(session_id=f"2026-08-{14 + index}-rth")),
            encoding="utf-8",
        )
        assert (
            main(["record", "--input", str(input_path), "--output", str(output)]) == 0
        )

    assert len(load_session_jsonl(output)) == 3
    assert main(["read-three", "--records", str(output)]) == 0
    stdout = capsys.readouterr().out
    assert "continue_bounded_cohort_reading_without_tuning" in stdout
    assert '"threshold_tuning_permitted": false' in stdout


def test_record_rejects_duplicate_session_id_in_one_jsonl_artifact(tmp_path: Path):
    output = tmp_path / "sessions.jsonl"

    append_session_jsonl(output, _record())

    with pytest.raises(ValueError, match="already contains this session_id"):
        append_session_jsonl(output, _record())


def test_input_rejects_unknown_source_reference():
    payload = copy.deepcopy(_payload())
    candidate = payload["candidates"][0]
    assert isinstance(candidate, dict)
    candidate["matched_sources"] = [{"source_id": "unlisted", "rank": 1}]

    with pytest.raises(ValidationError, match="references unknown source"):
        InstrumentationInput.model_validate(payload)
