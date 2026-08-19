from __future__ import annotations

import ast
from dataclasses import fields, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.schemas.execution_contracts import LaneStatus, SchedulerOwner
from app.services import mock_lane_registry as registry
from app.services.mock_integration import lineage
from app.services.mock_integration.lineage import (
    DecisionIntentDraft,
    ExecutionPlanDraft,
    LineageEnvelope,
    MockLineageFactory,
    OrderAttemptDraft,
)


def _by_id() -> dict[str, registry.LaneRegistryEntry]:
    return {entry.lane_id: entry for entry in registry.CANONICAL_LANE_REGISTRY}


def _issue_codes(exc: registry.RegistryStartupError) -> set[str]:
    return {issue.code for issue in exc.issues}


def test_registry_stays_free_of_transport_signing_and_secret_value_loading() -> None:
    source_path = Path(registry.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden_modules = {
        "aiohttp",
        "dotenv",
        "hashlib",
        "hmac",
        "http.client",
        "httpx",
        "os",
        "pydantic_settings",
        "requests",
        "socket",
        "urllib.request",
        "websockets",
    }
    imported_modules: set[str] = set()
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)

    assert imported_modules.isdisjoint(forbidden_modules)
    assert called_names.isdisjoint({"open"})


def _lineage_envelope(
    lane_id: str,
    *,
    intent_overrides: dict[str, object] | None = None,
    plan_overrides: dict[str, object] | None = None,
) -> tuple[MockLineageFactory, LineageEnvelope]:
    entry = _by_id()[lane_id]
    intent_values: dict[str, object] = {
        "policy_version": "test-policy-v1",
        "policy_version_hash": "test-policy-hash-v1",
        "decision_timestamp": datetime(2026, 8, 16, 0, 0, tzinfo=UTC),
        "market_data_cutoff": datetime(2026, 8, 15, 23, 59, tzinfo=UTC),
        "symbol": "BRK.B",
        "side": "buy",
        "target_notional": Decimal("1"),
        "target_notional_currency": entry.quote_currency,
        "limit_policy": {"order_type": "limit"},
        "expiry_policy": {"kind": "day"},
        "rationale": "test-only registry binding",
    }
    intent_values.update(intent_overrides or {})
    plan_values: dict[str, object] = {
        "lane_id": lane_id,
        "broker": entry.broker,
        "account_profile": entry.account_profile,
        "account_mode": entry.account_mode.value,
        "normalized_symbol": "BRK.B",
        "quantity": Decimal("1"),
        "limit_price": Decimal("1"),
        "quote_currency": entry.quote_currency,
        "tick_rounding": {"increment": "0.01"},
        "session": "regular",
        "time_in_force": "day",
        "min_order_validation": {"quote_required": True},
        "risk_caps": {"max_notional": "1"},
    }
    plan_values.update(plan_overrides or {})
    factory = MockLineageFactory()
    intent = factory.create_decision_intent(DecisionIntentDraft(**intent_values))
    return factory, factory.create_plan_envelope(
        intent,
        ExecutionPlanDraft(**plan_values),
    )


def _registry_replacing(
    replacement: registry.LaneRegistryEntry,
) -> tuple[registry.LaneRegistryEntry, ...]:
    return tuple(
        replacement if entry.lane_id == replacement.lane_id else entry
        for entry in registry.CANONICAL_LANE_REGISTRY
    )


def _policy_bound_entry(
    envelope: LineageEnvelope,
    lane_id: str,
) -> registry.LaneRegistryEntry:
    return replace(
        _by_id()[lane_id],
        policy_binding=registry.PolicyBinding(
            envelope.decision_intent.policy_version,
            envelope.decision_intent.policy_version_hash,
        ),
        missing_bindings=tuple(
            binding
            for binding in _by_id()[lane_id].missing_bindings
            if binding is not registry.MissingBinding.POLICY
        ),
    )


def _fully_bound_entry(
    envelope: LineageEnvelope,
    lane_id: str,
    **overrides: object,
) -> registry.LaneRegistryEntry:
    values: dict[str, object] = {
        "lane_status": LaneStatus.AUTO_ENABLED,
        "activation_status": registry.ActivationStatus.ENABLED,
        "activation_reason": "test-only fully bound fixture",
        "policy_binding": registry.PolicyBinding(
            envelope.decision_intent.policy_version,
            envelope.decision_intent.policy_version_hash,
        ),
        "execution_mode": "test-only-bounded",
        "scheduler_owner": SchedulerOwner.MANUAL,
        "timing_owner": "test-only-timing",
        "writer": True,
        "auto_order_enabled": True,
        "max_order_notional": Decimal("1"),
        "max_orders_per_session": 1,
        "max_open_orders": 1,
        "allowed_order_types": ("limit",),
        "allowed_time_in_force": ("day",),
        "reconcile_required": True,
        "physical_account_id": f"opaque-test-{lane_id}",
        "identity_status": "KNOWN",
        "fingerprint_evidence_ref": "test-only-fingerprint",
        "canary_binding": "test-only-bounded-canary",
        "missing_bindings": (),
    }
    values.update(overrides)
    return replace(_by_id()[lane_id], **values)


async def _assert_both_boundaries_reject(
    envelope: LineageEnvelope,
    snapshot: tuple[registry.LaneRegistryEntry, ...],
    *,
    reason: str,
    endpoint_url: str,
    credential_namespace: str,
    recurring_requested: bool = False,
    bounded_canary: bool = False,
) -> None:
    broker_calls = 0
    factory_calls = 0

    async def broker_io() -> None:
        nonlocal broker_calls
        broker_calls += 1

    def factory() -> object:
        nonlocal factory_calls
        factory_calls += 1
        return object()

    with pytest.raises((registry.LaneGuardError, registry.RegistryStartupError)) as io:
        await registry.guarded_broker_io(
            envelope,
            endpoint_url=endpoint_url,
            credential_namespace=credential_namespace,
            broker_io=broker_io,
            registry=snapshot,
            recurring_requested=recurring_requested,
            bounded_canary=bounded_canary,
        )
    with pytest.raises(
        (registry.LaneGuardError, registry.RegistryStartupError)
    ) as client:
        registry.guarded_client_factory(
            envelope,
            endpoint_url=endpoint_url,
            credential_namespace=credential_namespace,
            factory=factory,
            registry=snapshot,
            recurring_requested=recurring_requested,
            bounded_canary=bounded_canary,
        )

    for error in (io.value, client.value):
        if isinstance(error, registry.LaneGuardError):
            assert error.code == reason
        else:
            assert reason in _issue_codes(error)
    assert broker_calls == 0
    assert factory_calls == 0


def test_canonical_lane_ids_and_quote_currencies_are_exact() -> None:
    expected = (
        ("kr.kis.mock", "KRW"),
        ("kr.kiwoom.mock", "KRW"),
        ("us.kis.mock", "USD"),
        ("us.kiwoom.mock", "USD"),
        ("us.alpaca.paper.default", "USD"),
        ("us.alpaca.paper.lab", "USD"),
        ("crypto.binance.spot_demo.canonical", "USDT"),
        ("crypto.binance.spot_demo.b0x_sidecar", "USDT"),
        ("crypto.alpaca.paper.default", "USD"),
        ("crypto.alpaca.paper.clean", "USD"),
        ("crypto.upbit.shadow", "KRW"),
        ("crypto.binance.futures_demo", "USDT"),
    )

    assert (
        tuple(
            (entry.lane_id, entry.quote_currency)
            for entry in registry.CANONICAL_LANE_REGISTRY
        )
        == expected
    )
    assert registry.CANONICAL_LANE_IDS == tuple(lane_id for lane_id, _ in expected)
    assert tuple(registry.LANE_QUOTE_CURRENCIES.items()) == expected


def test_canonical_roles_and_hard_writer_literals_are_exact() -> None:
    rows = _by_id()

    assert rows["kr.kis.mock"].role is registry.RegistryRole.AUTO_MIRROR
    assert rows["kr.kiwoom.mock"].role is registry.RegistryRole.PRIMARY_AUTO
    assert rows["us.kis.mock"].role is registry.RegistryRole.AUTO_MIRROR
    assert rows["us.kiwoom.mock"].role is registry.RegistryRole.BROKER_REGRESSION
    assert rows["us.alpaca.paper.default"].role is registry.RegistryRole.PRIMARY_AUTO

    lab = rows["us.alpaca.paper.lab"]
    assert lab.role is None
    assert lab.role_pending_reason == "policy_absent"
    assert lab.role_on_policy_approval is registry.RegistryRole.AUTO_CHALLENGER

    canonical = rows["crypto.binance.spot_demo.canonical"]
    assert canonical.role is registry.RegistryRole.PRIMARY_AUTO

    sidecar = rows["crypto.binance.spot_demo.b0x_sidecar"]
    assert sidecar.role is registry.RegistryRole.SHADOW_ONLY
    assert sidecar.writer is False

    for lane_id in (
        "crypto.alpaca.paper.default",
        "crypto.alpaca.paper.clean",
    ):
        assert rows[lane_id].role is registry.RegistryRole.AUTO_MIRROR
        assert rows[lane_id].writer is False

    assert rows["crypto.upbit.shadow"].role is registry.RegistryRole.SHADOW_ONLY

    futures = rows["crypto.binance.futures_demo"]
    assert futures.role is None
    assert futures.writer is False
    assert futures.lane_status is LaneStatus.DISABLED_NO_STRATEGY


def test_activation_enum_is_exact_and_separate_from_lane_status() -> None:
    assert tuple(status.value for status in registry.ActivationStatus) == (
        "DISABLED",
        "BLOCKED",
        "READY",
        "ENABLED",
        "RUNTIME_ACCEPTANCE_PENDING",
        "READY_FOR_MOCK_DEPLOYMENT",
    )
    for entry in registry.CANONICAL_LANE_REGISTRY:
        assert isinstance(entry.lane_status, LaneStatus)
        assert isinstance(entry.activation_status, registry.ActivationStatus)
        assert type(entry.lane_status) is not type(entry.activation_status)


def test_amendment_rule_and_guard_literals_are_verbatim() -> None:
    assert dict(registry.ACTIVATION_TRANSITION_GUARDS) == {
        "G1": (
            "ENABLED 진입은 기존에 직접 증명된 cadence 보존 lane 에만. 신규 recurring\n"
            "       schedule 이 필요하면 진입 금지 → "
            "AUTO_READY_BLOCKED_BY_SCHEDULER (D4)"
        ),
        "G2": (
            "READY_FOR_MOCK_DEPLOYMENT 는 종착 상태. shared production release 또는\n"
            "       live restart 필요 시 여기서 정지, 자동 승격 경로 없음 "
            "(운영자 결정 6)"
        ),
        "G3": (
            "J8 canary 성공은 RUNTIME_ACCEPTANCE_PENDING → READY 까지만 이동시키며\n"
            "       ENABLED 로 자동 전이시키지 않는다 (D4)"
        ),
    }
    assert registry.UNKNOWN_IDENTITY_RULE == (
        "broker fingerprint 증거 부재 시 physical_account_id=null,\n"
        "  identity_status=UNKNOWN, writer=false, auto=false. 행은 삭제하지 않는다."
    )
    assert registry.MISSING_BINDING_RULE == (
        "policy/cap/owner/canary 부재 시 행을 보존하고\n"
        "  blocked|disabled + 사유. worker 는 값을 발명하지 않는다."
    )


def test_r2_reject_code_allowlist_is_exact_and_consumes_j2b_literal() -> None:
    assert registry.R2_REJECT_CODES == {
        "lane_signed_restriction_violation",
        "lane_recurring_not_authorized",
        "canonical_lane_identity_mismatch",
        "invalid_scheduler_owner",
        "invalid_timing_owner",
        "physical_account_writer_conflict",
        "canonical_lane_ids_mismatch",
        "canonical_credential_namespace_mismatch",
        "canonical_host_allowlist_mismatch",
        "lane_broker_mismatch",
        "lane_account_profile_mismatch",
        "lane_account_mode_mismatch",
        "lane_policy_binding_mismatch",
        "lane_binding_incomplete",
        lineage.BROKER_CLIENT_ID_TARGET_MISMATCH,
        "lane_quote_currency_mismatch",
    }
    assert "unknown_lane" not in registry.R2_REJECT_CODES


def test_g1_rejects_new_recurring_schedule_and_missing_cadence_proof() -> None:
    lane_id = "kr.kiwoom.mock"
    with pytest.raises(registry.ActivationTransitionBlocked) as new_schedule:
        registry.transition_activation(
            lane_id,
            registry.ActivationStatus.READY,
            registry.ActivationStatus.ENABLED,
            evidence=registry.ActivationEvidence(
                directly_proven_cadence_preserved=True,
                requires_new_recurring_schedule=True,
            ),
        )
    assert new_schedule.value.guard_id == "G1"
    assert new_schedule.value.code == "AUTO_READY_BLOCKED_BY_SCHEDULER"

    with pytest.raises(registry.ActivationTransitionBlocked) as no_proof:
        registry.transition_activation(
            lane_id,
            registry.ActivationStatus.READY,
            registry.ActivationStatus.ENABLED,
        )
    assert no_proof.value.guard_id == "G1"
    assert no_proof.value.code == "directly_proven_cadence_required"

    assert (
        registry.transition_activation(
            lane_id,
            registry.ActivationStatus.READY,
            registry.ActivationStatus.ENABLED,
            evidence=registry.ActivationEvidence(
                directly_proven_cadence_preserved=True
            ),
        )
        is registry.ActivationStatus.ENABLED
    )


def test_g2_stops_at_ready_for_mock_deployment() -> None:
    lane_id = "kr.kiwoom.mock"
    release_evidence = registry.ActivationEvidence(
        shared_production_release_required=True
    )

    assert (
        registry.transition_activation(
            lane_id,
            registry.ActivationStatus.BLOCKED,
            registry.ActivationStatus.READY_FOR_MOCK_DEPLOYMENT,
            evidence=release_evidence,
        )
        is registry.ActivationStatus.READY_FOR_MOCK_DEPLOYMENT
    )

    with pytest.raises(registry.ActivationTransitionBlocked) as skipped_stop:
        registry.transition_activation(
            lane_id,
            registry.ActivationStatus.BLOCKED,
            registry.ActivationStatus.ENABLED,
            evidence=release_evidence,
        )
    assert skipped_stop.value.guard_id == "G2"

    with pytest.raises(registry.ActivationTransitionBlocked) as terminal:
        registry.transition_activation(
            lane_id,
            registry.ActivationStatus.READY_FOR_MOCK_DEPLOYMENT,
            registry.ActivationStatus.READY,
        )
    assert terminal.value.guard_id == "G2"
    assert terminal.value.code == "ready_for_mock_deployment_terminal"


def test_g3_canary_moves_pending_to_ready_only() -> None:
    lane_id = "kr.kiwoom.mock"
    canary_evidence = registry.ActivationEvidence(j8_canary_succeeded=True)

    assert (
        registry.transition_activation(
            lane_id,
            registry.ActivationStatus.RUNTIME_ACCEPTANCE_PENDING,
            registry.ActivationStatus.READY,
            evidence=canary_evidence,
        )
        is registry.ActivationStatus.READY
    )

    with pytest.raises(registry.ActivationTransitionBlocked) as enabled:
        registry.transition_activation(
            lane_id,
            registry.ActivationStatus.RUNTIME_ACCEPTANCE_PENDING,
            registry.ActivationStatus.ENABLED,
            evidence=canary_evidence,
        )
    assert enabled.value.guard_id == "G3"
    assert enabled.value.code == "j8_canary_cannot_auto_enable"

    with pytest.raises(registry.ActivationTransitionBlocked) as missing:
        registry.transition_activation(
            lane_id,
            registry.ActivationStatus.RUNTIME_ACCEPTANCE_PENDING,
            registry.ActivationStatus.READY,
        )
    assert missing.value.guard_id == "G3"
    assert missing.value.code == "j8_canary_evidence_required"


@pytest.mark.parametrize(
    "lane_id",
    (
        "us.alpaca.paper.default",
        "us.alpaca.paper.lab",
        "us.kis.mock",
        "us.kiwoom.mock",
        "crypto.binance.spot_demo.canonical",
        "crypto.binance.spot_demo.b0x_sidecar",
        "crypto.alpaca.paper.default",
        "crypto.alpaca.paper.clean",
        "crypto.upbit.shadow",
        "crypto.binance.futures_demo",
    ),
)
def test_fully_bound_signed_lane_promotion_is_immutable(lane_id: str) -> None:
    _, envelope = _lineage_envelope(lane_id)
    mutant = _fully_bound_entry(envelope, lane_id)

    with pytest.raises(registry.RegistryStartupError) as exc_info:
        registry.assert_registry_startup(
            _registry_replacing(mutant), require_canonical=True
        )

    assert "lane_signed_restriction_violation" in _issue_codes(exc_info.value)


@pytest.mark.parametrize(
    "lane_id",
    (
        "us.alpaca.paper.default",
        "us.alpaca.paper.lab",
        "us.kiwoom.mock",
        "crypto.binance.spot_demo.canonical",
        "crypto.binance.spot_demo.b0x_sidecar",
        "crypto.alpaca.paper.default",
        "crypto.alpaca.paper.clean",
        "crypto.upbit.shadow",
        "crypto.binance.futures_demo",
    ),
)
def test_signed_lane_cannot_transition_to_enabled(lane_id: str) -> None:
    with pytest.raises(registry.ActivationTransitionBlocked) as exc_info:
        registry.transition_activation(
            lane_id,
            registry.ActivationStatus.READY,
            registry.ActivationStatus.ENABLED,
            evidence=registry.ActivationEvidence(
                directly_proven_cadence_preserved=True
            ),
        )

    assert exc_info.value.code == "lane_signed_restriction_violation"
    assert exc_info.value.guard_id == "B2"


def test_recurring_requires_lane_and_activation_enabled_together() -> None:
    _, envelope = _lineage_envelope("kr.kiwoom.mock")
    both_enabled = _fully_bound_entry(envelope, "kr.kiwoom.mock")
    lane_only = replace(
        both_enabled,
        activation_status=registry.ActivationStatus.READY,
    )
    activation_only = replace(both_enabled, lane_status=LaneStatus.NOT_READY)

    for one_sided in (lane_only, activation_only):
        with pytest.raises(registry.LaneGuardError) as exc_info:
            registry.assert_recurring_authorized(
                one_sided,
                recurring_requested=True,
            )
        assert exc_info.value.code == "lane_recurring_not_authorized"

    registry.assert_recurring_authorized(both_enabled, recurring_requested=True)


def test_bounded_canary_does_not_authorize_recurring() -> None:
    _, envelope = _lineage_envelope("kr.kiwoom.mock")
    entry = _fully_bound_entry(envelope, "kr.kiwoom.mock")

    registry.assert_recurring_authorized(
        entry,
        recurring_requested=False,
        bounded_canary=True,
    )
    with pytest.raises(registry.LaneGuardError) as exc_info:
        registry.assert_recurring_authorized(
            entry,
            recurring_requested=True,
            bounded_canary=True,
        )
    assert exc_info.value.code == "lane_recurring_not_authorized"


def test_unknown_fingerprint_rows_are_safe_and_preserved() -> None:
    binance_demo_physical_account_id = (
        "binance_demo:spot_plus_futures:credential_fingerprint="
        "sha256:e33925948f2cb6e03842cca9967b70f11f9242bc5c8f99c69ce0ca5cbc4d73df:"
        "one_shared_domain"
    )
    binance_demo_fingerprint_evidence_ref = (
        "d2-phasea-20260817:impl="
        "sha256:44a9a5b4059c176eb8300d23048cd396daa77d6400faa3be8bbaf7c465d6ee82;"
        "verify=sha256:03cfae4c8a9193ce0aa8ef4803d7e4ff3190eca1b6de777862b44d410a498e21"
    )
    expected_identity_by_lane = {
        "kr.kis.mock": (None, None, "UNKNOWN"),
        "kr.kiwoom.mock": (None, None, "UNKNOWN"),
        "us.kis.mock": (None, None, "UNKNOWN"),
        "us.kiwoom.mock": (None, None, "UNKNOWN"),
        "us.alpaca.paper.default": (None, None, "UNKNOWN"),
        "us.alpaca.paper.lab": (None, None, "UNKNOWN"),
        "crypto.binance.spot_demo.canonical": (
            binance_demo_physical_account_id,
            binance_demo_fingerprint_evidence_ref,
            "KNOWN",
        ),
        "crypto.binance.spot_demo.b0x_sidecar": (
            binance_demo_physical_account_id,
            binance_demo_fingerprint_evidence_ref,
            "KNOWN",
        ),
        "crypto.alpaca.paper.default": (None, None, "UNKNOWN"),
        "crypto.alpaca.paper.clean": (None, None, "UNKNOWN"),
        "crypto.upbit.shadow": (None, None, "UNKNOWN"),
        "crypto.binance.futures_demo": (
            binance_demo_physical_account_id,
            binance_demo_fingerprint_evidence_ref,
            "KNOWN",
        ),
    }

    assert len(registry.CANONICAL_LANE_REGISTRY) == 12
    assert tuple(expected_identity_by_lane) == registry.CANONICAL_LANE_IDS
    for entry in registry.CANONICAL_LANE_REGISTRY:
        (
            expected_physical_account_id,
            expected_fingerprint_evidence_ref,
            expected_identity_status,
        ) = expected_identity_by_lane[entry.lane_id]
        assert entry.physical_account_id == expected_physical_account_id
        assert entry.fingerprint_evidence_ref == expected_fingerprint_evidence_ref
        assert entry.identity_status == expected_identity_status
        assert entry.writer is False
        assert entry.auto is False
        assert registry.get_lane_registry_entry(entry.lane_id) is entry


def test_j2a_binance_demo_identity_amendment_is_verbatim_and_additive() -> None:
    """§3 values are exact; §4 changes no base-row field beyond the binding."""

    lane_ids = (
        "crypto.binance.spot_demo.canonical",
        "crypto.binance.spot_demo.b0x_sidecar",
        "crypto.binance.futures_demo",
    )
    physical_account_id = (
        "binance_demo:spot_plus_futures:credential_fingerprint="
        "sha256:e33925948f2cb6e03842cca9967b70f11f9242bc5c8f99c69ce0ca5cbc4d73df:"
        "one_shared_domain"
    )
    fingerprint_evidence_ref = (
        "d2-phasea-20260817:impl="
        "sha256:44a9a5b4059c176eb8300d23048cd396daa77d6400faa3be8bbaf7c465d6ee82;"
        "verify=sha256:03cfae4c8a9193ce0aa8ef4803d7e4ff3190eca1b6de777862b44d410a498e21"
    )
    base_by_id = {
        entry.lane_id: entry for entry in registry._BASE_CANONICAL_LANE_REGISTRY
    }
    effective_by_id = _by_id()
    binding_fields = {
        "physical_account_id",
        "identity_status",
        "fingerprint_evidence_ref",
        "missing_bindings",
    }
    expected_activation_statuses = {
        "crypto.binance.spot_demo.canonical": registry.ActivationStatus.BLOCKED,
        "crypto.binance.spot_demo.b0x_sidecar": registry.ActivationStatus.DISABLED,
        "crypto.binance.futures_demo": registry.ActivationStatus.DISABLED,
    }

    assert len(fields(registry.LaneRegistryEntry)) == 34
    assert (
        tuple(lane_id for lane_id in lane_ids if lane_id in effective_by_id) == lane_ids
    )
    for lane_id, base in base_by_id.items():
        effective = effective_by_id[lane_id]
        changed_fields = {
            field.name
            for field in fields(registry.LaneRegistryEntry)
            if getattr(base, field.name) != getattr(effective, field.name)
        }
        if lane_id not in lane_ids:
            assert changed_fields == set()
            continue

        assert changed_fields == binding_fields
        assert effective.physical_account_id == physical_account_id
        assert effective.identity_status == "KNOWN"
        assert effective.fingerprint_evidence_ref == fingerprint_evidence_ref
        assert effective.missing_bindings == (
            registry.MissingBinding.POLICY,
            registry.MissingBinding.CAP,
            registry.MissingBinding.OWNER,
            registry.MissingBinding.CANARY,
        )
        assert effective.activation_status is expected_activation_statuses[lane_id]
        assert effective.activation_status is base.activation_status
        assert effective.writer is base.writer is False
        assert effective.auto_order_enabled is base.auto_order_enabled is False


def test_binance_demo_identity_unblocks_only_j3a_scope_not_execution_grant() -> None:
    """Known identity permits the lease key; activation/writer gates still deny use."""

    from app.services.mock_integration import coordination

    lane_ids = (
        "crypto.binance.spot_demo.canonical",
        "crypto.binance.spot_demo.b0x_sidecar",
        "crypto.binance.futures_demo",
    )
    entries = [registry.get_lane_registry_entry(lane_id) for lane_id in lane_ids]
    scopes = [coordination.physical_account_scope_for_entry(entry) for entry in entries]

    assert scopes[0] == scopes[1] == scopes[2]
    assert tuple(coordination.physical_account_scope_for_entry.__annotations__) == (
        "entry",
        "return",
    )
    scope_deriver = coordination.physical_account_scope_for_entry
    with pytest.raises(TypeError):
        scope_deriver(entries[0], "caller")  # type: ignore[call-arg]

    for entry in entries:
        with pytest.raises(registry.LaneGuardError) as refusal:
            registry.assert_entry_execution_ready(entry)
        assert refusal.value.code == "lane_activation_not_enabled"


@pytest.mark.parametrize(
    "changes",
    (
        {"writer": True},
        {"auto_order_enabled": True},
        {"physical_account_id": "opaque-test-id"},
    ),
)
def test_unknown_identity_mutants_fail_closed(changes: dict[str, object]) -> None:
    mutant = replace(registry.CANONICAL_LANE_REGISTRY[0], **changes)

    with pytest.raises(registry.RegistryStartupError) as exc_info:
        registry.assert_registry_startup((mutant,))

    assert "unknown_identity_must_be_safe" in _issue_codes(exc_info.value)


def test_missing_bindings_keep_rows_blocked_or_disabled_with_reasons() -> None:
    required_missing = {
        registry.MissingBinding.PHYSICAL_ACCOUNT_FINGERPRINT,
        registry.MissingBinding.POLICY,
        registry.MissingBinding.CAP,
        registry.MissingBinding.OWNER,
        registry.MissingBinding.CANARY,
    }

    assert (
        tuple(entry.lane_id for entry in registry.CANONICAL_LANE_REGISTRY)
        == registry.CANONICAL_LANE_IDS
    )
    for entry in registry.CANONICAL_LANE_REGISTRY:
        expected_missing = required_missing
        if entry.lane_id in {
            "crypto.binance.spot_demo.canonical",
            "crypto.binance.spot_demo.b0x_sidecar",
            "crypto.binance.futures_demo",
        }:
            expected_missing = required_missing - {
                registry.MissingBinding.PHYSICAL_ACCOUNT_FINGERPRINT
            }
        assert set(entry.missing_bindings) == expected_missing
        assert entry.activation_status in {
            registry.ActivationStatus.BLOCKED,
            registry.ActivationStatus.DISABLED,
        }
        assert entry.activation_reason
        assert entry.policy_binding is None
        assert entry.max_order_notional is None
        assert entry.canary_binding is None


def test_missing_binding_reason_is_required_once() -> None:
    mutant = replace(registry.CANONICAL_LANE_REGISTRY[0], activation_reason="")

    with pytest.raises(registry.RegistryStartupError) as exc_info:
        registry.assert_registry_startup((mutant,))

    matching = [
        issue
        for issue in exc_info.value.issues
        if issue.code == "missing_binding_reason_absent"
    ]
    assert len(matching) == 1


def test_live_account_mode_is_not_representable_or_dispatchable() -> None:
    mutant = replace(
        registry.CANONICAL_LANE_REGISTRY[0],
        account_mode="live",
        lane_type="live",
        endpoint_class="live",
    )

    with pytest.raises(registry.RegistryStartupError) as startup:
        registry.assert_registry_startup((mutant,))
    assert "live_account_mode_forbidden" in _issue_codes(startup.value)

    with pytest.raises(registry.LaneGuardError) as dispatch:
        registry.assert_mock_only_endpoint(
            mutant, "https://openapi.koreainvestment.com:9443"
        )
    assert dispatch.value.code == "live_account_mode_forbidden"


def test_canonical_registry_passes_startup_assertion() -> None:
    registry.assert_registry_startup(
        registry.CANONICAL_LANE_REGISTRY, require_canonical=True
    )


def test_startup_default_and_explicit_false_accept_full_canonical_registry() -> None:
    assert (
        registry.assert_registry_startup(
            registry.CANONICAL_LANE_REGISTRY, require_canonical=False
        )
        is None
    )
    assert registry.assert_registry_startup(registry.CANONICAL_LANE_REGISTRY) is None


def test_startup_default_and_explicit_false_skip_canonical_only_checks() -> None:
    custom = replace(
        registry.CANONICAL_LANE_REGISTRY[0],
        lane_id="kr.custom.mock",
        broker="custom",
        credential_namespace="CUSTOM_MOCK_*",
        allowed_hosts=("mock.custom.invalid",),
    )
    identity_mismatch = replace(
        registry.CANONICAL_LANE_REGISTRY[1],
        role=registry.RegistryRole.AUTO_MIRROR,
    )
    assert registry.assert_registry_startup((custom,), require_canonical=False) is None
    assert registry.assert_registry_startup((custom,)) is None

    with pytest.raises(registry.RegistryStartupError) as strict:
        registry.assert_registry_startup(
            (custom, identity_mismatch), require_canonical=True
        )
    assert {
        "canonical_lane_ids_mismatch",
        "canonical_lane_identity_mismatch",
        "lane_quote_currency_mismatch",
        "canonical_credential_namespace_mismatch",
        "canonical_host_allowlist_mismatch",
    } <= _issue_codes(strict.value)


def test_same_physical_account_two_writers_fail_without_identifier_leak() -> None:
    raw_identifier = "opaque-account-id-that-must-not-appear"
    first = replace(
        registry.CANONICAL_LANE_REGISTRY[0],
        physical_account_id=raw_identifier,
        writer=True,
    )
    second = replace(
        registry.CANONICAL_LANE_REGISTRY[1],
        physical_account_id=raw_identifier,
        writer=True,
    )

    with pytest.raises(registry.RegistryStartupError) as exc_info:
        registry.assert_single_writer((first, second))

    assert _issue_codes(exc_info.value) == {"physical_account_writer_conflict"}
    assert raw_identifier not in str(exc_info.value)
    assert "[MASKED]" in str(exc_info.value)
    assert registry.mask_account_identifier(raw_identifier) == "[MASKED]"
    assert registry.mask_account_identifier(None) is None
    assert raw_identifier not in repr(first)


def test_scheduler_owner_and_timing_owner_cannot_collapse() -> None:
    mutant = replace(
        registry.CANONICAL_LANE_REGISTRY[0],
        scheduler_owner=SchedulerOwner.MANUAL,
        timing_owner="manual",
    )

    with pytest.raises(registry.RegistryStartupError) as exc_info:
        registry.assert_registry_startup((mutant,))

    assert "invalid_timing_owner" in _issue_codes(exc_info.value)


@pytest.mark.parametrize(
    ("field_name", "value", "reason"),
    (
        ("scheduler_owner", "manual", "invalid_scheduler_owner"),
        ("timing_owner", "   ", "invalid_timing_owner"),
        ("timing_owner", 17, "invalid_timing_owner"),
    ),
)
def test_owner_types_are_strict(field_name: str, value: object, reason: str) -> None:
    mutant = replace(
        registry.CANONICAL_LANE_REGISTRY[0],
        **{field_name: value},
    )

    with pytest.raises(registry.RegistryStartupError) as exc_info:
        registry.assert_registry_startup((mutant,))

    assert reason in _issue_codes(exc_info.value)


@pytest.mark.parametrize(("version", "version_hash"), (("", "hash"), ("v1", " ")))
def test_policy_binding_fields_must_be_nonblank(
    version: str, version_hash: str
) -> None:
    with pytest.raises(ValueError, match="^lane_binding_incomplete$"):
        registry.PolicyBinding(version, version_hash)


def test_credential_namespaces_and_host_allowlists_are_exact() -> None:
    assert dict(registry.LANE_CREDENTIAL_NAMESPACES) == {
        "kr.kis.mock": "KIS_MOCK_*",
        "kr.kiwoom.mock": "KIWOOM_MOCK_*",
        "us.kis.mock": "KIS_MOCK_*",
        "us.kiwoom.mock": "KIWOOM_MOCK_US_*",
        "us.alpaca.paper.default": "ALPACA_PAPER_*",
        "us.alpaca.paper.lab": "ALPACA_PAPER_LAB_*",
        "crypto.binance.spot_demo.canonical": "BINANCE_SPOT_DEMO_API_*",
        "crypto.binance.spot_demo.b0x_sidecar": "BINANCE_SPOT_DEMO_API_*",
        "crypto.alpaca.paper.default": "ALPACA_PAPER_*",
        "crypto.alpaca.paper.clean": "ALPACA_PAPER_CRYPTO_*",
        "crypto.upbit.shadow": None,
        "crypto.binance.futures_demo": "BINANCE_FUTURES_DEMO_API_*",
    }
    assert dict(registry.LANE_ALLOWED_HOSTS) == {
        "kr.kis.mock": ("openapivts.koreainvestment.com:29443",),
        "kr.kiwoom.mock": ("mockapi.kiwoom.com",),
        "us.kis.mock": ("openapivts.koreainvestment.com:29443",),
        "us.kiwoom.mock": ("mockapi.kiwoom.com",),
        "us.alpaca.paper.default": ("paper-api.alpaca.markets",),
        "us.alpaca.paper.lab": ("paper-api.alpaca.markets",),
        "crypto.binance.spot_demo.canonical": ("demo-api.binance.com",),
        "crypto.binance.spot_demo.b0x_sidecar": ("demo-api.binance.com",),
        "crypto.alpaca.paper.default": ("paper-api.alpaca.markets",),
        "crypto.alpaca.paper.clean": ("paper-api.alpaca.markets",),
        "crypto.upbit.shadow": (),
        "crypto.binance.futures_demo": ("demo-fapi.binance.com",),
    }


@pytest.mark.parametrize(
    ("field_name", "value", "reason"),
    (
        (
            "credential_namespace",
            "CHANGED_*",
            "canonical_credential_namespace_mismatch",
        ),
        (
            "allowed_hosts",
            ("changed.invalid",),
            "canonical_host_allowlist_mismatch",
        ),
    ),
)
def test_retained_canonical_binding_reasons_remain_exact(
    field_name: str,
    value: object,
    reason: str,
) -> None:
    mutant = replace(_by_id()["kr.kiwoom.mock"], **{field_name: value})

    with pytest.raises(registry.RegistryStartupError) as exc_info:
        registry.assert_registry_startup(
            _registry_replacing(mutant), require_canonical=True
        )

    assert reason in _issue_codes(exc_info.value)


@pytest.mark.asyncio
async def test_currency_mismatch_blocks_before_broker_io() -> None:
    _, envelope = _lineage_envelope(
        "kr.kiwoom.mock",
        intent_overrides={"target_notional_currency": "USD"},
        plan_overrides={"quote_currency": "USD"},
    )
    await _assert_both_boundaries_reject(
        envelope,
        registry.CANONICAL_LANE_REGISTRY,
        reason="lane_quote_currency_mismatch",
        endpoint_url="https://mockapi.kiwoom.com",
        credential_namespace="KIWOOM_MOCK_*",
    )


def test_factory_derived_alpaca_paper_binding_uses_exact_canonical_values() -> None:
    _, envelope = _lineage_envelope("us.alpaca.paper.default")
    assert envelope.execution_plan is not None
    entry = _policy_bound_entry(envelope, "us.alpaca.paper.default")

    resolved = registry.assert_lineage_registry_binding(
        envelope,
        _registry_replacing(entry),
    )

    assert envelope.decision_intent.decision_intent_id.startswith("mock-intent-v1:")
    assert envelope.execution_plan.execution_plan_id.startswith("mock-plan-v1:")
    assert envelope.execution_plan.account_profile == "paper"
    assert envelope.execution_plan.account_mode == "paper"
    assert resolved is entry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value", "reason"),
    (
        ("broker", "other-broker", "lane_broker_mismatch"),
        ("account_profile", "other-profile", "lane_account_profile_mismatch"),
        ("account_mode", "alpaca_paper", "lane_account_mode_mismatch"),
    ),
)
async def test_lineage_plan_identity_mismatch_rejects_both_boundaries(
    field_name: str,
    value: str,
    reason: str,
) -> None:
    _, envelope = _lineage_envelope(
        "kr.kiwoom.mock",
        plan_overrides={field_name: value},
    )
    entry = _policy_bound_entry(envelope, "kr.kiwoom.mock")

    await _assert_both_boundaries_reject(
        envelope,
        _registry_replacing(entry),
        reason=reason,
        endpoint_url="https://mockapi.kiwoom.com",
        credential_namespace="KIWOOM_MOCK_*",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent_overrides", "policy_binding"),
    (
        (
            {"policy_version": "changed-policy"},
            registry.PolicyBinding("expected-policy", "test-policy-hash-v1"),
        ),
        (
            {"policy_version_hash": "changed-hash"},
            registry.PolicyBinding("test-policy-v1", "expected-hash"),
        ),
    ),
)
async def test_lineage_policy_mismatch_rejects_both_boundaries(
    intent_overrides: dict[str, object],
    policy_binding: registry.PolicyBinding,
) -> None:
    _, envelope = _lineage_envelope(
        "kr.kiwoom.mock",
        intent_overrides=intent_overrides,
    )
    entry = replace(
        _policy_bound_entry(envelope, "kr.kiwoom.mock"),
        policy_binding=policy_binding,
    )

    await _assert_both_boundaries_reject(
        envelope,
        _registry_replacing(entry),
        reason="lane_policy_binding_mismatch",
        endpoint_url="https://mockapi.kiwoom.com",
        credential_namespace="KIWOOM_MOCK_*",
    )


@pytest.mark.asyncio
async def test_absent_policy_binding_keeps_existing_incomplete_reason() -> None:
    _, envelope = _lineage_envelope("kr.kiwoom.mock")

    await _assert_both_boundaries_reject(
        envelope,
        registry.CANONICAL_LANE_REGISTRY,
        reason="lane_binding_incomplete",
        endpoint_url="https://mockapi.kiwoom.com",
        credential_namespace="KIWOOM_MOCK_*",
    )


@pytest.mark.asyncio
async def test_declared_endpoint_and_namespace_reject_before_opaque_callbacks() -> None:
    _, envelope = _lineage_envelope("kr.kiwoom.mock")
    entry = _policy_bound_entry(envelope, "kr.kiwoom.mock")
    snapshot = _registry_replacing(entry)

    await _assert_both_boundaries_reject(
        envelope,
        snapshot,
        reason="live_endpoint_forbidden",
        endpoint_url="https://api.kiwoom.com",
        credential_namespace="KIWOOM_MOCK_*",
    )
    await _assert_both_boundaries_reject(
        envelope,
        snapshot,
        reason="credential_namespace_mismatch",
        endpoint_url="https://mockapi.kiwoom.com",
        credential_namespace="KIWOOM_MOCK_US_*",
    )


@pytest.mark.asyncio
async def test_current_blocked_row_never_reaches_broker_io() -> None:
    called = 0

    async def broker_io() -> None:
        nonlocal called
        called += 1

    _, envelope = _lineage_envelope("kr.kiwoom.mock")
    entry = _policy_bound_entry(envelope, "kr.kiwoom.mock")
    with pytest.raises(registry.LaneGuardError) as exc_info:
        await registry.guarded_broker_io(
            envelope,
            endpoint_url="https://mockapi.kiwoom.com",
            credential_namespace="KIWOOM_MOCK_*",
            broker_io=broker_io,
            registry=_registry_replacing(entry),
        )

    assert exc_info.value.code == "lane_activation_not_enabled"
    assert called == 0


@pytest.mark.asyncio
async def test_broker_boundary_rechecks_single_writer_registry() -> None:
    physical_account_id = "opaque-test-account"
    _, envelope = _lineage_envelope("kr.kiwoom.mock")
    first = _fully_bound_entry(
        envelope,
        "kr.kis.mock",
        physical_account_id=physical_account_id,
    )
    second = _fully_bound_entry(
        envelope,
        "kr.kiwoom.mock",
        physical_account_id=physical_account_id,
    )
    replacements = {first.lane_id: first, second.lane_id: second}
    snapshot = tuple(
        replacements.get(entry.lane_id, entry)
        for entry in registry.CANONICAL_LANE_REGISTRY
    )

    await _assert_both_boundaries_reject(
        envelope,
        snapshot,
        reason="physical_account_writer_conflict",
        endpoint_url="https://mockapi.kiwoom.com",
        credential_namespace="KIWOOM_MOCK_*",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("market", "changed"),
        ("broker", "changed"),
        ("account_profile", "changed"),
        ("profile_variant", "changed"),
        ("account_mode", registry.AccountMode.PAPER),
        ("lane_type", registry.AccountMode.PAPER),
        ("quote_currency", "USD"),
        ("role", registry.RegistryRole.AUTO_MIRROR),
        ("role_pending_reason", "changed"),
        ("role_on_policy_approval", registry.RegistryRole.AUTO_CHALLENGER),
        ("endpoint_class", registry.EndpointClass.PAPER),
        ("credential_namespace", "CHANGED_*"),
        ("allowed_hosts", ("changed.invalid",)),
    ),
)
async def test_all_13_canonical_identity_fields_are_immutable_at_both_boundaries(
    field_name: str,
    value: object,
) -> None:
    _, envelope = _lineage_envelope("kr.kiwoom.mock")
    mutant = replace(_by_id()["kr.kiwoom.mock"], **{field_name: value})

    await _assert_both_boundaries_reject(
        envelope,
        _registry_replacing(mutant),
        reason="canonical_lane_identity_mismatch",
        endpoint_url="https://mockapi.kiwoom.com",
        credential_namespace="KIWOOM_MOCK_*",
    )


@pytest.mark.asyncio
async def test_custom_registry_cannot_bypass_canonical_lane_set() -> None:
    _, envelope = _lineage_envelope("kr.kiwoom.mock")
    custom = replace(
        _by_id()["kr.kis.mock"],
        lane_id="kr.custom.mock",
        broker="custom",
    )
    snapshot = (custom, *registry.CANONICAL_LANE_REGISTRY[1:])

    await _assert_both_boundaries_reject(
        envelope,
        snapshot,
        reason="canonical_lane_ids_mismatch",
        endpoint_url="https://mockapi.kiwoom.com",
        credential_namespace="KIWOOM_MOCK_*",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value", "reason"),
    (
        ("scheduler_owner", "manual", "invalid_scheduler_owner"),
        ("timing_owner", " ", "invalid_timing_owner"),
    ),
)
async def test_invalid_owner_types_reject_both_boundaries_without_calls(
    field_name: str,
    value: object,
    reason: str,
) -> None:
    _, envelope = _lineage_envelope("kr.kiwoom.mock")
    mutant = replace(_by_id()["kr.kiwoom.mock"], **{field_name: value})

    await _assert_both_boundaries_reject(
        envelope,
        _registry_replacing(mutant),
        reason=reason,
        endpoint_url="https://mockapi.kiwoom.com",
        credential_namespace="KIWOOM_MOCK_*",
    )


@pytest.mark.asyncio
async def test_fully_bound_signed_restriction_rejects_both_boundaries() -> None:
    _, envelope = _lineage_envelope("crypto.alpaca.paper.default")
    mutant = _fully_bound_entry(envelope, "crypto.alpaca.paper.default")

    await _assert_both_boundaries_reject(
        envelope,
        _registry_replacing(mutant),
        reason="lane_signed_restriction_violation",
        endpoint_url="https://paper-api.alpaca.markets",
        credential_namespace="ALPACA_PAPER_*",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lane_status", "activation_status"),
    (
        (LaneStatus.AUTO_ENABLED, registry.ActivationStatus.READY),
        (LaneStatus.NOT_READY, registry.ActivationStatus.ENABLED),
    ),
)
async def test_one_sided_recurring_state_rejects_both_boundaries(
    lane_status: LaneStatus,
    activation_status: registry.ActivationStatus,
) -> None:
    _, envelope = _lineage_envelope("kr.kiwoom.mock")
    mutant = _fully_bound_entry(
        envelope,
        "kr.kiwoom.mock",
        lane_status=lane_status,
        activation_status=activation_status,
    )

    await _assert_both_boundaries_reject(
        envelope,
        _registry_replacing(mutant),
        reason="lane_recurring_not_authorized",
        endpoint_url="https://mockapi.kiwoom.com",
        credential_namespace="KIWOOM_MOCK_*",
        recurring_requested=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lane_id", "endpoint_url", "credential_namespace"),
    (
        (
            "kr.kis.mock",
            "https://openapivts.koreainvestment.com:29443",
            "KIS_MOCK_*",
        ),
        ("kr.kiwoom.mock", "https://mockapi.kiwoom.com", "KIWOOM_MOCK_*"),
    ),
)
async def test_kis_and_kiwoom_none_client_id_pair_pass_exact_registered_binding(
    lane_id: str,
    endpoint_url: str,
    credential_namespace: str,
) -> None:
    factory, plan_envelope = _lineage_envelope(lane_id)
    envelope = factory.create_attempt_envelope(
        plan_envelope,
        OrderAttemptDraft(
            cycle_id=f"test-cycle-{lane_id}",
            attempt_seq=1,
            lane_prefix=None,
            broker_client_id_target=None,
        ),
    )
    entry = _fully_bound_entry(envelope, lane_id)
    snapshot = _registry_replacing(entry)
    broker_calls = 0
    factory_calls = 0

    async def broker_io() -> str:
        nonlocal broker_calls
        broker_calls += 1
        return "broker-ok"

    def client_factory() -> str:
        nonlocal factory_calls
        factory_calls += 1
        return "factory-ok"

    assert envelope.order_attempt is not None
    assert envelope.order_attempt.broker_client_order_id is None
    assert (
        await registry.guarded_broker_io(
            envelope,
            endpoint_url=endpoint_url,
            credential_namespace=credential_namespace,
            broker_io=broker_io,
            registry=snapshot,
        )
        == "broker-ok"
    )
    assert (
        registry.guarded_client_factory(
            envelope,
            endpoint_url=endpoint_url,
            credential_namespace=credential_namespace,
            factory=client_factory,
            registry=snapshot,
        )
        == "factory-ok"
    )
    assert broker_calls == 1
    assert factory_calls == 1


@pytest.mark.asyncio
async def test_unknown_broker_none_client_id_pair_stops_at_existing_unknown_lane() -> (
    None
):
    factory, plan_envelope = _lineage_envelope(
        "kr.kis.mock",
        plan_overrides={
            "lane_id": "kr.unregistered.mock",
            "broker": "unregistered",
        },
    )
    envelope = factory.create_attempt_envelope(
        plan_envelope,
        OrderAttemptDraft(
            cycle_id="test-cycle-unregistered",
            attempt_seq=1,
            lane_prefix=None,
            broker_client_id_target=None,
        ),
    )
    registered_entry = _fully_bound_entry(envelope, "kr.kis.mock")

    await _assert_both_boundaries_reject(
        envelope,
        _registry_replacing(registered_entry),
        reason="unknown_lane",
        endpoint_url="https://openapivts.koreainvestment.com:29443",
        credential_namespace="KIS_MOCK_*",
    )


def test_declared_near_miss_host_rejects_before_opaque_factory() -> None:
    _, envelope = _lineage_envelope("kr.kiwoom.mock")
    entry = _policy_bound_entry(envelope, "kr.kiwoom.mock")
    called = 0

    def factory() -> object:
        nonlocal called
        called += 1
        return object()

    with pytest.raises(registry.LaneGuardError) as near_miss:
        registry.guarded_client_factory(
            envelope,
            endpoint_url="https://mockapi.kiwoom.com.evil.test",
            credential_namespace="KIWOOM_MOCK_*",
            factory=factory,
            registry=_registry_replacing(entry),
        )
    assert near_miss.value.code == "lane_endpoint_host_mismatch"
    assert called == 0


def test_shadow_lane_structurally_rejects_broker_io() -> None:
    shadow = registry.get_lane_registry_entry("crypto.upbit.shadow")

    with pytest.raises(registry.LaneGuardError) as exc_info:
        registry.assert_mock_only_endpoint(shadow, "https://api.upbit.com")

    assert exc_info.value.code == "shadow_broker_io_forbidden"


def test_mirror_failure_is_record_only_without_peer_rollback_or_cancel() -> None:
    divergence = registry.record_mirror_divergence(
        "us.alpaca.paper.default",
        "us.kis.mock",
        "mirror execution failed",
    )

    assert divergence.primary_lane_id == "us.alpaca.paper.default"
    assert divergence.divergent_lane_id == "us.kis.mock"
    assert divergence.reason == "mirror execution failed"
    assert divergence.rollback_other_lanes is False
    assert divergence.cancel_other_lanes is False
