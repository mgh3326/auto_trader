"""Mock-only boundaries and append-only schema contract; no DB or broker I/O."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import UniqueConstraint

from app.models.kis_mock_dispatch_evidence import KISMockDispatchEvidence
from app.schemas.execution_contracts import SchedulerOwner
from app.services.kis_mock_runner.e3_activation import (
    ACTIVATION_ENV,
    E3_ACTIVATION_CONTRACT,
)
from app.services.kis_mock_runner.evidence_store import (
    KISMockEvidenceConflict,
    KISMockEvidenceStore,
    KISMockRecoveryAuthority,
    NativeOrderEvidence,
    _dispatch_matches_claim,
    _key,
)
from app.services.kis_mock_runner.registry_binding import (
    IDENTITY_ENV,
    kis_mock_registry_from_environment,
)
from app.services.kis_mock_runner.singleton import describe_kis_mock_lifecycle_readiness
from app.services.mock_integration.coordination import (
    DurableClaim,
    DurableSendClaimAdapter,
    TerminalClaimEvidence,
    physical_account_scope_for_entry,
)
from app.services.mock_lane_registry import (
    CANONICAL_LANE_REGISTRY,
    AccountMode,
    ActivationStatus,
    LaneGuardError,
    MissingBinding,
    RegistryStartupError,
    assert_entry_execution_ready,
    assert_recurring_authorized,
)
from app.services.order_send_intent_service import OrderSendIntentService

pytestmark = pytest.mark.unit
FINGERPRINT = "kismock:v1:" + "a" * 64


def bound_entry():
    return kis_mock_registry_from_environment(environ={IDENTITY_ENV: FINGERPRINT})[0]


def test_unconfigured_identity_remains_blocked():
    entry = kis_mock_registry_from_environment(environ={})[0]
    assert entry.physical_account_id is None
    assert entry.identity_status == "UNKNOWN"
    assert not entry.auto and not entry.writer
    with pytest.raises(LaneGuardError):
        physical_account_scope_for_entry(entry)
    with pytest.raises(LaneGuardError):
        KISMockEvidenceStore(AsyncMock(), entry=entry)


def test_identity_config_is_single_lane_and_never_activates():
    entries = kis_mock_registry_from_environment(environ={IDENTITY_ENV: FINGERPRINT})
    entry = entries[0]
    assert entries[1:] == CANONICAL_LANE_REGISTRY[1:]
    assert entry.physical_account_id == FINGERPRINT
    assert entry.identity_status == "KNOWN"
    assert MissingBinding.PHYSICAL_ACCOUNT_FINGERPRINT not in entry.missing_bindings
    assert entry.missing_bindings == tuple(
        v
        for v in CANONICAL_LANE_REGISTRY[0].missing_bindings
        if v is not MissingBinding.PHYSICAL_ACCOUNT_FINGERPRINT
    )
    assert physical_account_scope_for_entry(entry).claim_account_scope
    assert not entry.auto and not entry.writer
    with pytest.raises(LaneGuardError, match="lane_activation_not_enabled"):
        assert_entry_execution_ready(entry)
    readiness = describe_kis_mock_lifecycle_readiness(
        KISMockEvidenceStore(AsyncMock(), entry=entry).ports()
    )
    assert readiness.ready
    assert readiness.status == "AUTO_READY_PENDING_ACTIVATION"


@pytest.mark.parametrize("identity", [" ", "live:identity", "kismock:v1:" + "g" * 64])
def test_malformed_identity_refused(identity):
    with pytest.raises(LaneGuardError, match="configuration_invalid"):
        kis_mock_registry_from_environment(environ={IDENTITY_ENV: identity})


def test_live_other_lane_and_mismatched_identity_refused():
    entry = bound_entry()
    for invalid in (
        replace(entry, account_mode="live"),
        replace(entry, lane_id="us.kis.mock"),
        replace(entry, broker="toss"),
        replace(entry, credential_namespace="KIS_*"),
        replace(entry, physical_account_id=None),
        replace(entry, identity_status="UNKNOWN"),
        replace(entry, account_mode=AccountMode.PAPER),
    ):
        factory = AsyncMock()
        with pytest.raises(LaneGuardError):
            KISMockEvidenceStore(factory, entry=invalid)
        factory.assert_not_called()
    with pytest.raises(LaneGuardError, match="identity_mismatch"):
        kis_mock_registry_from_environment(
            entries=(entry, *CANONICAL_LANE_REGISTRY[1:]),
            environ={IDENTITY_ENV: "kismock:v1:" + "b" * 64},
        )


def test_idempotency_has_all_three_axes():
    keys = [
        tuple(column.name for column in constraint.columns)
        for constraint in KISMockDispatchEvidence.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    assert ("order_attempt_id", "kind", "broker_evidence_key") in keys


def test_migration_is_additive_and_retains_evidence_on_downgrade():
    source = (
        Path(__file__).resolve().parents[3]
        / "alembic/versions/20261102_rob1263_kis_evidence.py"
    ).read_text()
    upgrade = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert "ALTER TABLE" not in upgrade and "DROP " not in upgrade
    assert "review.order_send_intents" not in upgrade
    assert "BEFORE UPDATE OR DELETE OR TRUNCATE" in upgrade
    assert "kis_mock_dispatch_evidence_retained" in source
    assert "at_app" in upgrade


@pytest.mark.asyncio
async def test_common_release_requires_exact_terminal_and_gate():
    intents = AsyncMock()
    intents.release_if_matches.return_value = 1
    claims = DurableSendClaimAdapter(intents)
    claim = DurableClaim(
        row_id=7, claim_account_scope="scope", idempotency_key="key", side="buy"
    )
    for evidence in (
        TerminalClaimEvidence(),
        TerminalClaimEvidence(lane_native_terminal_evidence=True),
        TerminalClaimEvidence(
            lane_native_terminal_evidence=True, account_position_reconciled=True
        ),
        TerminalClaimEvidence(authoritative_absence_proven=True),
    ):
        with pytest.raises(Exception) as error:
            await claims.release_with_terminal_evidence(claim, evidence)
        assert "terminal_evidence_required" in str(error.value)
        intents.release_if_matches.assert_not_awaited()
    assert (
        await claims.release_with_terminal_evidence(
            claim,
            TerminalClaimEvidence(
                lane_native_terminal_evidence=True,
                account_position_reconciled=True,
                remainder_known=True,
            ),
        )
        == 1
    )
    assert intents.release_if_matches.await_args.kwargs == {
        "account_scope": "scope",
        "row_id": 7,
        "idempotency_key": "key",
        "side": "buy",
    }


@pytest.mark.asyncio
async def test_native_wrong_shape_refused_before_database():
    factory = AsyncMock()
    store = KISMockEvidenceStore(factory, entry=bound_entry())
    with pytest.raises(KISMockEvidenceConflict, match="native_evidence_shape"):
        await store.record_native_evidence(
            None, NativeOrderEvidence("soft_cancel", "key", None)
        )
    factory.assert_not_called()


@pytest.mark.parametrize("setting", [None, "false", "True", "1", " true", "true "])
def test_e3_activation_defaults_off_and_requires_exact_opt_in(setting):
    config = {IDENTITY_ENV: FINGERPRINT}
    if setting is not None:
        config[ACTIVATION_ENV] = setting
    entry = kis_mock_registry_from_environment(environ=config)[0]
    assert entry.activation_status is ActivationStatus.BLOCKED
    assert entry.auto_order_enabled is False and entry.writer is False
    with pytest.raises(LaneGuardError, match="lane_activation_not_enabled"):
        assert_entry_execution_ready(entry)


def test_e3_explicit_on_matching_identity_satisfies_existing_readiness():
    entries = kis_mock_registry_from_environment(
        environ={IDENTITY_ENV: FINGERPRINT, ACTIVATION_ENV: "true"}
    )
    entry = entries[0]
    assert_entry_execution_ready(entry)
    assert entry.activation_status is ActivationStatus.ENABLED
    assert entry.writer is True and entry.auto_order_enabled is True
    assert entry.physical_account_id == FINGERPRINT
    assert entry.policy_binding == E3_ACTIVATION_CONTRACT.policy_binding
    assert entry.missing_bindings == ()
    assert entry.execution_mode == "operator_present_foreground_e3"
    assert entry.scheduler_owner is SchedulerOwner.MANUAL
    assert entry.timing_owner == E3_ACTIVATION_CONTRACT.owner
    assert entry.canary_binding == E3_ACTIVATION_CONTRACT.canary
    assert entry.max_order_notional == 99_999
    assert entry.max_orders_per_session == 1 and entry.max_open_orders == 1
    assert entry.allowed_order_types == ("limit",)
    assert entry.allowed_time_in_force == ("day",)
    assert entry.reconcile_required is True
    assert physical_account_scope_for_entry(entry).claim_account_scope
    assert entries[1:] == CANONICAL_LANE_REGISTRY[1:]
    assert CANONICAL_LANE_REGISTRY[0].writer is False
    assert CANONICAL_LANE_REGISTRY[0].auto_order_enabled is False
    # This is one foreground canary. Opt-in grants no recurring lane status.
    with pytest.raises(LaneGuardError, match="lane_recurring_not_authorized"):
        assert_recurring_authorized(entry, recurring_requested=True)


def test_e3_activation_contract_has_the_approved_per_intent_bounds():
    contract = E3_ACTIVATION_CONTRACT
    assert contract.lane_id == "kr.kis.mock" and contract.account == "kis_mock"
    assert contract.max_quantity_per_intent == 1
    assert contract.max_notional_krw_exclusive == 100_000
    assert contract.max_lifetime_seconds == 30 * 60
    assert contract.max_working_orders_per_intent == 1
    assert contract.max_intents_per_run == 1
    assert contract.owner == "E1 gateway route (POST /execution-intents/children)"
    assert contract.writer == "gateway"
    assert contract.operator_present is True and contract.foreground_only is True
    assert (
        contract.canary
        == "docs/runbooks/execution-intent-gateway.md#run-one-mock-order"
    )


def test_e3_on_without_identity_or_with_mismatched_pin_refused():
    with pytest.raises(LaneGuardError, match="activation_identity_required"):
        kis_mock_registry_from_environment(environ={ACTIVATION_ENV: "true"})
    with pytest.raises(LaneGuardError, match="identity_mismatch"):
        kis_mock_registry_from_environment(
            entries=(bound_entry(), *CANONICAL_LANE_REGISTRY[1:]),
            environ={ACTIVATION_ENV: "true", IDENTITY_ENV: "kismock:v1:" + "b" * 64},
        )


@pytest.mark.parametrize(
    "change",
    [
        {"account_mode": "live"},
        {"account_profile": "live"},
        {"credential_namespace": "KIS_*"},
        {"allowed_hosts": ("openapi.koreainvestment.com:9443",)},
    ],
)
def test_e3_on_with_live_identity_or_transport_refused(change):
    invalid = replace(CANONICAL_LANE_REGISTRY[0], **change)
    with pytest.raises(RegistryStartupError):
        kis_mock_registry_from_environment(
            entries=(invalid, *CANONICAL_LANE_REGISTRY[1:]),
            environ={ACTIVATION_ENV: "true", IDENTITY_ENV: FINGERPRINT},
        )


@pytest.mark.parametrize("keep_identity", [False, True])
def test_e3_turning_setting_off_removes_prior_effective_authority(keep_identity):
    activated = kis_mock_registry_from_environment(
        environ={IDENTITY_ENV: FINGERPRINT, ACTIVATION_ENV: "true"}
    )
    entry = kis_mock_registry_from_environment(
        entries=activated, environ={IDENTITY_ENV: FINGERPRINT} if keep_identity else {}
    )[0]
    assert entry.auto_order_enabled is False and entry.writer is False
    assert entry.activation_status is ActivationStatus.BLOCKED
    expected = bound_entry() if keep_identity else CANONICAL_LANE_REGISTRY[0]
    assert entry == expected
    with pytest.raises(LaneGuardError, match="lane_activation_not_enabled"):
        assert_entry_execution_ready(entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["dispatch", "lineage-ack"])
async def test_native_evidence_cannot_use_coordinator_reserved_keys(key):
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = AsyncMock()
    store = KISMockEvidenceStore(factory, entry=bound_entry())
    store._lineage = AsyncMock(
        side_effect=KISMockEvidenceConflict("lineage_unavailable")
    )
    with pytest.raises(KISMockEvidenceConflict, match="reserved_evidence_key"):
        await store.record_native_evidence(
            None, NativeOrderEvidence("cancel", key, None)
        )
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,payload,dispatch_matches",
    [
        ("reject", {}, False),
        ("ack", {}, False),
        ("ack", {"claim_row_id": "7"}, False),
        ("ack", {"claim_row_id": True}, False),
        ("ack", {"claim_row_id": 8}, False),
        ("ack", {"claim_row_id": 7}, True),
    ],
)
async def test_rediscovery_blocks_even_when_dispatch_matches(
    kind, payload, dispatch_matches
):
    session = AsyncMock()
    sessions = MagicMock()
    sessions.return_value.__aenter__.return_value = session
    store = KISMockEvidenceStore(sessions, entry=bound_entry())
    claim = SimpleNamespace(
        id=7, account_scope=store._scope, idempotency_key="key", side="buy"
    )
    event = SimpleNamespace(
        kind=kind, payload=payload, broker_evidence_key=_key("dispatch")
    )
    session.scalars.side_effect = [[claim]]
    session.scalar.return_value = SimpleNamespace(order_attempt_id="attempt")
    store._attributed_odno = AsyncMock(return_value=None)
    discovered = await store.rediscover_unreleased_claims()
    assert len(discovered) == 1
    assert discovered[0].blocks_account is True
    assert _dispatch_matches_claim(event, claim.id) is dispatch_matches


@pytest.mark.asyncio
async def test_ambiguous_attribution_still_reports_the_exact_blocking_claim():
    session = AsyncMock()
    sessions = MagicMock()
    sessions.return_value.__aenter__.return_value = session
    store = KISMockEvidenceStore(sessions, entry=bound_entry())
    session.scalars.return_value = [
        SimpleNamespace(id=7, idempotency_key="key", side="buy")
    ]
    session.scalar.return_value = SimpleNamespace(order_attempt_id="attempt")
    store._attributed_odno = AsyncMock(
        side_effect=KISMockEvidenceConflict("attributed_odno_ambiguous")
    )
    result = await store.rediscover_unreleased_claims()
    assert result[0].claim.row_id == 7
    assert result[0].broker_order_id is None
    assert result[0].attribution_error == "attributed_odno_ambiguous"
    assert result[0].blocks_account is True


@pytest.mark.asyncio
async def test_malformed_reserved_dispatch_cannot_crash_or_release_claim():
    session = AsyncMock()
    sessions = MagicMock()
    sessions.return_value.__aenter__.return_value = session
    store = KISMockEvidenceStore(sessions, entry=bound_entry())
    store._lineage = AsyncMock(
        return_value=SimpleNamespace(order_attempt_id="attempt", idempotency_key="key")
    )
    session.scalar.return_value = SimpleNamespace(
        payload={
            "terminal": {
                "lane_native_terminal_evidence": True,
                "account_position_reconciled": True,
                "remainder_known": True,
                "authoritative_absence_proven": False,
            }
        }
    )
    session.scalars.return_value = [
        SimpleNamespace(kind="reject", payload={}, broker_evidence_key=_key("dispatch"))
    ]
    claim = DurableClaim(
        row_id=7, claim_account_scope=store._scope, idempotency_key="key", side="buy"
    )
    authority = KISMockRecoveryAuthority(store, session)
    claims = DurableSendClaimAdapter(OrderSendIntentService(session))
    with pytest.raises(
        KISMockEvidenceConflict, match="terminal_claim_binding_mismatch"
    ):
        await store.release_with_recorded_terminal_evidence(
            SimpleNamespace(decision_intent=SimpleNamespace(side="buy")),
            broker_evidence_key="terminal",
            claim=claim,
            claims=claims,
            authority=authority,
        )
    session.execute.assert_not_awaited()


def test_position_baseline_migration_is_additive_and_refuses_retained_downgrade():
    text = (
        Path(__file__).resolve().parents[3]
        / "alembic/versions/20261109_hk2690_kis_pos_base.py"
    ).read_text()
    upgrade, downgrade = text.split("def downgrade", 1)
    assert "CREATE TABLE review.kis_mock_position_baseline" in upgrade
    assert "ALTER TABLE" not in upgrade and "DROP " not in upgrade
    assert "BEFORE UPDATE OR DELETE OR TRUNCATE" in upgrade
    assert "kis_mock_position_baseline_retained" in downgrade
