from __future__ import annotations

import ast
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError

from app.core.db import AsyncSessionLocal
from app.models.order_proposals import (
    OrderProposal,
    OrderProposalApprovalAuditEvent,
    OrderProposalApprovalDispatchAttempt,
)
from app.services.order_proposals.approval_record import (
    APPROVAL_RECORD_EVENT_TYPES,
    ApprovalRecordEventType,
    ApprovalRecordTimingSource,
    approval_nonce_digest,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalPublication,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.service import OrderProposalsService, RungInput
from app.telegram_contract import TelegramMethodResult

_REPO = Path(__file__).resolve().parents[3]
_MIGRATION = _REPO / "alembic/versions/20260815_rob1255_approval_audit.py"


async def _group(db_session, *, now: datetime | None = None):
    observed = now or datetime.now(UTC)
    return await OrderProposalsService(db_session).create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="audit-test",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("100"), None)],
        valid_until=observed + timedelta(hours=3),
        now=observed,
    )


def _publication(*, message_id: int, payload_chars: int) -> ApprovalPublication:
    return ApprovalPublication.published(
        payload_chars=payload_chars,
        method_result=TelegramMethodResult(
            ok=True,
            message_id=message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=payload_chars,
        ),
    )


def _migration_application_dml(source: str) -> list[str]:
    tree = ast.parse(source)
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "bulk_insert":
            findings.append("bulk_insert")
            continue
        if node.func.attr != "execute" or not node.args:
            continue
        sql = "".join(
            item.value
            for item in ast.walk(node.args[0])
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
        if re.match(
            r"^\s*(insert\s+into|update\s+\w+\s+set|delete\s+from)\b",
            sql,
            re.I,
        ):
            findings.append(sql)
    return findings


@pytest.mark.unit
def test_approval_audit_schema_is_additive_typed_and_nonce_safe():
    event = OrderProposalApprovalAuditEvent.__table__
    proposal = OrderProposal.__table__
    attempt = OrderProposalApprovalDispatchAttempt.__table__

    assert event.schema == "review"
    assert {
        "event_type",
        "occurred_at",
        "observed_at",
        "timing_source",
        "actor_id",
        "nonce_consumed",
        "proposal_id",
        "root_proposal_id",
        "rung_indices",
        "card_message_id",
        "dispatch_attempt_id",
        "successor_proposal_id",
    } <= set(event.columns.keys())
    assert set(APPROVAL_RECORD_EVENT_TYPES) == {
        "first_stage_approved",
        "second_stage_dispatched",
        "second_stage_clicked",
        "expired",
        "superseded",
    }
    assert "approval_nonce" not in event.columns
    assert approval_nonce_digest("secret-nonce") != "secret-nonce"

    # Legacy last-value compatibility remains present and unchanged in place.
    assert {
        "approval_dispatch_state",
        "approval_dispatch_attempt_id",
        "approval_dispatch_attempted_at",
        "approval_dispatch_payload_chars",
    } <= set(proposal.columns.keys())
    assert attempt.name == "order_proposal_approval_dispatch_attempts"


@pytest.mark.unit
def test_approval_audit_migration_is_additive_single_head_and_append_only():
    source = _MIGRATION.read_text(encoding="utf-8")
    assert _migration_application_dml(source) == []
    assert "trg_order_proposal_approval_audit_events_append_only" in source
    assert "trg_order_proposal_approval_audit_events_truncate_append_only" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "BEFORE TRUNCATE" in source

    config = Config(str(_REPO / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["20260815_rob1255_audit"]
    revision = scripts.get_revision("20260815_rob1255_audit")
    assert revision is not None
    assert revision.down_revision == "20260814_lcapprove_b1"


@pytest.mark.asyncio
async def test_approval_audit_events_reject_update_delete_and_truncate(
    db_session,
):
    now = datetime.now(UTC)
    group = await _group(db_session, now=now)
    service = OrderProposalsService(db_session)
    event = await service.append_approval_audit_event_best_effort(
        group=group,
        rung_indices=[0],
        event_type=ApprovalRecordEventType.FIRST_STAGE_APPROVED,
        event_result="accepted",
        occurred_at=now,
        observed_at=now,
        timing_source=ApprovalRecordTimingSource.TELEGRAM_CALLBACK_RECEIVED,
        actor_kind="telegram_user",
        actor_id="777",
        channel="telegram",
        nonce="raw-nonce",
        nonce_consumed=True,
    )
    assert event is not None
    event_id = event.event_id
    await db_session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(OrderProposalApprovalAuditEvent)
                .where(OrderProposalApprovalAuditEvent.event_id == event_id)
                .values(event_result="changed")
            )
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(
                delete(OrderProposalApprovalAuditEvent).where(
                    OrderProposalApprovalAuditEvent.event_id == event_id
                )
            )
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(
                text("TRUNCATE review.order_proposal_approval_audit_events")
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_audit_insert_failure_keeps_approval_transaction_usable(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    group = await _group(db_session, now=now)
    service = OrderProposalsService(db_session)

    async def fail_insert(**_cols):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(service._repo, "insert_approval_audit_event", fail_insert)
    event = await service.append_approval_audit_event_best_effort(
        group=group,
        rung_indices=[0],
        event_type=ApprovalRecordEventType.SECOND_STAGE_CLICKED,
        event_result="accepted",
        occurred_at=now,
        observed_at=now,
        timing_source=ApprovalRecordTimingSource.TELEGRAM_CALLBACK_RECEIVED,
        actor_kind="telegram_user",
        actor_id="777",
        channel="telegram",
        nonce="second-nonce",
        nonce_consumed=True,
    )
    assert event is None

    await service.record_approval(group.proposal_id, telegram_user_id="777", now=now)
    await db_session.commit()
    refreshed, _rungs = await service.get_proposal(group.proposal_id)
    assert refreshed.approved_by_telegram_user_id == "777"
    assert refreshed.approved_at == now


@pytest.mark.asyncio
async def test_dispatch_history_preserves_jup_first_card_after_52_minute_second_card(
    db_session,
):
    first_at = datetime(2026, 8, 15, 0, 0, tzinfo=UTC)
    group = await _group(db_session, now=first_at)
    service = OrderProposalsService(db_session)

    first_nonce = "first-stage"
    await service.set_approval_nonce(group.proposal_id, first_nonce)
    first_id = uuid.uuid4()
    first_binding = build_proposal_dispatch_binding(
        proposal_id=group.proposal_id,
        nonce=first_nonce,
        attempt_id=first_id,
        card_kind=ApprovalCardKind.MANUAL,
        current_membership_revision=group.approval_dispatch_membership_revision,
    )
    await service.start_approval_dispatch(
        group.proposal_id,
        attempt_id=first_id,
        binding=first_binding,
        now=first_at,
        payload_chars=5000,
        context_message_count=0,
    )
    await service.finish_approval_dispatch(
        group.proposal_id,
        attempt_id=first_id,
        publication=_publication(message_id=555, payload_chars=5000),
        chat_id="42",
        now=first_at,
    )

    second_at = first_at + timedelta(minutes=52)
    second_nonce = "second-stage"
    await service.set_approval_nonce(group.proposal_id, second_nonce)
    refreshed, _rungs = await service.get_proposal(group.proposal_id)
    second_id = uuid.uuid4()
    second_binding = build_proposal_dispatch_binding(
        proposal_id=group.proposal_id,
        nonce=second_nonce,
        attempt_id=second_id,
        card_kind=ApprovalCardKind.LOSS_CUT_CONFIRMATION,
        current_membership_revision=refreshed.approval_dispatch_membership_revision,
    )
    await service.start_approval_dispatch(
        group.proposal_id,
        attempt_id=second_id,
        binding=second_binding,
        now=second_at,
        payload_chars=397,
        context_message_count=0,
    )
    await service.finish_approval_dispatch(
        group.proposal_id,
        attempt_id=second_id,
        publication=_publication(message_id=555, payload_chars=397),
        chat_id="42",
        now=second_at,
    )
    await db_session.commit()

    history = await service.list_approval_dispatch_history(group.proposal_id)
    assert [row.attempt_id for row in history] == [first_id, second_id]
    assert [row.payload_chars for row in history] == [5000, 397]
    assert [row.attempted_at for row in history] == [first_at, second_at]
    assert history[1].attempted_at - history[0].attempted_at == timedelta(minutes=52)
    assert [row.state for row in history] == ["sent_superseded", "sent_current"]
    assert [row.card_kind for row in history] == [
        "manual",
        "loss_cut_confirmation",
    ]
    assert history[0].message_id == history[1].message_id == 555

    latest, _ = await service.get_proposal(group.proposal_id)
    assert latest.approval_dispatch_attempt_id == second_id
    assert latest.approval_dispatch_payload_chars == 397
    assert latest.approval_dispatch_card_kind == "loss_cut_confirmation"


@pytest.mark.asyncio
async def test_supersede_keeps_prior_approval_facts_without_inheriting_approval(
    db_session,
):
    now = datetime.now(UTC)
    old = await _group(db_session, now=now)
    service = OrderProposalsService(db_session)
    await service.set_approval_nonce(old.proposal_id, "old-nonce")
    await service.append_approval_audit_event_best_effort(
        group=old,
        rung_indices=[0],
        event_type=ApprovalRecordEventType.FIRST_STAGE_APPROVED,
        event_result="accepted",
        occurred_at=now,
        observed_at=now,
        timing_source=ApprovalRecordTimingSource.TELEGRAM_CALLBACK_RECEIVED,
        actor_kind="telegram_user",
        actor_id="777",
        channel="telegram",
        nonce="old-nonce",
        nonce_consumed=True,
    )
    replacement = await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="audit-test",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("99"), None)],
        valid_until=now + timedelta(hours=3),
        supersedes_proposal_id=old.proposal_id,
        now=now + timedelta(seconds=1),
    )
    await db_session.commit()

    old_after, _ = await service.get_proposal(old.proposal_id)
    replacement_after, _ = await service.get_proposal(replacement.proposal_id)
    assert old_after.lifecycle_state == "superseded"
    assert old_after.approval_nonce == "old-nonce"
    assert old_after.approval_nonce_used_at == now + timedelta(seconds=1)
    assert replacement_after.approval_nonce is None
    assert replacement_after.approved_at is None

    lineage = await service.list_approval_audit_events(replacement.proposal_id)
    assert [row.event_type for row in lineage] == [
        "first_stage_approved",
        "superseded",
    ]
    assert lineage[0].proposal_id == old.proposal_id
    assert lineage[1].successor_proposal_id == replacement.proposal_id
    assert lineage[1].nonce_consumed is False
    assert lineage[1].nonce_invalidated is True
