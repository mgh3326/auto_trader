"""ROB-1351 v2 server-boundary integrity and live-policy alignment tests."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.buy_gate_ab_shadow.forecast_guard_v2 as forecast_guard_v2
import app.services.buy_gate_ab_shadow.policy_alignment_v2 as policy_alignment_v2
from app.models.invalid_sample_eligibility import SampleEligibilityDecision
from app.services import trading_policy_service
from app.services.buy_gate_ab_shadow.evaluate import (
    CandidateEvidence as V1CandidateEvidence,
)
from app.services.buy_gate_ab_shadow.evaluate import evaluate_candidate as evaluate_v1
from app.services.buy_gate_ab_shadow.evaluate_v2 import (
    CandidateEvidence,
    evaluate_candidate,
)
from app.services.buy_gate_ab_shadow.forecast_tag import (
    build_shadow_buy_forecasts as build_v1_shadow_buy_forecasts,
)
from app.services.buy_gate_ab_shadow.forecast_tag_v2 import (
    build_shadow_buy_forecasts,
)
from app.services.buy_gate_ab_shadow.spec_v2 import (
    PINNED_POLICY_PROJECTION_SHA256_V2,
    PINNED_SPEC_SHA256_V2,
    POLICY_PROJECTION_V2,
    PRE_REGISTRATION_V2,
)
from app.services.invalid_sample_eligibility.contract import (
    CalibrationEligibility,
    EligibilitySubject,
    EligibilitySubjectKind,
    TradePerformanceEligibility,
)
from app.services.invalid_sample_eligibility.service import (
    InvalidSampleEligibilityService,
)
from app.services.trade_journal import forecast_service

pytestmark = pytest.mark.integration

_AS_OF = datetime(2026, 9, 7, 6, 30, tzinfo=UTC)
_MISSING = object()


def _evaluation(
    *,
    support_strength: str = "moderate",
    include_shared_bits: bool = True,
):
    raw: dict[str, Any] = {
        "symbol": "005930",
        "market": "kr",
        "current_price": "70000",
        "support_strength": support_strength,
        "support_distance_pct": "4",
        "rsi": "40",
        "honest_upside_pct": "45",
    }
    if include_shared_bits:
        raw["other_gate_bits"] = {
            "liquid_midcap": True,
            "concentration": True,
            "overhang": True,
        }
    return evaluate_candidate(
        CandidateEvidence.from_mapping(raw),
        evaluation_as_of=_AS_OF,
    )


def _v2_payload(
    *,
    support_strength: str = "moderate",
    include_shared_bits: bool = True,
) -> dict[str, Any]:
    """Use the actual v2 builder output as every normal forecast fixture."""

    return deepcopy(
        build_shadow_buy_forecasts(
            _evaluation(
                support_strength=support_strength,
                include_shared_bits=include_shared_bits,
            ),
            created_by="rob-1351-server-guard-test",
        )[0]
    )


@pytest.fixture
def v2_payload() -> dict[str, Any]:
    return _v2_payload()


def _changed_target(
    payload: dict[str, Any], *, field: str, value: object
) -> dict[str, Any]:
    changed = deepcopy(payload)
    target = changed["forecast_target"]
    assert isinstance(target, dict)
    if value is _MISSING:
        target.pop(field)
    else:
        target[field] = value
    return changed


def _policy_copy_from_actual_yaml():
    """A full, deep-copied policy document rather than a hand-made mini dict."""

    return deepcopy(trading_policy_service.load_trading_policy())


def _patch_loaded_policy(
    monkeypatch: pytest.MonkeyPatch,
    document: Any,
) -> None:
    monkeypatch.setattr(
        trading_policy_service,
        "_load",
        lambda: (document, "test"),
    )


@pytest.mark.asyncio
async def test_save_forecast_validates_all_v2_evaluated_cohorts(
    db_session: AsyncSession,
) -> None:
    """The server sees v2 by experiment_id even when shadow_buy is false."""

    cases = (
        ("a_and_b", False, True, "supplied", _v2_payload()),
        ("b_only", True, True, "supplied", _v2_payload(support_strength="weak")),
        (
            "neither",
            False,
            False,
            "unavailable_at_this_call_site",
            _v2_payload(include_shared_bits=False),
        ),
    )
    for (
        expected_cohort,
        expected_shadow_buy,
        expected_sample,
        expected_shared_gate_bits,
        payload,
    ) in cases:
        action, row = await forecast_service.save_forecast(db_session, **payload)
        assert action in {"created", "updated"}
        target = row.forecast_target
        assert target["evaluated_cohort"] == expected_cohort
        assert target["shadow_buy"] is expected_shadow_buy
        assert target["cohort"] == "shadow_buy"
        assert target["experiment_sample"] is expected_sample
        assert target["shared_gate_bits"] == expected_shared_gate_bits


@pytest.mark.asyncio
async def test_save_forecast_rejects_manual_v2_bypass_before_persistence(
    db_session: AsyncSession,
) -> None:
    """A caller cannot bypass the builder by crafting a target dict directly."""

    snapshot = {
        "symbol": "005930",
        "market": "kr",
        "current_price": "70000",
        "support_strength": "moderate",
        "support_distance_pct": "4",
        "rsi": "40",
        "honest_upside_pct": "45",
        "other_gate_bits": {
            "liquid_midcap": False,
            "concentration": False,
            "overhang": False,
        },
        "shared_gate_bits_supplied": False,
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manual_payload: dict[str, Any] = {
        "created_by": "manual-v2-server-guard-test",
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "forecast_target": {
            "kind": "price_target",
            "direction": "at_or_above",
            "target_price": 70000.0,
            "outcome_rule_version": "window-touch-v1-high-gte-low-lte",
            "experiment_id": "rob-1351-buy-gate-moderate-live",
            "variant": "B",
            "cohort": "shadow_buy",
            "evaluated_cohort": "neither",
            "shadow_buy": False,
            "promote": False,
            "live_gate_impact": False,
            "calibration_eligibility": "calibration_exclude",
            "trade_performance_eligibility": "trade_performance_exclude",
            "spec_sha256": PINNED_SPEC_SHA256_V2,
            "policy_projection_sha256": PINNED_POLICY_PROJECTION_SHA256_V2,
            "collection_epoch_id": None,
            "pre_arming_witness": True,
            "experiment_sample": False,
            "shared_gate_bits": "unavailable_at_this_call_site",
            "evaluation_as_of": _AS_OF.isoformat(),
            "session_date": _AS_OF.date().isoformat(),
            "entry_price": "70000",
            "input_snapshot": snapshot,
            "input_snapshot_sha256": digest,
            "scoring_authority": "rob-1351-buy-gate-moderate-live.scoring",
        },
        "probability": 0.5,
        "review_date": "2026-09-14",
    }

    action, _row = await forecast_service.save_forecast(db_session, **manual_payload)
    assert action == "created"

    bypass = _changed_target(manual_payload, field="promote", value=True)
    with pytest.raises(forecast_service.ForecastValidationError, match="promote"):
        await forecast_service.save_forecast(db_session, **bypass)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "value"),
    [
        ("true", True),
        ("missing", _MISSING),
        ("falsy_int", 0),
    ],
)
async def test_v2_rejects_invalid_promote_exactly(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
    case: str,
    value: object,
) -> None:
    payload = _changed_target(v2_payload, field="promote", value=value)
    with pytest.raises(forecast_service.ForecastValidationError, match="promote"):
        await forecast_service.save_forecast(db_session, **payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "value"),
    [
        ("missing", _MISSING),
        ("include", "calibration_include"),
        ("none", None),
    ],
)
async def test_v2_rejects_invalid_calibration_eligibility(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
    case: str,
    value: object,
) -> None:
    payload = _changed_target(
        v2_payload,
        field="calibration_eligibility",
        value=value,
    )
    with pytest.raises(
        forecast_service.ForecastValidationError,
        match="calibration_eligibility",
    ):
        await forecast_service.save_forecast(db_session, **payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "value"),
    [
        ("missing", _MISSING),
        ("include", "trade_performance_include"),
    ],
)
async def test_v2_rejects_invalid_trade_performance_eligibility(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
    case: str,
    value: object,
) -> None:
    payload = _changed_target(
        v2_payload,
        field="trade_performance_eligibility",
        value=value,
    )
    with pytest.raises(
        forecast_service.ForecastValidationError,
        match="trade_performance_eligibility",
    ):
        await forecast_service.save_forecast(db_session, **payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cohort", "not_shadow_buy", "cohort"),
        ("live_gate_impact", True, "live_gate_impact"),
        ("live_gate_impact", 0, "live_gate_impact"),
        ("variant", "A", "variant"),
    ],
)
async def test_v2_rejects_wrong_sealed_stream_fields(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _changed_target(v2_payload, field=field, value=value)
    with pytest.raises(forecast_service.ForecastValidationError, match=message):
        await forecast_service.save_forecast(db_session, **payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spec_sha256", PINNED_SPEC_SHA256_V2[:-1] + "0"),
        ("spec_sha256", _MISSING),
        ("spec_sha256", 123),
        (
            "policy_projection_sha256",
            PINNED_POLICY_PROJECTION_SHA256_V2[:-1] + "0",
        ),
        ("policy_projection_sha256", _MISSING),
        ("policy_projection_sha256", 123),
    ],
)
async def test_v2_rejects_sealed_digest_mismatch_or_missing(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
    field: str,
    value: object,
) -> None:
    payload = _changed_target(v2_payload, field=field, value=value)
    with pytest.raises(forecast_service.ForecastValidationError, match=field):
        await forecast_service.save_forecast(db_session, **payload)


def test_v2_guard_calls_existing_seal_and_does_not_mutate_inputs(
    monkeypatch: pytest.MonkeyPatch,
    v2_payload: dict[str, Any],
) -> None:
    target = v2_payload["forecast_target"]
    assert isinstance(target, dict)
    before_target = deepcopy(target)
    before_projection = deepcopy(POLICY_PROJECTION_V2)
    before_registration = deepcopy(PRE_REGISTRATION_V2)
    calls: list[None] = []
    original_assert_v2_seal = forecast_guard_v2.assert_v2_seal

    def checked_assert_v2_seal() -> None:
        calls.append(None)
        original_assert_v2_seal()

    monkeypatch.setattr(forecast_guard_v2, "assert_v2_seal", checked_assert_v2_seal)
    forecast_guard_v2.validate_v2_forecast_target(
        target,
        instrument_type="equity_kr",
    )

    assert calls == [None]
    assert target == before_target
    assert POLICY_PROJECTION_V2 == before_projection
    assert PRE_REGISTRATION_V2 == before_registration


@pytest.mark.asyncio
async def test_v2_accepts_input_snapshot_with_different_key_insertion_order(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
) -> None:
    payload = deepcopy(v2_payload)
    target = payload["forecast_target"]
    assert isinstance(target, dict)
    snapshot = target["input_snapshot"]
    assert isinstance(snapshot, dict)
    reordered = dict(reversed(list(snapshot.items())))
    other_gate_bits = reordered.get("other_gate_bits")
    if isinstance(other_gate_bits, dict):
        reordered["other_gate_bits"] = dict(reversed(list(other_gate_bits.items())))
    target["input_snapshot"] = reordered

    action, _row = await forecast_service.save_forecast(db_session, **payload)
    assert action == "created"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "snapshot_value", "message"),
    [
        ("missing", _MISSING, "input_snapshot"),
        ("not_dict", [], "input_snapshot"),
        ("changed_value", {"rsi": "39"}, "input_snapshot_sha256"),
    ],
)
async def test_v2_rejects_input_snapshot_bypass(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
    case: str,
    snapshot_value: object,
    message: str,
) -> None:
    payload = deepcopy(v2_payload)
    target = payload["forecast_target"]
    assert isinstance(target, dict)
    if snapshot_value is _MISSING:
        target.pop("input_snapshot")
    elif case == "changed_value":
        snapshot = target["input_snapshot"]
        assert isinstance(snapshot, dict)
        changed_snapshot = deepcopy(snapshot)
        changed_snapshot.update(snapshot_value)
        target["input_snapshot"] = changed_snapshot
    else:
        target["input_snapshot"] = snapshot_value

    with pytest.raises(forecast_service.ForecastValidationError, match=message):
        await forecast_service.save_forecast(db_session, **payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("collection_epoch_id", "forged-epoch", "collection_epoch_id"),
        ("pre_arming_witness", False, "pre_arming_witness"),
        ("pre_arming_witness", 1, "pre_arming_witness"),
    ],
)
async def test_v2_rejects_epoch_arm_forgery(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
    field: str,
    value: object,
    message: str,
) -> None:
    payload = _changed_target(v2_payload, field=field, value=value)
    with pytest.raises(forecast_service.ForecastValidationError, match=message):
        await forecast_service.save_forecast(db_session, **payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1, "true", None])
async def test_v2_requires_exact_bool_experiment_sample(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
    value: object,
) -> None:
    payload = _changed_target(v2_payload, field="experiment_sample", value=value)
    with pytest.raises(
        forecast_service.ForecastValidationError,
        match="experiment_sample",
    ):
        await forecast_service.save_forecast(db_session, **payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("experiment_sample", "shared_gate_bits", "message"),
    [
        (True, "unavailable_at_this_call_site", "disagree"),
        (False, "supplied", "disagree"),
        (False, "not_a_v2_value", "shared_gate_bits"),
        (False, 1, "shared_gate_bits"),
    ],
)
async def test_v2_rejects_sample_provenance_forgery(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
    experiment_sample: bool,
    shared_gate_bits: object,
    message: str,
) -> None:
    payload = deepcopy(v2_payload)
    target = payload["forecast_target"]
    assert isinstance(target, dict)
    target["experiment_sample"] = experiment_sample
    target["shared_gate_bits"] = shared_gate_bits
    with pytest.raises(forecast_service.ForecastValidationError, match=message):
        await forecast_service.save_forecast(db_session, **payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evaluation_as_of", ""),
        ("session_date", ""),
        ("entry_price", ""),
        ("scoring_authority", ""),
        ("input_snapshot_sha256", ""),
        ("input_snapshot_sha256", _MISSING),
        ("entry_price", 70000),
    ],
)
async def test_v2_requires_nonempty_string_contract_fields(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
    field: str,
    value: object,
) -> None:
    payload = _changed_target(v2_payload, field=field, value=value)
    with pytest.raises(forecast_service.ForecastValidationError, match=field):
        await forecast_service.save_forecast(db_session, **payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("evaluation_as_of", "2026-09-07T06:30:00", "timezone-aware"),
        ("evaluation_as_of", "not-a-date", "ISO-8601"),
        ("session_date", "2026-09-08", "session_date"),
    ],
)
async def test_v2_validates_evaluation_timestamp_and_session_date(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
    field: str,
    value: str,
    message: str,
) -> None:
    payload = _changed_target(v2_payload, field=field, value=value)
    with pytest.raises(forecast_service.ForecastValidationError, match=message):
        await forecast_service.save_forecast(db_session, **payload)


@pytest.mark.asyncio
async def test_v2_rejects_non_equity_instrument(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
) -> None:
    payload = deepcopy(v2_payload)
    payload["instrument_type"] = "crypto"
    with pytest.raises(forecast_service.ForecastValidationError, match="equity_kr"):
        await forecast_service.save_forecast(db_session, **payload)


@pytest.mark.asyncio
async def test_v2_persists_real_eligibility_exclusions_idempotently(
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
) -> None:
    action, row = await forecast_service.save_forecast(db_session, **v2_payload)
    assert action == "created"
    await db_session.commit()

    subject = EligibilitySubject(
        kind=EligibilitySubjectKind.FORECAST,
        ref=str(row.forecast_id),
    )
    service = InvalidSampleEligibilityService(db_session)
    decision = await service.get_decision(subject)
    assert decision.calibration_eligibility is CalibrationEligibility.EXCLUDE
    assert decision.trade_performance_eligibility is TradePerformanceEligibility.EXCLUDE

    repeated_action, repeated = await forecast_service.save_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        **v2_payload,
    )
    assert repeated_action == "updated"
    assert repeated.forecast_id == row.forecast_id
    await db_session.commit()

    decisions = (
        (
            await db_session.execute(
                select(SampleEligibilityDecision).where(
                    SampleEligibilityDecision.subject_kind == "forecast",
                    SampleEligibilityDecision.subject_ref == str(row.forecast_id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(decisions) == 1


@pytest.mark.asyncio
async def test_v1_shadow_and_general_forecast_paths_remain_unchanged(
    db_session: AsyncSession,
) -> None:
    v1_evaluation = evaluate_v1(
        V1CandidateEvidence.from_mapping(
            {
                "symbol": "000660",
                "market": "kr",
                "current_price": "200000",
                "support_strength": "moderate",
                "support_distance_pct": "4",
                "rsi": "40",
                "honest_upside_pct": "45",
                "other_gate_bits": {
                    "liquid_midcap": True,
                    "concentration": True,
                    "overhang": True,
                },
            }
        ),
        evaluation_as_of=datetime(2026, 8, 31, 0, 30, tzinfo=UTC),
    )
    v1_payload = build_v1_shadow_buy_forecasts(v1_evaluation, created_by="v1-test")[0]
    action, _row = await forecast_service.save_forecast(db_session, **v1_payload)
    assert action == "created"

    v1_tampered = _changed_target(
        v1_payload,
        field="policy_projection_sha256",
        value="0" * 64,
    )
    with pytest.raises(
        forecast_service.ForecastValidationError,
        match="mismatched policy_projection_sha256",
    ):
        await forecast_service.save_forecast(db_session, **v1_tampered)

    action, _row = await forecast_service.save_forecast(
        db_session,
        created_by="ordinary-forecast-test",
        symbol="AAPL",
        instrument_type="equity_us",
        forecast_target={
            "kind": "price_target",
            "direction": "at_or_above",
            "target_price": 250.0,
            "outcome_rule_version": "window-touch-v1-high-gte-low-lte",
        },
        probability=0.5,
        review_date="2026-09-14",
    )
    assert action == "created"


def test_current_real_policy_matches_sealed_v2_projection() -> None:
    policy = trading_policy_service.load_trading_policy()
    assert policy.version == "2026-09-08.1"
    policy_alignment_v2.assert_v2_policy_alignment()


def test_policy_alignment_uses_loaded_document_not_projection_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _policy_copy_from_actual_yaml()
    document.thresholds["screen.rsi_max"].value = 50
    _patch_loaded_policy(monkeypatch, document)

    with pytest.raises(
        policy_alignment_v2.PolicyAlignmentV2Error,
        match=r"sealed .* does not match .* screen\.rsi_max",
    ):
        policy_alignment_v2.assert_v2_policy_alignment()


@pytest.mark.asyncio
async def test_save_forecast_rejects_live_policy_drift(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    v2_payload: dict[str, Any],
) -> None:
    document = _policy_copy_from_actual_yaml()
    document.thresholds["screen.rsi_max"].value = 50
    _patch_loaded_policy(monkeypatch, document)

    with pytest.raises(
        forecast_service.ForecastValidationError,
        match=r"sealed .* does not match .* screen\.rsi_max",
    ):
        await forecast_service.save_forecast(db_session, **v2_payload)


@pytest.mark.parametrize(
    ("policy_key", "changed_value"),
    [
        ("screen.rsi_max", 50),
        ("screen.rsi_max", 45.5),
        ("screen.support_within_pct", 10),
        ("screen.upside_min_pct", 30),
        ("screen.support_strength_min", "strong"),
    ],
)
def test_policy_alignment_rejects_each_related_live_drift(
    monkeypatch: pytest.MonkeyPatch,
    policy_key: str,
    changed_value: object,
) -> None:
    document = _policy_copy_from_actual_yaml()
    document.thresholds[policy_key].value = changed_value
    _patch_loaded_policy(monkeypatch, document)

    with pytest.raises(
        policy_alignment_v2.PolicyAlignmentV2Error,
        match=rf"sealed .* does not match .* {re.escape(policy_key)}",
    ):
        policy_alignment_v2.assert_v2_policy_alignment()


def test_policy_alignment_rejects_missing_related_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _policy_copy_from_actual_yaml()
    del document.thresholds["screen.rsi_max"]
    _patch_loaded_policy(monkeypatch, document)

    with pytest.raises(
        policy_alignment_v2.PolicyAlignmentV2Error,
        match=r"loaded trading policy is missing screen\.rsi_max",
    ):
        policy_alignment_v2.assert_v2_policy_alignment()


@pytest.mark.parametrize("malformed_value", [{"value": 45}, None, "forty-five"])
def test_policy_alignment_rejects_malformed_numeric_live_value(
    monkeypatch: pytest.MonkeyPatch,
    malformed_value: object,
) -> None:
    document = _policy_copy_from_actual_yaml()
    document.thresholds["screen.rsi_max"].value = malformed_value
    _patch_loaded_policy(monkeypatch, document)

    with pytest.raises(
        policy_alignment_v2.PolicyAlignmentV2Error,
        match=r"loaded kr screen\.rsi_max must be a finite number",
    ):
        policy_alignment_v2.assert_v2_policy_alignment()


def test_policy_alignment_applies_market_override_effective_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _policy_copy_from_actual_yaml()
    document.market_overrides["kr"]["screen.rsi_max"] = 50
    _patch_loaded_policy(monkeypatch, document)

    with pytest.raises(
        policy_alignment_v2.PolicyAlignmentV2Error,
        match=r"sealed .* does not match .* screen\.rsi_max",
    ):
        policy_alignment_v2.assert_v2_policy_alignment()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: setattr(document.cash_proxy, "semantics", "changed only"),
        lambda document: setattr(
            document.thresholds["screen.independent_support_source_count_min"],
            "value",
            3,
        ),
        lambda document: document.thresholds.pop(
            "screen.independent_support_source_count_min"
        ),
        lambda document: setattr(document, "version", "different-version"),
        lambda document: setattr(document, "source", "different-source"),
    ],
    ids=(
        "cash_proxy",
        "independent_support_value",
        "independent_support_deleted",
        "version",
        "source",
    ),
)
def test_policy_alignment_allows_unrelated_real_policy_changes(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> None:
    document = _policy_copy_from_actual_yaml()
    mutate(document)
    _patch_loaded_policy(monkeypatch, document)

    policy_alignment_v2.assert_v2_policy_alignment()


def test_policy_alignment_normalizes_equivalent_live_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _policy_copy_from_actual_yaml()
    document.thresholds["screen.rsi_max"].value = "45"
    document.thresholds["screen.support_strength_min"].value = " MODERATE "
    _patch_loaded_policy(monkeypatch, document)

    policy_alignment_v2.assert_v2_policy_alignment()


def test_all_sealed_projection_leaves_are_explicitly_classified() -> None:
    leaves = policy_alignment_v2.projection_leaf_paths_v2()
    mapped = set(policy_alignment_v2.SEALED_TO_LIVE_POLICY_KEYS_V2)
    non_live = set(policy_alignment_v2.SEALED_NO_LIVE_POLICY_REASON_V2)

    assert leaves == mapped | non_live
    assert not mapped & non_live
    policy_alignment_v2.assert_v2_policy_mapping_coverage()


def test_v2_guard_functions_have_no_mutable_defaults() -> None:
    for function in (
        forecast_guard_v2.is_rob1351_v2_target,
        forecast_guard_v2.validate_v2_forecast_target,
        policy_alignment_v2.projection_leaf_paths_v2,
        policy_alignment_v2.assert_v2_policy_mapping_coverage,
        policy_alignment_v2.assert_v2_policy_alignment,
    ):
        for parameter in inspect.signature(function).parameters.values():
            assert not isinstance(parameter.default, dict | list | set)
