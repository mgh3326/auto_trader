"""Operator recovery uses exact readback but cannot invent position proof."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.kis_mock_lifecycle_service import KISMockLifecycleService
from app.services.kis_mock_runner.evidence_store import (
    KISMockEvidenceConflict,
    KISMockEvidenceStore,
    KISMockRecoveryLineage,
    RediscoveredKISMockClaim,
)
from app.services.mock_integration.coordination import DurableClaim
from tests.services.mock_integration.test_coordination import _attempt_envelope
from tests.services.mock_integration.test_kis_mock_evidence_store import bound_entry

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
ODNO = "0007"
KST = timezone(timedelta(hours=9))


@pytest.fixture
def recovery():
    _, envelope = _attempt_envelope()
    created = datetime.now(UTC) - timedelta(seconds=2)
    row_time = datetime.now(KST)
    row = {
        "odno": ODNO,
        "ord_dt": row_time.strftime("%Y%m%d"),
        "ord_tmd": row_time.strftime("%H%M%S"),
        "pdno": "005930",
        "sll_buy_dvsn_cd": "02",
        "ord_qty": "1",
        "tot_ccld_qty": "1",
        "rmn_qty": "0",
    }
    claim = RediscoveredKISMockClaim(
        DurableClaim(7, "scope", envelope.order_attempt.idempotency_key, "buy"),
        envelope.order_attempt.order_attempt_id,
        ODNO,
        True,
    )
    store = KISMockEvidenceStore(MagicMock(), entry=bound_entry())
    store.rediscover_unreleased_claims = AsyncMock(return_value=(claim,))
    store.load_recovery_lineage = AsyncMock(
        return_value=KISMockRecoveryLineage(envelope, created)
    )
    recorded = {}

    async def record(envelope, evidence):
        existing = recorded.setdefault(evidence.broker_evidence_key, evidence)
        assert existing == evidence

    store.record_native_evidence = AsyncMock(side_effect=record)
    store.release_with_recorded_terminal_evidence = AsyncMock(
        side_effect=AssertionError("claim release is forbidden without position proof")
    )
    reader = AsyncMock(return_value=row)
    service = KISMockLifecycleService(MagicMock())
    return SimpleNamespace(
        row=row,
        claim=claim,
        store=store,
        reader=reader,
        service=service,
        recorded=recorded,
        created=created,
        claims=MagicMock(),
    )


async def run_recovery(recovery, *, dry_run=False, confirm=True):
    return await recovery.service.recover_dispatch_claims(
        store=recovery.store,
        claims=recovery.claims,
        reader=recovery.reader,
        dry_run=dry_run,
        confirm=confirm,
    )


async def test_exact_terminal_fill_is_recorded_but_retained_without_position_proof(
    recovery,
):
    result = await run_recovery(recovery)
    assert result["account_blocked"] is True
    assert result["claims"] == [
        {
            "claim_id": 7,
            "status": "retained",
            "reason": "position proof unavailable",
            "disposition": "retained: position proof unavailable",
            "readback": "terminal_fill",
            "evidence": "recorded",
        }
    ]
    recovery.reader.assert_awaited_once_with(ODNO, recovery.created)
    evidence = next(iter(recovery.recorded.values()))
    assert evidence.kind == "terminal_reconciliation"
    assert evidence.broker_order_id == evidence.readback_order_id == ODNO
    assert evidence.terminal.lane_native_terminal_evidence is True
    assert evidence.terminal.remainder_known is True
    assert evidence.terminal.account_position_reconciled is False
    recovery.store.release_with_recorded_terminal_evidence.assert_not_awaited()


async def test_second_run_has_one_immutable_event_and_no_release(recovery):
    assert (await run_recovery(recovery))["claims"][0]["status"] == "retained"
    # Equivalent broker quantity spelling is the same evidence, not a new event.
    recovery.row.update(ord_qty="01", tot_ccld_qty="01", rmn_qty="00")
    assert (await run_recovery(recovery))["claims"][0]["status"] == "retained"
    assert len(recovery.recorded) == 1
    assert recovery.store.record_native_evidence.await_count == 2
    recovery.store.release_with_recorded_terminal_evidence.assert_not_awaited()


async def test_broker_payload_cannot_supply_position_proof(recovery):
    recovery.row.update(
        account_position_reconciled=True,
        authoritative_absence_proven=True,
        position_proof=True,
    )
    result = await run_recovery(recovery)
    assert result["claims"][0]["disposition"] == "retained: position proof unavailable"
    evidence = next(iter(recovery.recorded.values()))
    assert evidence.terminal.account_position_reconciled is False
    assert evidence.terminal.authoritative_absence_proven is False
    recovery.store.release_with_recorded_terminal_evidence.assert_not_awaited()


async def test_mismatched_odno_is_uncertain_and_blocks(recovery):
    recovery.row["odno"] = "7"
    result = await run_recovery(recovery)
    assert result["account_blocked"] is True
    assert result["claims"][0]["claim_id"] == 7
    assert result["claims"][0]["readback"] == "uncertain"
    assert not recovery.recorded
    recovery.reader.assert_awaited_once_with(ODNO, recovery.created)
    recovery.store.release_with_recorded_terminal_evidence.assert_not_awaited()


@pytest.mark.parametrize("odno", [None, "", " "])
async def test_missing_or_blank_ack_odno_never_calls_reader(recovery, odno):
    claim = RediscoveredKISMockClaim(
        recovery.claim.claim, recovery.claim.order_attempt_id, odno, True
    )
    recovery.store.rediscover_unreleased_claims.return_value = (claim,)
    result = await run_recovery(recovery)
    assert result["account_blocked"] is True
    assert result["claims"][0]["claim_id"] == 7
    assert result["claims"][0]["readback_reason"] == "attributed_odno_unavailable"
    recovery.reader.assert_not_awaited()
    recovery.store.release_with_recorded_terminal_evidence.assert_not_awaited()


@pytest.mark.parametrize("error", [TimeoutError("timeout"), RuntimeError("private")])
async def test_reader_errors_are_uncertain_and_do_not_release(recovery, error):
    recovery.reader.side_effect = error
    result = await run_recovery(recovery)
    assert result["account_blocked"] is True
    assert result["claims"][0]["readback"] == "uncertain"
    assert result["claims"][0]["readback_reason"] == "recovery_error"
    assert "private" not in str(result)
    assert not recovery.recorded
    recovery.store.release_with_recorded_terminal_evidence.assert_not_awaited()


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"tot_ccld_qty": "0", "rmn_qty": "1"}, "accepted"),
        ({"tot_ccld_qty": "0", "rmn_qty": "0"}, "uncertain"),
        ({"ord_qty": "2", "tot_ccld_qty": "1", "rmn_qty": "1"}, "uncertain"),
        ({"rmn_qty": ""}, "uncertain"),
        ({"pdno": "000660"}, "uncertain"),
        ({"ord_dt": "20000101"}, "uncertain"),
    ],
)
async def test_nonterminal_or_conflicting_readback_never_releases(
    recovery, change, expected
):
    recovery.row.update(change)
    result = await run_recovery(recovery)
    assert result["account_blocked"] is True
    assert result["claims"][0]["readback"] == expected
    recovery.store.release_with_recorded_terminal_evidence.assert_not_awaited()


async def test_partial_fill_is_recorded_and_retained(recovery):
    _, envelope = _attempt_envelope(plan_overrides={"quantity": Decimal("2")})
    claim = RediscoveredKISMockClaim(
        DurableClaim(7, "scope", envelope.order_attempt.idempotency_key, "buy"),
        envelope.order_attempt.order_attempt_id,
        ODNO,
        True,
    )
    recovery.store.rediscover_unreleased_claims.return_value = (claim,)
    recovery.store.load_recovery_lineage.return_value = KISMockRecoveryLineage(
        envelope, recovery.created
    )
    recovery.row.update(ord_qty="2", tot_ccld_qty="1", rmn_qty="1")
    result = await run_recovery(recovery)
    assert result["claims"][0]["readback"] == "partial_fill"
    assert next(iter(recovery.recorded.values())).kind == "partial_fill"
    assert result["account_blocked"] is True
    recovery.store.release_with_recorded_terminal_evidence.assert_not_awaited()


async def test_preview_has_no_evidence_writes_and_confirm_is_required(recovery):
    result = await run_recovery(recovery, dry_run=True, confirm=False)
    assert result["claims"][0]["readback"] == "terminal_fill"
    recovery.store.record_native_evidence.assert_not_awaited()
    with pytest.raises(ValueError, match="confirm_required"):
        await run_recovery(recovery, dry_run=False, confirm=False)
    recovery.store.release_with_recorded_terminal_evidence.assert_not_awaited()


async def test_rediscovery_failure_blocks_without_claiming_exact_ids(recovery):
    recovery.store.rediscover_unreleased_claims.side_effect = RuntimeError("db")
    result = await run_recovery(recovery)
    assert result["account_blocked"] is True
    assert result["status"] == "uncertain"
    assert result["claims"] == []
    recovery.reader.assert_not_awaited()


async def test_recovery_lineage_reconstructs_exact_current_claim(recovery):
    session = AsyncMock()
    sessions = MagicMock()
    sessions.return_value.__aenter__.return_value = session
    store = KISMockEvidenceStore(sessions, entry=bound_entry())
    envelope = (await recovery.store.load_recovery_lineage(recovery.claim)).envelope
    claim = RediscoveredKISMockClaim(
        DurableClaim(7, store._scope, envelope.order_attempt.idempotency_key, "buy"),
        envelope.order_attempt.order_attempt_id,
        ODNO,
        True,
    )
    row = SimpleNamespace(
        physical_account_id=bound_entry().physical_account_id,
        claim_account_scope=store._scope,
        idempotency_key=claim.claim.idempotency_key,
        initial_envelope=envelope.model_dump(mode="json"),
        created_at=recovery.created,
    )
    current = SimpleNamespace(
        account_scope=store._scope,
        idempotency_key=claim.claim.idempotency_key,
        side="buy",
    )
    session.get.side_effect = [row, current]
    lineage = await store.load_recovery_lineage(claim)
    assert lineage.envelope == envelope
    assert lineage.created_at == recovery.created

    current.side = "sell"
    session.get.side_effect = [row, current]
    with pytest.raises(KISMockEvidenceConflict, match="recovery_claim_binding"):
        await store.load_recovery_lineage(claim)


async def test_manual_reconciler_uses_mock_exact_order_reader_and_closes_client(
    recovery, monkeypatch
):
    from app.mcp_server.tooling import kis_mock_ledger
    from app.services.brokers import kis
    from app.services.kis_mock_runner import evidence_store, registry_binding, singleton

    entry = bound_entry()
    view = SimpleNamespace(
        _is_mock=True,
        kis_base_url="https://openapivts.koreainvestment.com:29443",
        kis_app_key="fake",
        kis_account_no="fake",
    )
    client = SimpleNamespace(
        _is_mock_client=True,
        _settings=view,
        inquire_daily_order_domestic=AsyncMock(return_value=[recovery.row]),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        kis, "KISClient", lambda *, is_mock: client if is_mock else None
    )
    monkeypatch.setattr(
        registry_binding, "kis_mock_registry_from_environment", lambda: (entry,)
    )
    monkeypatch.setattr(
        evidence_store, "KISMockEvidenceStore", lambda *a, **k: recovery.store
    )
    monkeypatch.setattr(
        singleton, "kis_mock_account_fingerprint", lambda **k: entry.physical_account_id
    )
    result = await kis_mock_ledger._recover_mock_dispatch_claims(
        MagicMock(), dry_run=False, confirm=True
    )
    assert result["claims"][0]["readback"] == "terminal_fill"
    assert result["claims"][0]["reason"] == "position proof unavailable"
    kwargs = client.inquire_daily_order_domestic.await_args.kwargs
    assert kwargs["order_number"] == ODNO and kwargs["is_mock"] is True
    assert kwargs["start_date"] == recovery.created.astimezone(KST).strftime("%Y%m%d")
    client.close.assert_awaited_once()
    recovery.store.release_with_recorded_terminal_evidence.assert_not_awaited()
    recovery.store.rediscover_unreleased_claims.return_value = ()
    result = await kis_mock_ledger._recover_mock_dispatch_claims(
        MagicMock(), dry_run=True, confirm=False
    )
    assert result["status"] == "no_unreleased_claims"
    assert result["account_blocked"] is False
    client.inquire_daily_order_domestic.assert_awaited_once()


async def test_mismatched_mock_identity_never_reads_broker(recovery, monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger
    from app.services.brokers import kis
    from app.services.kis_mock_runner import evidence_store, registry_binding, singleton

    entry = bound_entry()
    client = SimpleNamespace(
        _is_mock_client=True,
        _settings=SimpleNamespace(
            _is_mock=True,
            kis_base_url="https://openapivts.koreainvestment.com:29443",
            kis_app_key="fake",
            kis_account_no="fake",
        ),
        inquire_daily_order_domestic=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(kis, "KISClient", lambda **k: client)
    monkeypatch.setattr(
        registry_binding, "kis_mock_registry_from_environment", lambda: (entry,)
    )
    monkeypatch.setattr(
        evidence_store, "KISMockEvidenceStore", lambda *a, **k: recovery.store
    )
    monkeypatch.setattr(
        singleton, "kis_mock_account_fingerprint", lambda **k: "kismock:v1:" + "b" * 64
    )
    result = await kis_mock_ledger._recover_mock_dispatch_claims(
        MagicMock(), dry_run=False, confirm=True
    )
    assert result["claims"][0]["readback"] == "uncertain"
    assert result["account_blocked"] is True
    client.inquire_daily_order_domestic.assert_not_awaited()
    client.close.assert_awaited_once()
    recovery.store.rediscover_unreleased_claims.return_value = ()
    result = await kis_mock_ledger._recover_mock_dispatch_claims(
        MagicMock(), dry_run=False, confirm=True
    )
    assert result["status"] == "uncertain"
    assert result["account_blocked"] is True
    assert result["reason"] == "recovery_identity_unavailable"
    client.inquire_daily_order_domestic.assert_not_awaited()


async def test_only_unscoped_manual_entry_invokes_claim_recovery(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr(kis_mock_ledger, "_order_session_factory", lambda: factory)
    monkeypatch.setattr(
        kis_mock_ledger,
        "run_kis_mock_reconciliation",
        AsyncMock(return_value={"success": True}),
    )
    recovery_call = AsyncMock(return_value={"status": "blocked"})
    monkeypatch.setattr(kis_mock_ledger, "_recover_mock_dispatch_claims", recovery_call)
    result = await kis_mock_ledger.kis_mock_reconciliation_run_impl(
        dry_run=False, confirm=True
    )
    assert result["dispatch_recovery"]["status"] == "blocked"
    recovery_call.assert_awaited_once_with(session, dry_run=False, confirm=True)
    recovery_call.reset_mock()
    await kis_mock_ledger.kis_mock_reconciliation_run_impl(market="kr")
    recovery_call.assert_not_awaited()
    denied = await kis_mock_ledger.kis_mock_reconciliation_run_impl(dry_run=False)
    assert denied["error"] == "confirm_required"
    recovery_call.assert_not_awaited()


async def test_recovery_never_calls_a_direct_claim_release():
    path = Path(__file__).resolve().parents[3] / "app/services/kis_mock_recovery.py"
    tree = ast.parse(path.read_text())
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "release_if_matches" not in calls
    assert "release" not in calls
    assert "release_with_recorded_terminal_evidence" in calls
