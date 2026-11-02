"""Mock-only boundaries and append-only schema contract; no DB or broker I/O."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import UniqueConstraint

from app.models.kis_mock_dispatch_evidence import KISMockDispatchEvidence
from app.services.kis_mock_runner.evidence_store import (
    KISMockEvidenceConflict,
    KISMockEvidenceStore,
    NativeOrderEvidence,
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
    LaneGuardError,
    MissingBinding,
    assert_entry_execution_ready,
)

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
