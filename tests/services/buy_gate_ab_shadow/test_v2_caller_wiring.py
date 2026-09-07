"""ROB-1351 v2 caller wiring: pre-arming witness boundaries."""

from __future__ import annotations

import copy
import logging
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

import app.services.buy_gate_ab_shadow.evaluate_v2 as evaluate_v2
from app.mcp_server.tooling.analysis_readonly_registration import (
    ANALYSIS_READONLY_TOOL_NAMES,
)
from app.mcp_server.tooling.buy_gate_ab_shadow_v2 import (
    evaluate_buy_gate_ab_shadow_v2_impl,
)
from app.mcp_server.tooling.buy_gate_ab_shadow_v2_registration import (
    register_buy_gate_ab_shadow_v2_tools,
)
from app.mcp_server.tooling.route_request_lanes import (
    MUTATION_TOOLS,
    READ_ONLY_ADVISORY_TOOLS,
)
from app.services.buy_gate_ab_shadow.evaluate_v2 import (
    CandidateEvidence,
    EvaluationError,
    evaluate_candidate,
)
from app.services.buy_gate_ab_shadow.forecast_tag_v2 import (
    build_shadow_buy_forecasts,
)
from app.services.buy_gate_ab_shadow.spec import (
    PINNED_POLICY_PROJECTION_SHA256,
    PINNED_SPEC_SHA256,
    policy_projection_sha256,
    spec_sha256,
)
from app.services.buy_gate_ab_shadow.spec_v2 import (
    EXPERIMENT_ID_V2,
    FORBIDDEN_V2,
    PINNED_POLICY_PROJECTION_SHA256_V2,
    PINNED_SPEC_SHA256_V2,
    PRE_REGISTRATION_V2,
    policy_projection_sha256_v2,
    spec_sha256_v2,
)
from app.services.buy_gate_ab_shadow_recorder import (
    MAX_FANOUT_WITNESS_CANDIDATES,
    maybe_record_buy_gate_ab_shadow,
)

pytestmark = pytest.mark.unit

_AS_OF = datetime(2026, 9, 7, 6, 30, tzinfo=UTC)
_SHARED_GATES = PRE_REGISTRATION_V2["shared_gates"]
_GATE_KEYS = tuple(_SHARED_GATES["other_gate_bit_keys"])
_A_MIN = PRE_REGISTRATION_V2["variant_a"]["support_strength_min"]
_B_MIN = PRE_REGISTRATION_V2["variant_b"]["support_strength_min"]
_STRONG = PRE_REGISTRATION_V2["support_strength_order"][-1]
_RSI_PASS = str(Decimal(str(_SHARED_GATES["rsi_max"])) - Decimal("1"))
_SUPPORT_DISTANCE_PASS = str(
    Decimal(str(_SHARED_GATES["support_within_pct"])) / Decimal("2")
)
_UPSIDE_PASS = str(Decimal(str(_SHARED_GATES["upside_min_pct"])) + Decimal("1"))


def _row(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": "005930",
        "market": "kr",
        "current_price": "70000",
        "support_strength": _A_MIN,
        "support_distance_pct": _SUPPORT_DISTANCE_PASS,
        "rsi": _RSI_PASS,
        "honest_upside_pct": _UPSIDE_PASS,
        "other_gate_bits": dict.fromkeys(_GATE_KEYS, True),
    }
    payload.update(overrides)
    return payload


def _evaluation(**overrides: object):
    return evaluate_candidate(
        CandidateEvidence.from_mapping(_row(**overrides)),
        evaluation_as_of=_AS_OF,
    )


def _fanout_candidate(symbol: str = "005930") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "funnel": {
            "base_eligibility": {"status": "pass", "current_price": "70000"},
            "support_source_count": {
                "status": "pass",
                "strength": _A_MIN,
                "distance_pct": _SUPPORT_DISTANCE_PASS,
            },
            "rsi": {"status": "regular_pass", "rsi_14": _RSI_PASS},
            "upside": {
                "status": "pass",
                "honest_upside_pct": _UPSIDE_PASS,
            },
        },
    }


def _fanout_result(*, count: int = 1) -> dict[str, Any]:
    return {
        "success": True,
        "market": "kr",
        "candidates": [
            _fanout_candidate(f"{index:06d}") for index in range(1, count + 1)
        ],
    }


def test_v2_thresholds_are_read_from_its_registration_not_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _evaluation().cohort == "a_and_b"

    changed_v2 = deepcopy(PRE_REGISTRATION_V2)
    changed_v2["shared_gates"]["rsi_max"] = str(
        Decimal(str(_SHARED_GATES["rsi_max"])) - Decimal("10")
    )
    monkeypatch.setattr(evaluate_v2, "PRE_REGISTRATION_V2", changed_v2)
    assert _evaluation().cohort == "neither"

    untouched_v2 = deepcopy(PRE_REGISTRATION_V2)
    monkeypatch.setattr(evaluate_v2, "PRE_REGISTRATION_V2", untouched_v2)
    import app.services.buy_gate_ab_shadow.spec as spec_v1

    changed_v1 = deepcopy(spec_v1.PRE_REGISTRATION)
    changed_v1["shared_gates"]["rsi_max"] = Decimal("100")
    monkeypatch.setattr(spec_v1, "PRE_REGISTRATION", changed_v1)
    assert _evaluation().cohort == "a_and_b"


def test_v2_variant_contract_cohorts_and_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moderate = _evaluation()
    assert moderate.variant_a.role == PRE_REGISTRATION_V2["variant_a"]["role"]
    assert moderate.variant_a.executes is PRE_REGISTRATION_V2["variant_a"]["executes"]
    assert moderate.variant_a.support_strength_min == _A_MIN
    assert moderate.variant_b.role == PRE_REGISTRATION_V2["variant_b"]["role"]
    assert moderate.variant_b.executes is PRE_REGISTRATION_V2["variant_b"]["executes"]
    assert (
        moderate.variant_b.register_as
        == PRE_REGISTRATION_V2["variant_b"]["register_as"]
    )
    assert moderate.variant_b.support_strength_min == _B_MIN
    assert moderate.variant_a.passed is True
    assert moderate.variant_b.passed is True
    assert moderate.cohort == "a_and_b"
    assert PRE_REGISTRATION_V2["only_difference"] == "support_strength_min"

    weak = _evaluation(support_strength=_B_MIN)
    assert weak.variant_a.passed is False
    assert weak.variant_b.passed is True
    assert weak.cohort == "b_only"

    changed_v2 = deepcopy(PRE_REGISTRATION_V2)
    changed_v2["variant_b"]["support_strength_min"] = _STRONG
    monkeypatch.setattr(evaluate_v2, "PRE_REGISTRATION_V2", changed_v2)
    with pytest.raises(EvaluationError, match="variant A cannot pass"):
        _evaluation()


def test_v2_a_cohort_labels_follow_support_strength() -> None:
    assert (
        _evaluation(support_strength=_STRONG).variant_a_cohort_label
        == (PRE_REGISTRATION_V2["variant_a"]["cohort_labels"][0])
    )
    assert (
        _evaluation(support_strength=_A_MIN).variant_a_cohort_label
        == (PRE_REGISTRATION_V2["variant_a"]["cohort_labels"][1])
    )


def test_v2_missing_or_non_boolean_shared_bits_reject_fail_closed() -> None:
    missing = _evaluation(other_gate_bits=dict.fromkeys(_GATE_KEYS[:-1], True))
    assert missing.cohort == "neither"
    assert missing.shared_gate_bits_supplied is False
    assert f"other_gate_{_GATE_KEYS[-1]}_failed" in missing.shared_reject_reasons

    non_boolean = _evaluation(
        other_gate_bits={
            _GATE_KEYS[0]: True,
            _GATE_KEYS[1]: True,
            _GATE_KEYS[2]: "true",
        }
    )
    assert non_boolean.cohort == "neither"
    assert non_boolean.shared_gate_bits_supplied is False
    assert f"other_gate_{_GATE_KEYS[-1]}_failed" in non_boolean.shared_reject_reasons


def test_v2_forecast_tag_forces_experiment_sample_from_actual_bits() -> None:
    incomplete = _evaluation(other_gate_bits={})
    payloads = build_shadow_buy_forecasts(
        incomplete,
        created_by="reviewer",
        experiment_sample=True,
    )
    assert [payload["horizon"] for payload in payloads] == ["5d", "20d"]
    for payload in payloads:
        target = payload["forecast_target"]
        assert target["experiment_sample"] is False
        assert target["shared_gate_bits"] == "unavailable_at_this_call_site"
        assert target["collection_epoch_id"] is None
        assert target["pre_arming_witness"] is True
        assert target["spec_sha256"] == PINNED_SPEC_SHA256_V2
        assert target["policy_projection_sha256"] == PINNED_POLICY_PROJECTION_SHA256_V2
        assert payload["session_label"] == EXPERIMENT_ID_V2
        assert payload["correlation_id"].endswith(f":{payload['horizon']}")

    supplied = build_shadow_buy_forecasts(_evaluation(), created_by="reviewer")
    assert all(row["forecast_target"]["experiment_sample"] is True for row in supplied)
    assert all(
        row["forecast_target"]["shared_gate_bits"] == "supplied" for row in supplied
    )


def test_v2_forecast_tags_distinguish_a_and_b_from_b_only() -> None:
    a_and_b_target = build_shadow_buy_forecasts(_evaluation(), created_by="reviewer")[
        0
    ]["forecast_target"]
    b_only_target = build_shadow_buy_forecasts(
        _evaluation(support_strength=_B_MIN), created_by="reviewer"
    )[0]["forecast_target"]

    assert a_and_b_target["evaluated_cohort"] == "a_and_b"
    assert a_and_b_target["shadow_buy"] is False
    assert b_only_target["evaluated_cohort"] == "b_only"
    assert b_only_target["shadow_buy"] is True
    assert (
        a_and_b_target["evaluated_cohort"],
        a_and_b_target["shadow_buy"],
    ) != (
        b_only_target["evaluated_cohort"],
        b_only_target["shadow_buy"],
    )


def test_v2_forecast_tag_records_neither_verdict_facts() -> None:
    target = build_shadow_buy_forecasts(
        _evaluation(other_gate_bits={}), created_by="reviewer"
    )[0]["forecast_target"]

    assert target["evaluated_cohort"] == "neither"
    assert target["shadow_buy"] is False
    assert target["variant_a_passed"] is False
    assert target["variant_b_passed"] is False


def test_v2_forecast_tag_records_b_only_verdict_facts() -> None:
    target = build_shadow_buy_forecasts(
        _evaluation(support_strength=_B_MIN), created_by="reviewer"
    )[0]["forecast_target"]

    assert target["evaluated_cohort"] == "b_only"
    assert target["shadow_buy"] is True
    assert target["variant_a_passed"] is False
    assert target["variant_b_passed"] is True


def test_v2_forecast_tag_preserves_sealed_stream_cohort() -> None:
    for evaluation in (
        _evaluation(),
        _evaluation(support_strength=_B_MIN),
        _evaluation(other_gate_bits={}),
    ):
        target = build_shadow_buy_forecasts(evaluation, created_by="reviewer")[0][
            "forecast_target"
        ]
        assert target["variant"] == "B"
        assert target["cohort"] == PRE_REGISTRATION_V2["forecast_tagging"]["cohort"]


def test_v1_and_v2_seals_stay_pinned() -> None:
    assert spec_sha256() == PINNED_SPEC_SHA256
    assert policy_projection_sha256() == PINNED_POLICY_PROJECTION_SHA256
    assert spec_sha256_v2() == PINNED_SPEC_SHA256_V2
    assert policy_projection_sha256_v2() == PINNED_POLICY_PROJECTION_SHA256_V2


@pytest.mark.asyncio
async def test_fanout_witness_recorder_is_fail_open_and_keeps_result_unchanged() -> (
    None
):
    result = _fanout_result()
    snapshot = copy.deepcopy(result)

    async def broken_save(**_kwargs: Any) -> None:
        raise RuntimeError("storage unavailable")

    await maybe_record_buy_gate_ab_shadow(
        result,
        enabled=True,
        save=broken_save,
        now=_AS_OF,
    )
    assert result == snapshot


@pytest.mark.asyncio
async def test_fanout_witness_recorder_defaults_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BUY_GATE_AB_SHADOW_RECORD_ENABLED", raising=False)
    saved: list[dict[str, Any]] = []

    async def save(**kwargs: Any) -> None:
        saved.append(kwargs)

    await maybe_record_buy_gate_ab_shadow(_fanout_result(), save=save, now=_AS_OF)
    assert saved == []


@pytest.mark.asyncio
async def test_fanout_witness_records_neither_cohort_as_non_sample() -> None:
    saved: list[dict[str, Any]] = []

    async def save(**kwargs: Any) -> None:
        saved.append(kwargs)

    await maybe_record_buy_gate_ab_shadow(
        _fanout_result(),
        enabled=True,
        save=save,
        now=_AS_OF,
    )
    assert len(saved) == 2
    for payload in saved:
        target = payload["forecast_target"]
        assert target["experiment_sample"] is False
        assert target["shared_gate_bits"] == "unavailable_at_this_call_site"
        assert target["collection_epoch_id"] is None
        assert target["pre_arming_witness"] is True


@pytest.mark.asyncio
async def test_fanout_witness_recorder_stops_at_ten_candidates() -> None:
    saved: list[dict[str, Any]] = []

    async def save(**kwargs: Any) -> None:
        saved.append(kwargs)

    await maybe_record_buy_gate_ab_shadow(
        _fanout_result(count=MAX_FANOUT_WITNESS_CANDIDATES + 1),
        enabled=True,
        save=save,
        now=_AS_OF,
    )
    assert len(saved) == MAX_FANOUT_WITNESS_CANDIDATES * 2


@pytest.mark.asyncio
async def test_fanout_witness_recorder_logs_evaluation_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    saved: list[dict[str, Any]] = []
    result = _fanout_result()
    result["candidates"].append({"symbol": "invalid", "funnel": {}})

    async def save(**kwargs: Any) -> None:
        saved.append(kwargs)

    with caplog.at_level(
        logging.INFO,
        logger="app.services.buy_gate_ab_shadow_recorder",
    ):
        await maybe_record_buy_gate_ab_shadow(
            result,
            enabled=True,
            save=save,
            now=_AS_OF,
        )

    completed = [
        record
        for record in caplog.records
        if record.message == "buy-gate A/B v2 witness recorder completed"
    ]
    assert len(saved) == 2
    assert len(completed) == 1
    assert completed[0].candidates_seen == 2
    assert completed[0].evaluated == 1
    assert completed[0].skipped == 1
    assert completed[0].rows_saved == 2


def test_v2_mcp_tool_returns_witness_kwargs_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.mcp_server.tooling.forecast_tools as forecast_tools

    async def forbidden_save(**_kwargs: Any) -> None:
        raise AssertionError("evaluate_buy_gate_ab_shadow_v2 must not write")

    monkeypatch.setattr(forecast_tools, "forecast_save", forbidden_save)
    supplied = evaluate_buy_gate_ab_shadow_v2_impl(
        [_row()],
        evaluation_as_of=_AS_OF.isoformat(),
        created_by="reviewer",
    )
    assert supplied["success"] is True
    assert supplied["pre_arming_witness"] is True
    assert supplied["collection_epoch_id"] is None
    assert supplied["spec_sha256"] == PINNED_SPEC_SHA256_V2
    assert supplied["policy_projection_sha256"] == PINNED_POLICY_PROJECTION_SHA256_V2
    assert supplied["forbidden"] == list(FORBIDDEN_V2)
    assert len(supplied["shadow_buy_forecasts"]) == 2
    assert all(
        row["forecast_target"]["experiment_sample"] is True
        for row in supplied["shadow_buy_forecasts"]
    )

    missing = evaluate_buy_gate_ab_shadow_v2_impl(
        [_row(other_gate_bits={})],
        evaluation_as_of=_AS_OF.isoformat(),
        created_by="reviewer",
    )
    assert missing["success"] is True
    assert missing["candidates"][0]["cohort"] == "neither"
    assert all(
        row["forecast_target"]["experiment_sample"] is False
        for row in missing["shadow_buy_forecasts"]
    )


def test_v2_mcp_registration_is_explicitly_read_only() -> None:
    class _FakeMCP:
        def __init__(self) -> None:
            self.descriptions: dict[str, str] = {}

        def tool(self, *, name: str, description: str, **_options: Any) -> Any:
            def decorate(function: Any) -> Any:
                self.descriptions[name] = description
                return function

            return decorate

    mcp = _FakeMCP()
    register_buy_gate_ab_shadow_v2_tools(mcp)  # type: ignore[arg-type]
    description = mcp.descriptions["evaluate_buy_gate_ab_shadow_v2"].lower()
    assert "forecast_save kwargs" in description
    assert "never creates a proposal, order, watch, collection" in description
    assert "evaluate_buy_gate_ab_shadow_v2" in ANALYSIS_READONLY_TOOL_NAMES
    assert "evaluate_buy_gate_ab_shadow_v2" in READ_ONLY_ADVISORY_TOOLS
    assert "evaluate_buy_gate_ab_shadow_v2" not in MUTATION_TOOLS
