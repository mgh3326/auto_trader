from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.schemas.execution_contracts import LaneStatus, SchedulerOwner
from app.services import mock_lane_registry as registry


def _by_id() -> dict[str, registry.LaneRegistryEntry]:
    return {entry.lane_id: entry for entry in registry.CANONICAL_LANE_REGISTRY}


def _issue_codes(exc: registry.RegistryStartupError) -> set[str]:
    return {issue.code for issue in exc.issues}


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
    lane_id = "us.alpaca.paper.default"
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
    lane_id = "crypto.binance.spot_demo.canonical"
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


def test_unknown_fingerprint_rows_are_safe_and_preserved() -> None:
    assert len(registry.CANONICAL_LANE_REGISTRY) == 12
    for entry in registry.CANONICAL_LANE_REGISTRY:
        assert entry.physical_account_id is None
        assert entry.fingerprint_evidence_ref is None
        assert entry.identity_status == "UNKNOWN"
        assert entry.writer is False
        assert entry.auto is False
        assert registry.get_lane_registry_entry(entry.lane_id) is entry


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
        assert set(entry.missing_bindings) == required_missing
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

    assert "scheduler_timing_owner_collapsed" in _issue_codes(exc_info.value)


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


@pytest.mark.asyncio
async def test_currency_mismatch_blocks_before_broker_io() -> None:
    called = False

    async def broker_io() -> None:
        nonlocal called
        called = True

    plan = SimpleNamespace(lane_id="kr.kiwoom.mock", quote_currency="USD")
    with pytest.raises(registry.LaneGuardError) as exc_info:
        await registry.guarded_broker_io(
            plan,
            endpoint_url="https://mockapi.kiwoom.com",
            credential_namespace="KIWOOM_MOCK_*",
            broker_io=broker_io,
        )

    assert exc_info.value.code == "lane_quote_currency_mismatch"
    assert called is False


@pytest.mark.asyncio
async def test_live_endpoint_and_namespace_mismatch_block_before_broker_io() -> None:
    called = False

    async def broker_io() -> None:
        nonlocal called
        called = True

    plan = SimpleNamespace(lane_id="kr.kiwoom.mock", quote_currency="KRW")
    with pytest.raises(registry.LaneGuardError) as live:
        await registry.guarded_broker_io(
            plan,
            endpoint_url="https://api.kiwoom.com",
            credential_namespace="KIWOOM_MOCK_*",
            broker_io=broker_io,
        )
    assert live.value.code == "live_endpoint_forbidden"

    with pytest.raises(registry.LaneGuardError) as namespace:
        await registry.guarded_broker_io(
            plan,
            endpoint_url="https://mockapi.kiwoom.com",
            credential_namespace="KIWOOM_MOCK_US_*",
            broker_io=broker_io,
        )
    assert namespace.value.code == "credential_namespace_mismatch"
    assert called is False


@pytest.mark.asyncio
async def test_current_blocked_row_never_reaches_broker_io() -> None:
    called = False

    async def broker_io() -> None:
        nonlocal called
        called = True

    plan = SimpleNamespace(lane_id="kr.kiwoom.mock", quote_currency="KRW")
    with pytest.raises(registry.LaneGuardError) as exc_info:
        await registry.guarded_broker_io(
            plan,
            endpoint_url="https://mockapi.kiwoom.com",
            credential_namespace="KIWOOM_MOCK_*",
            broker_io=broker_io,
        )

    assert exc_info.value.code == "lane_activation_not_enabled"
    assert called is False


@pytest.mark.asyncio
async def test_broker_boundary_rechecks_single_writer_registry() -> None:
    called = False
    physical_account_id = "opaque-test-account"

    async def broker_io() -> None:
        nonlocal called
        called = True

    first = replace(
        registry.CANONICAL_LANE_REGISTRY[0],
        physical_account_id=physical_account_id,
        writer=True,
    )
    second = replace(
        registry.CANONICAL_LANE_REGISTRY[1],
        physical_account_id=physical_account_id,
        writer=True,
    )
    plan = SimpleNamespace(lane_id=first.lane_id, quote_currency="KRW")

    with pytest.raises(registry.RegistryStartupError) as exc_info:
        await registry.guarded_broker_io(
            plan,
            endpoint_url="https://openapivts.koreainvestment.com:29443",
            credential_namespace="KIS_MOCK_*",
            broker_io=broker_io,
            registry={first.lane_id: first, second.lane_id: second},
        )

    assert "physical_account_writer_conflict" in _issue_codes(exc_info.value)
    assert called is False


def test_live_client_factory_and_near_miss_host_are_never_invoked() -> None:
    entry = registry.get_lane_registry_entry("kr.kiwoom.mock")
    called = False

    def factory() -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(registry.LaneGuardError) as live:
        registry.guarded_client_factory(
            entry,
            endpoint_url="https://api.kiwoom.com",
            credential_namespace="KIWOOM_MOCK_*",
            factory=factory,
        )
    assert live.value.code == "live_endpoint_forbidden"

    with pytest.raises(registry.LaneGuardError) as near_miss:
        registry.guarded_client_factory(
            entry,
            endpoint_url="https://mockapi.kiwoom.com.evil.test",
            credential_namespace="KIWOOM_MOCK_*",
            factory=factory,
        )
    assert near_miss.value.code == "lane_endpoint_host_mismatch"
    assert called is False


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
