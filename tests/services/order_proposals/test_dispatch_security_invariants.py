from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.models.order_proposals import (
    OrderProposal,
    OrderProposalApprovalDispatchAttempt,
    OrderProposalRung,
)
from app.monitoring.trade_notifier import transports
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals.approval_message import (
    ApprovalDispatchMessages,
    build_batch_approval_message,
)
from app.services.order_proposals.dispatch import (
    dispatch_proposal,
    publish_approval_messages,
    send_proposal_for_approval,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalDispatchState,
    ApprovalPublication,
    CallbackEnvelope,
    CallbackGateSnapshot,
    DispatchBinding,
    TelegramDispatchResult,
    assert_callback_gate,
    build_membership_digest,
)
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.repository import OrderProposalRepository
from app.services.order_proposals.revalidation import RungOutcome
from app.services.order_proposals.service import OrderProposalsService
from app.services.order_proposals.telegram_callback import (
    _handle_approve,
    _handle_auto_veto,
    _handle_loss_cut_first_click,
)
from app.telegram_contract import (
    TelegramErrorClassification,
    TelegramMethodResult,
    telegram_text_length,
)

NOW = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
PROPOSAL_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
ATTEMPT_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
DIGEST = "AbCdEf0123_-"


def _service_with_repo(repo: object, *, session: object | None = None):
    service = object.__new__(OrderProposalsService)
    service._repo = repo
    service._session = session or SimpleNamespace(execute=AsyncMock())
    return service


def _callback(
    *,
    action: str = "op",
    attempt_id: uuid.UUID = ATTEMPT_ID,
    revision: int = 1,
    digest: str = DIGEST,
    nonce: str = "safe-nonce",
    subject_short: str | None = None,
) -> CallbackEnvelope:
    return CallbackEnvelope(
        action=action,
        subject_short=subject_short or str(PROPOSAL_ID)[:8],
        attempt_id=attempt_id,
        membership_revision=revision,
        membership_digest=digest,
        nonce=nonce,
    )


def _proposal_digest(
    nonce: str | None,
    *,
    revision: int = 1,
    card_kind: ApprovalCardKind = ApprovalCardKind.MANUAL,
) -> str:
    return build_membership_digest(
        card_kind=card_kind,
        membership_revision=revision,
        members=[
            {
                "proposal_id": str(PROPOSAL_ID),
                "approval_nonce": nonce,
            }
        ],
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raw_telegram_description_is_discarded_everywhere(caplog) -> None:
    markers = (
        "https://api.telegram.org/bot123:SECRET/sendMessage",
        "chat=998877",
        "THESIS_MARKER",
        "callback_data=op:11111111:NONCE_MARKER",
        "reply_markup=INLINE_MARKER",
    )
    response = MagicMock(spec=httpx.Response)
    response.status_code = 400
    response.json.return_value = {
        "ok": False,
        "error_code": 400,
        "description": " | ".join(markers),
    }
    client = SimpleNamespace(post=AsyncMock(return_value=response))

    with caplog.at_level(logging.ERROR):
        result = await transports.send_telegram_message(
            http_client=client,
            bot_token="request-token",
            chat_id="998877",
            text="THESIS_MARKER",
            reply_markup={"callback_data": "NONCE_MARKER"},
        )

    assert result.error_classification is (
        TelegramErrorClassification.UNKNOWN_TELEGRAM_ERROR
    )
    assert not hasattr(result, "description")
    assert "telegram_description" not in (
        column.name for column in OrderProposalApprovalDispatchAttempt.__table__.columns
    )
    rendered_records = "\n".join(
        f"{record.getMessage()} {record.__dict__}" for record in caplog.records
    )
    assert all(marker not in rendered_records for marker in markers)
    caller_result = TelegramDispatchResult.from_publication(
        ApprovalPublication.failed(
            payload_chars=result.payload_chars,
            failure_code="approval_card_dispatch_failed",
            method_result=result,
        ),
        state=ApprovalDispatchState.FAILED,
    ).as_dict()
    assert all(marker not in str(caller_result) for marker in markers)
    assert "description" not in caller_result


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("units", "expected_ok", "expected_calls"),
    [(4095, True, 1), (4096, True, 1), (4097, False, 0)],
)
async def test_edit_message_uses_same_utf16_preflight(
    units: int, expected_ok: bool, expected_calls: int
) -> None:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.json.return_value = {"ok": True, "result": True}
    client = SimpleNamespace(post=AsyncMock(return_value=response))

    result = await transports.edit_message_text(
        http_client=client,
        bot_token="token",
        chat_id="chat",
        message_id=1,
        text="가" * units,
    )

    assert result.ok is expected_ok
    assert client.post.await_count == expected_calls
    if units == 4097:
        assert result.failure_code == "telegram_payload_too_long"
        assert result.error_classification is (
            TelegramErrorClassification.PAYLOAD_TOO_LONG
        )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_code", "classification"),
    [
        (400, 400, TelegramErrorClassification.UNKNOWN_TELEGRAM_ERROR),
        (429, 429, TelegramErrorClassification.UNKNOWN_TELEGRAM_ERROR),
        (500, 500, TelegramErrorClassification.UNKNOWN_TELEGRAM_ERROR),
        (503, 503, TelegramErrorClassification.UNKNOWN_TELEGRAM_ERROR),
    ],
)
async def test_edit_failure_is_structured_without_remote_text(
    status: int,
    error_code: int,
    classification: TelegramErrorClassification,
) -> None:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.json.return_value = {
        "ok": False,
        "error_code": error_code,
        "description": "NONCE THESIS CHAT CALLBACK REPLY_MARKUP",
    }
    client = SimpleNamespace(post=AsyncMock(return_value=response))

    result = await transports.edit_message_text(
        http_client=client,
        bot_token="token",
        chat_id="chat",
        message_id=1,
        text="safe",
    )

    assert result.ok is False
    assert result.status_code == status
    assert result.error_code == error_code
    assert result.error_classification is classification
    assert not hasattr(result, "description")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edit_timeout_is_structured() -> None:
    client = SimpleNamespace(post=AsyncMock(side_effect=TimeoutError("SECRET")))
    result = await transports.edit_message_text(
        http_client=client,
        bot_token="token",
        chat_id="chat",
        message_id=1,
        text="safe",
    )
    assert result.ok is False
    assert result.failure_code == "telegram_transport_error"
    assert result.error_classification is TelegramErrorClassification.TRANSPORT_ERROR


@pytest.mark.unit
@pytest.mark.parametrize(
    "state",
    [
        ApprovalDispatchState.PENDING,
        ApprovalDispatchState.FAILED,
        ApprovalDispatchState.PARTIAL_FAILED,
        ApprovalDispatchState.SENT_SUPERSEDED,
        ApprovalDispatchState.FAILED_SUPERSEDED,
    ],
)
def test_common_gate_rejects_every_non_current_state(
    state: ApprovalDispatchState,
) -> None:
    snapshot = CallbackGateSnapshot(
        subject_short=str(PROPOSAL_ID)[:8],
        state=state,
        attempt_id=ATTEMPT_ID,
        card_kind=ApprovalCardKind.MANUAL,
        membership_revision=1,
        membership_digest=DIGEST,
        nonce="safe-nonce",
        nonce_used=False,
    )
    with pytest.raises(ValueError, match=f"approval_dispatch_{state.value}"):
        assert_callback_gate(snapshot=snapshot, callback=_callback())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("card_kind", "action"),
    [
        (ApprovalCardKind.MANUAL, "vc"),
        (ApprovalCardKind.AUTO_VETO, "op"),
        (ApprovalCardKind.LOSS_CUT_CONFIRMATION, "op"),
        (ApprovalCardKind.BATCH, "dn"),
    ],
)
def test_common_gate_rejects_action_for_wrong_card_kind(
    card_kind: ApprovalCardKind, action: str
) -> None:
    snapshot = CallbackGateSnapshot(
        subject_short=str(PROPOSAL_ID)[:8],
        state=ApprovalDispatchState.SENT_CURRENT,
        attempt_id=ATTEMPT_ID,
        card_kind=card_kind,
        membership_revision=1,
        membership_digest=DIGEST,
        nonce="safe-nonce",
        nonce_used=False,
    )
    with pytest.raises(ValueError, match="approval_card_action_mismatch"):
        assert_callback_gate(snapshot=snapshot, callback=_callback(action=action))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nonce_replacement_invalidates_previous_published_snapshot() -> None:
    old_digest = _proposal_digest("old-nonce")
    group = OrderProposal(
        id=1,
        proposal_id=PROPOSAL_ID,
        root_proposal_id=PROPOSAL_ID,
        revision=1,
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="probe",
        lifecycle_state="proposed",
        approval_nonce="old-nonce",
        approval_dispatch_state=ApprovalDispatchState.SENT_CURRENT.value,
        approval_dispatch_attempt_id=ATTEMPT_ID,
        approval_dispatch_card_kind=ApprovalCardKind.MANUAL.value,
        approval_dispatch_membership_revision=1,
        approval_dispatch_membership_digest=old_digest,
        approval_dispatch_published_at=NOW,
    )

    class Repo:
        async def get_group_by_proposal_id(self, proposal_id, *, for_update=False):
            return group

        async def update_group(self, target, **fields):
            for key, value in fields.items():
                setattr(target, key, value)
            return target

    service = _service_with_repo(Repo())
    await service.set_approval_nonce(PROPOSAL_ID, "new-nonce")

    assert group.approval_dispatch_state == ApprovalDispatchState.FAILED.value
    assert group.approval_dispatch_published_at is None
    assert group.approval_dispatch_failure_code == "approval_dispatch_snapshot_missing"
    with pytest.raises(OrderProposalError, match="approval_dispatch_failed"):
        await service.consume_published_proposal_callback(
            PROPOSAL_ID,
            callback=_callback(nonce="new-nonce"),
            now=NOW,
        )
    assert group.approval_nonce_used_at is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_start_rejects_binding_for_different_nonce_snapshot() -> None:
    group = OrderProposal(
        id=1,
        proposal_id=PROPOSAL_ID,
        root_proposal_id=PROPOSAL_ID,
        revision=1,
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="probe",
        lifecycle_state="proposed",
        approval_nonce="new-nonce",
        approval_dispatch_membership_revision=1,
    )

    class Repo:
        async def get_group_by_proposal_id(self, proposal_id, *, for_update=False):
            return group

        async def insert_approval_dispatch_attempt(self, **fields):
            raise AssertionError("invalid binding must not create an attempt")

    service = _service_with_repo(Repo())
    with pytest.raises(OrderProposalError, match="approval_membership_digest_invalid"):
        await service.start_approval_dispatch(
            PROPOSAL_ID,
            attempt_id=ATTEMPT_ID,
            binding=DispatchBinding(
                attempt_id=ATTEMPT_ID,
                card_kind=ApprovalCardKind.MANUAL,
                membership_revision=2,
                membership_digest=DIGEST,
            ),
            now=NOW,
            payload_chars=10,
            context_message_count=0,
        )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        ApprovalDispatchState.PENDING,
        ApprovalDispatchState.FAILED,
        ApprovalDispatchState.PARTIAL_FAILED,
        ApprovalDispatchState.SENT_SUPERSEDED,
        ApprovalDispatchState.FAILED_SUPERSEDED,
    ],
)
async def test_auto_veto_non_current_card_never_consumes_or_calls_cancel(
    state: ApprovalDispatchState,
) -> None:
    group = OrderProposal(
        id=1,
        proposal_id=PROPOSAL_ID,
        root_proposal_id=PROPOSAL_ID,
        revision=1,
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="probe",
        lifecycle_state="submitted",
        source_asof={"auto_approved": {"policy_version": "probe"}},
        approval_nonce="safe-nonce",
        approval_dispatch_state=state.value,
        approval_dispatch_attempt_id=ATTEMPT_ID,
        approval_dispatch_card_kind=ApprovalCardKind.AUTO_VETO.value,
        approval_dispatch_membership_revision=1,
        approval_dispatch_membership_digest=DIGEST,
    )
    rung = OrderProposalRung(
        id=1,
        proposal_pk=1,
        rung_index=0,
        side="buy",
        quantity=1,
        state="resting",
        broker_order_id="broker-1",
    )

    class Repo:
        updates = 0

        async def get_group_by_proposal_id(self, proposal_id, *, for_update=False):
            return group

        async def list_rungs(self, proposal_pk):
            return [rung]

        async def update_group(self, target, **fields):
            self.updates += 1
            for key, value in fields.items():
                setattr(target, key, value)
            return target

    repo = Repo()
    service = _service_with_repo(repo)
    session = SimpleNamespace(commit=AsyncMock())
    cancel_fn = AsyncMock()
    fetch_fn = AsyncMock()
    result = await _handle_auto_veto(
        session=session,
        service=service,
        proposal_id=PROPOSAL_ID,
        callback=_callback(action="vc"),
        now=NOW,
        notifier=SimpleNamespace(),
        chat_id="chat",
        message_id=7,
        telegram_user_id="operator",
        cancel_fn=cancel_fn,
        fetch_fn=fetch_fn,
    )

    assert result["handled"] is False
    assert result["reason"] == f"approval_dispatch_{state.value}"
    assert group.approval_nonce_used_at is None
    assert repo.updates == 0
    cancel_fn.assert_not_awaited()
    fetch_fn.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_crash_pending_manual_card_cannot_mutate_or_revalidate() -> None:
    group = OrderProposal(
        id=1,
        proposal_id=PROPOSAL_ID,
        root_proposal_id=PROPOSAL_ID,
        revision=1,
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="probe",
        lifecycle_state="proposed",
        approval_nonce="safe-nonce",
        approval_dispatch_state=ApprovalDispatchState.PENDING.value,
        approval_dispatch_attempt_id=ATTEMPT_ID,
        approval_dispatch_card_kind=ApprovalCardKind.MANUAL.value,
        approval_dispatch_membership_revision=1,
        approval_dispatch_membership_digest=DIGEST,
    )

    class Repo:
        updates = 0

        async def get_group_by_proposal_id(self, proposal_id, *, for_update=False):
            return group

        async def list_rungs(self, proposal_pk):
            return []

        async def update_group(self, target, **fields):
            self.updates += 1
            for key, value in fields.items():
                setattr(target, key, value)
            return target

    repo = Repo()
    service = _service_with_repo(repo)
    service.acquire_target_mutation_lock = AsyncMock()
    service.expire_if_needed = AsyncMock(
        side_effect=AssertionError("pending callback must not expire proposal")
    )
    revalidate_fn = AsyncMock(
        side_effect=AssertionError("pending callback must not revalidate")
    )
    session = SimpleNamespace(commit=AsyncMock())

    result = await _handle_approve(
        session=session,
        service=service,
        proposal_id=PROPOSAL_ID,
        callback=_callback(),
        now=NOW,
        notifier=SimpleNamespace(),
        chat_id="chat",
        message_id=7,
        callback_query_id=None,
        telegram_user_id="operator",
        revalidate_fn=revalidate_fn,
    )

    assert result["handled"] is False
    assert result["reason"] == "approval_dispatch_pending"
    assert group.approval_nonce_used_at is None
    assert repo.updates == 0
    service.expire_if_needed.assert_not_awaited()
    revalidate_fn.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "attempt_id", "card_kind", "action"),
    [
        (ApprovalDispatchState.PENDING, ATTEMPT_ID, ApprovalCardKind.MANUAL, "op"),
        (ApprovalDispatchState.FAILED, ATTEMPT_ID, ApprovalCardKind.MANUAL, "op"),
        (
            ApprovalDispatchState.PARTIAL_FAILED,
            ATTEMPT_ID,
            ApprovalCardKind.MANUAL,
            "op",
        ),
        (
            ApprovalDispatchState.SENT_SUPERSEDED,
            ATTEMPT_ID,
            ApprovalCardKind.MANUAL,
            "op",
        ),
        (
            ApprovalDispatchState.FAILED_SUPERSEDED,
            ATTEMPT_ID,
            ApprovalCardKind.MANUAL,
            "op",
        ),
        (
            ApprovalDispatchState.SENT_CURRENT,
            uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            ApprovalCardKind.MANUAL,
            "op",
        ),
        (
            ApprovalDispatchState.SENT_CURRENT,
            ATTEMPT_ID,
            ApprovalCardKind.AUTO_VETO,
            "op",
        ),
        (
            ApprovalDispatchState.SENT_CURRENT,
            ATTEMPT_ID,
            ApprovalCardKind.MANUAL,
            "vc",
        ),
    ],
)
async def test_invalid_loss_cut_binding_has_zero_pre_gate_external_calls(
    state: ApprovalDispatchState,
    attempt_id: uuid.UUID,
    card_kind: ApprovalCardKind,
    action: str,
) -> None:
    nonce = "safe-nonce"
    digest = _proposal_digest(nonce, card_kind=card_kind)
    group = OrderProposal(
        id=1,
        proposal_id=PROPOSAL_ID,
        root_proposal_id=PROPOSAL_ID,
        revision=1,
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="market",
        proposer="probe",
        lifecycle_state="proposed",
        exit_intent="loss_cut",
        approval_nonce=nonce,
        approval_dispatch_state=state.value,
        approval_dispatch_attempt_id=attempt_id,
        approval_dispatch_card_kind=card_kind.value,
        approval_dispatch_membership_revision=1,
        approval_dispatch_membership_digest=digest,
    )

    class Repo:
        updates = 0

        async def get_group_by_proposal_id(self, proposal_id, *, for_update=False):
            return group

        async def update_group(self, target, **fields):
            self.updates += 1
            for key, value in fields.items():
                setattr(target, key, value)
            return target

    repo = Repo()
    service = _service_with_repo(repo)
    session = SimpleNamespace(commit=AsyncMock())
    provider_read = AsyncMock()
    dry_run = AsyncMock()
    submit = AsyncMock()
    cancel = AsyncMock()
    answer_callback = AsyncMock()

    async def preview(**_kwargs):
        await provider_read()
        await dry_run()
        return []

    preview_mock = AsyncMock(side_effect=preview)
    result = await _handle_loss_cut_first_click(
        session=session,
        service=service,
        proposal_id=PROPOSAL_ID,
        callback=_callback(action=action),
        now=NOW,
        notifier=SimpleNamespace(answer_callback=answer_callback),
        chat_id="chat",
        message_id=7,
        callback_query_id="callback-query",
        telegram_user_id="operator",
        loss_cut_preview_fn=preview_mock,
    )

    assert result["handled"] is False
    preview_mock.assert_not_awaited()
    provider_read.assert_not_awaited()
    dry_run.assert_not_awaited()
    submit.assert_not_awaited()
    cancel.assert_not_awaited()
    answer_callback.assert_not_awaited()
    assert group.approval_nonce_used_at is None
    assert repo.updates == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        (
            "approval_dispatch_attempt_id",
            uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        ),
        ("approval_nonce", "replacement-nonce"),
        ("approval_dispatch_membership_revision", 2),
    ],
)
async def test_loss_cut_preview_to_consume_toctou_fails_closed(
    field: str, replacement: object
) -> None:
    nonce = "safe-nonce"
    digest = _proposal_digest(nonce)
    group = OrderProposal(
        id=1,
        proposal_id=PROPOSAL_ID,
        root_proposal_id=PROPOSAL_ID,
        revision=1,
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="market",
        proposer="probe",
        lifecycle_state="proposed",
        exit_intent="loss_cut",
        approval_nonce=nonce,
        approval_dispatch_state=ApprovalDispatchState.SENT_CURRENT.value,
        approval_dispatch_attempt_id=ATTEMPT_ID,
        approval_dispatch_card_kind=ApprovalCardKind.MANUAL.value,
        approval_dispatch_membership_revision=1,
        approval_dispatch_membership_digest=digest,
    )

    class Repo:
        updates = 0

        async def get_group_by_proposal_id(self, proposal_id, *, for_update=False):
            return group

        async def update_group(self, target, **fields):
            self.updates += 1
            for key, value in fields.items():
                setattr(target, key, value)
            return target

    repo = Repo()
    service = _service_with_repo(repo)
    session = SimpleNamespace(commit=AsyncMock())
    submit = AsyncMock()
    cancel = AsyncMock()

    async def preview(**_kwargs):
        setattr(group, field, replacement)
        return []

    preview_mock = AsyncMock(side_effect=preview)
    result = await _handle_loss_cut_first_click(
        session=session,
        service=service,
        proposal_id=PROPOSAL_ID,
        callback=_callback(digest=digest),
        now=NOW,
        notifier=SimpleNamespace(),
        chat_id="chat",
        message_id=7,
        telegram_user_id="operator",
        loss_cut_preview_fn=preview_mock,
    )

    assert result["handled"] is False
    assert result["reason"] in {
        "approval_dispatch_attempt_mismatch",
        "nonce_mismatch",
        "approval_membership_revision_mismatch",
    }
    preview_mock.assert_awaited_once()
    submit.assert_not_awaited()
    cancel.assert_not_awaited()
    assert group.approval_nonce_used_at is None
    assert repo.updates == 0


def _batch_fixture(*, state: ApprovalDispatchState, extra_member_revision: int = 1):
    batch_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
    batch = SimpleNamespace(
        id=7,
        batch_id=batch_id,
        chat_id="chat",
        approval_nonce="safe-nonce",
        approval_nonce_used_at=None,
        expires_at=NOW + timedelta(minutes=5),
        approval_dispatch_state=state.value,
        approval_dispatch_attempt_id=ATTEMPT_ID,
        membership_revision=1,
        membership_digest=None,
    )
    members = []
    groups = {}
    for index in range(1, 38):
        revision = 1 if index <= 36 else extra_member_revision
        proposal_id = uuid.UUID(int=index)
        groups[index] = SimpleNamespace(proposal_id=proposal_id)
        members.append(
            SimpleNamespace(
                id=index,
                proposal_pk=index,
                approval_nonce_snapshot=f"member-{index}",
                approval_message_id=1000 + index,
                membership_revision=revision,
                approval_dispatch_attempt_id_snapshot=uuid.UUID(int=100 + index),
                approval_membership_revision_snapshot=1,
                approval_membership_digest_snapshot=DIGEST,
                approval_card_kind_snapshot=ApprovalCardKind.MANUAL.value,
            )
        )

    batch.membership_digest = build_membership_digest(
        card_kind=ApprovalCardKind.BATCH,
        membership_revision=1,
        members=[
            {
                "proposal_id": str(groups[index].proposal_id),
                "approval_nonce": members[index - 1].approval_nonce_snapshot,
                "approval_message_id": members[index - 1].approval_message_id,
                "approval_dispatch_attempt_id": str(
                    members[index - 1].approval_dispatch_attempt_id_snapshot
                ),
                "approval_membership_revision": 1,
                "approval_membership_digest": DIGEST,
            }
            for index in range(1, 37)
        ],
    )

    class Repo:
        async def get_approval_batch_by_id(self, requested, *, for_update=False):
            return batch

        async def list_approval_batch_members(self, batch_pk):
            return members

        async def update_approval_batch(self, target, **fields):
            for key, value in fields.items():
                setattr(target, key, value)
            return target

        async def get_group_by_pk(self, proposal_pk):
            return groups[proposal_pk]

    return batch_id, batch, groups, _service_with_repo(Repo())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_old_36_member_button_cannot_approve_db_member_37() -> None:
    batch_id, batch, groups, service = _batch_fixture(
        state=ApprovalDispatchState.SENT_CURRENT
    )
    with pytest.raises(
        OrderProposalError, match="approval_batch_membership_digest_mismatch"
    ):
        await service.consume_approval_batch_nonce(
            batch_id,
            callback=_callback(
                action="ba",
                digest=batch.membership_digest,
                subject_short=str(batch_id)[:8],
            ),
            chat_id="chat",
            telegram_user_id="operator",
            now=NOW,
        )

    assert batch.approval_nonce_used_at is None
    assert groups[37].proposal_id is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exact_36_member_snapshot_processes_only_its_revision() -> None:
    batch_id, batch, groups, service = _batch_fixture(
        state=ApprovalDispatchState.SENT_CURRENT,
        extra_member_revision=2,
    )
    _consumed, snapshots = await service.consume_approval_batch_nonce(
        batch_id,
        callback=_callback(
            action="ba",
            digest=batch.membership_digest,
            subject_short=str(batch_id)[:8],
        ),
        chat_id="chat",
        telegram_user_id="operator",
        now=NOW,
    )
    assert len(snapshots) == 36
    assert all(item.proposal_id != groups[37].proposal_id for item in snapshots)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        ApprovalDispatchState.PENDING,
        ApprovalDispatchState.FAILED,
        ApprovalDispatchState.PARTIAL_FAILED,
        ApprovalDispatchState.SENT_SUPERSEDED,
        ApprovalDispatchState.FAILED_SUPERSEDED,
    ],
)
async def test_unpublished_or_failed_batch_callback_is_blocked(
    state: ApprovalDispatchState,
) -> None:
    batch_id, batch, _groups, service = _batch_fixture(state=state)
    with pytest.raises(OrderProposalError, match=f"approval_dispatch_{state.value}"):
        await service.consume_approval_batch_nonce(
            batch_id,
            callback=_callback(
                action="ba",
                digest=batch.membership_digest,
                subject_short=str(batch_id)[:8],
            ),
            chat_id="chat",
            telegram_user_id="operator",
            now=NOW,
        )
    assert batch.approval_nonce_used_at is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_frozen_batch_is_excluded_from_open_membership_query() -> None:
    captured = []

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class Session:
        async def execute(self, statement):
            captured.append(statement)
            return Result()

    repository = OrderProposalRepository(Session())
    assert (
        await repository.get_open_approval_batch(
            chat_id="chat", now=NOW, for_update=True
        )
        is None
    )
    statement = str(captured[0])
    assert "membership_frozen_at IS NULL" in statement
    assert "approval_dispatch_state" in statement
    assert "summary_dispatch_state" in statement
    assert "summary_message_id IS NULL" in statement
    assert "approval_dispatch_attempt_id IS NULL" in statement
    assert "membership_digest IS NULL" in statement


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oversized_batch_card_is_rejected_before_any_http_call() -> None:
    batch_id = uuid.UUID("66666666-6666-4666-8666-666666666666")
    binding = DispatchBinding(
        attempt_id=ATTEMPT_ID,
        card_kind=ApprovalCardKind.BATCH,
        membership_revision=1,
        membership_digest=DIGEST,
    )
    batch = SimpleNamespace(
        batch_id=batch_id,
        approval_nonce="safe-nonce",
        expires_at=NOW + timedelta(minutes=5),
    )
    proposals = [
        (
            SimpleNamespace(
                proposal_id=uuid.UUID(int=index),
                symbol=f"{index:02d}-" + ("매우긴종목" * 25),
                side="buy",
                market="equity_kr",
                account_mode="kis_live",
                broker_account_id=f"account-{index:04d}",
                order_type="limit",
            ),
            [
                SimpleNamespace(
                    rung_index=0,
                    quantity=Decimal("1"),
                    limit_price=Decimal("100000"),
                    notional=None,
                )
            ],
        )
        for index in range(1, 38)
    ]
    text, keyboard = build_batch_approval_message(
        batch=batch, proposals=proposals, binding=binding
    )
    assert telegram_text_length(text) > 4096
    notifier = SimpleNamespace(send_approval_message=AsyncMock())

    publication = await publish_approval_messages(
        notifier=notifier,
        messages=ApprovalDispatchMessages(
            (),
            text,
            keyboard,
            telegram_text_length(text),
        ),
        chat_id="chat",
    )

    assert publication.card_published is False
    assert publication.failure_code == "approval_payload_too_long"
    notifier.send_approval_message.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_code", "classification"),
    [
        (400, 400, TelegramErrorClassification.BAD_REQUEST),
        (429, 429, TelegramErrorClassification.RATE_LIMITED),
        (500, 500, TelegramErrorClassification.SERVER_ERROR),
        (503, 503, TelegramErrorClassification.SERVER_ERROR),
        (None, None, TelegramErrorClassification.TRANSPORT_ERROR),
    ],
)
async def test_failed_batch_publication_never_becomes_approvable(
    status: int | None,
    error_code: int | None,
    classification: TelegramErrorClassification,
) -> None:
    batch_id = uuid.UUID("77777777-7777-4777-8777-777777777777")
    batch = SimpleNamespace(
        id=1,
        batch_id=batch_id,
        chat_id="chat",
        approval_nonce="safe-nonce",
        approval_nonce_used_at=None,
        expires_at=NOW + timedelta(minutes=5),
        approval_dispatch_state=ApprovalDispatchState.PENDING.value,
        approval_dispatch_attempt_id=ATTEMPT_ID,
        membership_revision=1,
        membership_digest=DIGEST,
        membership_frozen_at=NOW,
        summary_dispatch_state="sending",
    )

    class Repo:
        async def get_approval_batch_by_id(self, requested, *, for_update=False):
            return batch

        async def update_approval_batch(self, target, **fields):
            for key, value in fields.items():
                setattr(target, key, value)
            return target

        async def list_approval_batch_members(self, batch_pk):
            raise AssertionError("failed state must block before membership")

    service = _service_with_repo(Repo())
    method = TelegramMethodResult.failed(
        payload_chars=200,
        failure_code="telegram_transport_error",
        status_code=status,
        error_code=error_code,
        error_classification=classification,
    )
    result = await service.finish_approval_batch_dispatch(
        batch_id,
        attempt_id=ATTEMPT_ID,
        publication=ApprovalPublication.failed(
            payload_chars=200,
            failure_code="approval_card_dispatch_failed",
            method_result=method,
        ),
        now=NOW,
    )

    assert result.ok is False
    assert result.state is ApprovalDispatchState.FAILED
    assert batch.approval_dispatch_state == ApprovalDispatchState.FAILED.value
    assert batch.summary_message_id is None
    with pytest.raises(OrderProposalError, match="approval_dispatch_failed"):
        await service.consume_approval_batch_nonce(
            batch_id,
            callback=_callback(action="ba", subject_short=str(batch_id)[:8]),
            chat_id="chat",
            telegram_user_id="operator",
            now=NOW,
        )
    assert batch.approval_nonce_used_at is None


def _published(message_id: int) -> ApprovalPublication:
    return ApprovalPublication.published(
        payload_chars=100,
        method_result=TelegramMethodResult(
            ok=True,
            message_id=message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=100,
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nonce_change_during_inflight_send_supersedes_same_attempt_id() -> None:
    old_digest = _proposal_digest("old-nonce")
    group = SimpleNamespace(
        id=1,
        proposal_id=PROPOSAL_ID,
        superseded_by_proposal_id=None,
        lifecycle_state="proposed",
        approval_dispatch_attempt_id=ATTEMPT_ID,
        approval_dispatch_state=ApprovalDispatchState.PENDING.value,
        approval_dispatch_card_kind=ApprovalCardKind.MANUAL.value,
        approval_dispatch_membership_revision=1,
        approval_dispatch_membership_digest=old_digest,
        approval_dispatch_published_at=None,
        approval_nonce="old-nonce",
        approval_nonce_used_at=None,
        source_asof={},
    )
    attempt = SimpleNamespace(
        attempt_id=ATTEMPT_ID,
        proposal_pk=1,
        state=ApprovalDispatchState.PENDING.value,
        card_kind=ApprovalCardKind.MANUAL.value,
        membership_revision=1,
        membership_digest=old_digest,
    )

    class Repo:
        async def get_group_by_proposal_id(self, proposal_id, *, for_update=False):
            return group

        async def get_approval_dispatch_attempt(self, attempt_id, *, for_update=False):
            return attempt

        async def update_approval_dispatch_attempt(self, target, **fields):
            for key, value in fields.items():
                setattr(target, key, value)
            return target

        async def update_group(self, target, **fields):
            for key, value in fields.items():
                setattr(target, key, value)
            return target

    service = _service_with_repo(Repo())
    await service.set_approval_nonce(PROPOSAL_ID, "new-nonce")
    result = await service.finish_approval_dispatch(
        PROPOSAL_ID,
        attempt_id=ATTEMPT_ID,
        publication=_published(700),
        chat_id="chat",
        now=NOW,
    )

    assert result.state is ApprovalDispatchState.SENT_SUPERSEDED
    assert result.ok is False
    assert result.failure_code == "approval_dispatch_superseded"
    assert attempt.state == ApprovalDispatchState.SENT_SUPERSEDED.value
    assert group.approval_dispatch_state == ApprovalDispatchState.FAILED.value
    assert group.approval_nonce == "new-nonce"
    assert "approval_message_id" not in group.source_asof


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_loss_finalizes_failed_and_invalidates_visible_card() -> None:
    digest = _proposal_digest("safe-nonce")
    group = SimpleNamespace(
        id=1,
        proposal_id=PROPOSAL_ID,
        superseded_by_proposal_id=None,
        lifecycle_state="proposed",
        approval_dispatch_attempt_id=ATTEMPT_ID,
        approval_dispatch_state=ApprovalDispatchState.PENDING.value,
        approval_dispatch_card_kind=ApprovalCardKind.MANUAL.value,
        approval_dispatch_membership_revision=1,
        approval_dispatch_membership_digest=digest,
        approval_nonce="safe-nonce",
        approval_nonce_used_at=None,
        source_asof={},
    )
    attempt = SimpleNamespace(
        attempt_id=ATTEMPT_ID,
        proposal_pk=1,
        state=ApprovalDispatchState.PENDING.value,
        card_kind=ApprovalCardKind.MANUAL.value,
        membership_revision=1,
        membership_digest=digest,
    )

    class Repo:
        async def get_group_by_proposal_id(self, proposal_id, *, for_update=False):
            return group

        async def get_approval_dispatch_attempt(self, attempt_id, *, for_update=False):
            return attempt

        async def update_approval_dispatch_attempt(self, target, **fields):
            for key, value in fields.items():
                setattr(target, key, value)
            return target

        async def update_group(self, target, **fields):
            for key, value in fields.items():
                setattr(target, key, value)
            return target

    service = _service_with_repo(Repo())
    method = TelegramMethodResult.failed(
        payload_chars=100,
        failure_code="telegram_transport_error",
        error_classification=TelegramErrorClassification.TRANSPORT_ERROR,
    )
    result = await service.finish_approval_dispatch(
        PROPOSAL_ID,
        attempt_id=ATTEMPT_ID,
        publication=ApprovalPublication.failed(
            payload_chars=100,
            failure_code="approval_card_dispatch_failed",
            method_result=method,
        ),
        chat_id="chat",
        now=NOW,
    )

    assert result.state is ApprovalDispatchState.FAILED
    assert result.ok is False
    assert attempt.state == ApprovalDispatchState.FAILED.value
    assert group.approval_dispatch_state == ApprovalDispatchState.FAILED.value
    assert group.approval_nonce is None
    assert "approval_message_id" not in group.source_asof
    with pytest.raises(OrderProposalError, match="approval_dispatch_failed"):
        await service.consume_published_proposal_callback(
            PROPOSAL_ID,
            callback=_callback(digest=digest),
            now=NOW,
        )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("new_finishes_first", [False, True])
async def test_attempt_completion_order_preserves_current_owner_invariants(
    new_finishes_first: bool,
) -> None:
    old_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    new_id = uuid.UUID("55555555-5555-4555-8555-555555555555")
    old_digest = _proposal_digest("old-nonce")
    new_digest = _proposal_digest("new-nonce", revision=2)
    group = SimpleNamespace(
        id=1,
        proposal_id=PROPOSAL_ID,
        approval_dispatch_attempt_id=new_id,
        approval_dispatch_state=ApprovalDispatchState.PENDING.value,
        approval_dispatch_card_kind=ApprovalCardKind.MANUAL.value,
        approval_dispatch_membership_revision=2,
        approval_dispatch_membership_digest=new_digest,
        approval_nonce="new-nonce",
        source_asof={},
    )
    attempts = {
        old_id: SimpleNamespace(
            attempt_id=old_id,
            proposal_pk=1,
            state=ApprovalDispatchState.PENDING.value,
            card_kind=ApprovalCardKind.MANUAL.value,
            membership_revision=1,
            membership_digest=old_digest,
        ),
        new_id: SimpleNamespace(
            attempt_id=new_id,
            proposal_pk=1,
            state=ApprovalDispatchState.PENDING.value,
            card_kind=ApprovalCardKind.MANUAL.value,
            membership_revision=2,
            membership_digest=new_digest,
        ),
    }

    class Repo:
        async def get_group_by_proposal_id(self, proposal_id, *, for_update=False):
            return group

        async def get_approval_dispatch_attempt(self, attempt_id, *, for_update=False):
            return attempts[attempt_id]

        async def update_approval_dispatch_attempt(self, attempt, **fields):
            for key, value in fields.items():
                setattr(attempt, key, value)
            return attempt

        async def update_group(self, target, **fields):
            for key, value in fields.items():
                setattr(target, key, value)
            return target

    service = _service_with_repo(Repo())
    order = (new_id, old_id) if new_finishes_first else (old_id, new_id)
    results = {}
    batch_members: list[uuid.UUID] = []
    for attempt_id in order:
        results[attempt_id] = await service.finish_approval_dispatch(
            PROPOSAL_ID,
            attempt_id=attempt_id,
            publication=_published(700 if attempt_id == old_id else 800),
            chat_id="chat",
            now=NOW,
        )
        if results[attempt_id].approvable:
            batch_members.append(attempt_id)

    assert results[old_id].ok is False
    assert results[old_id].state is ApprovalDispatchState.SENT_SUPERSEDED
    assert results[old_id].failure_code == "approval_dispatch_superseded"
    assert results[new_id].ok is True
    assert attempts[old_id].state == ApprovalDispatchState.SENT_SUPERSEDED.value
    assert attempts[new_id].state == ApprovalDispatchState.SENT_CURRENT.value
    assert group.approval_dispatch_attempt_id == new_id
    assert group.approval_dispatch_state == ApprovalDispatchState.SENT_CURRENT.value
    assert group.approval_nonce == "new-nonce"
    assert group.source_asof["approval_message_id"] == 800
    assert group.source_asof["approval_message_id"] != 700
    assert batch_members == [new_id]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_failed_attempt_cannot_trigger_current_failure_compensation() -> (
    None
):
    old_id = uuid.UUID("88888888-8888-4888-8888-888888888888")
    new_id = uuid.UUID("99999999-9999-4999-8999-999999999999")
    old_digest = _proposal_digest("old-nonce")
    new_digest = _proposal_digest("new-nonce", revision=2)
    group = SimpleNamespace(
        id=1,
        proposal_id=PROPOSAL_ID,
        approval_dispatch_attempt_id=new_id,
        approval_dispatch_state=ApprovalDispatchState.PENDING.value,
        approval_dispatch_card_kind=ApprovalCardKind.MANUAL.value,
        approval_dispatch_membership_revision=2,
        approval_dispatch_membership_digest=new_digest,
        approval_nonce="new-nonce",
        source_asof={},
    )
    old_attempt = SimpleNamespace(
        attempt_id=old_id,
        proposal_pk=1,
        state=ApprovalDispatchState.PENDING.value,
        card_kind=ApprovalCardKind.MANUAL.value,
        membership_revision=1,
        membership_digest=old_digest,
    )

    class Repo:
        async def get_group_by_proposal_id(self, proposal_id, *, for_update=False):
            return group

        async def get_approval_dispatch_attempt(self, attempt_id, *, for_update=False):
            return old_attempt

        async def update_approval_dispatch_attempt(self, attempt, **fields):
            for key, value in fields.items():
                setattr(attempt, key, value)
            return attempt

    service = _service_with_repo(Repo())
    method = TelegramMethodResult.failed(
        payload_chars=10,
        failure_code="telegram_transport_error",
        error_classification=TelegramErrorClassification.TRANSPORT_ERROR,
    )
    result = await service.finish_approval_dispatch(
        PROPOSAL_ID,
        attempt_id=old_id,
        publication=ApprovalPublication.failed(
            payload_chars=10,
            failure_code="approval_card_dispatch_failed",
            method_result=method,
        ),
        chat_id="chat",
        now=NOW,
    )

    assert result.state is ApprovalDispatchState.FAILED_SUPERSEDED
    assert result.ok is False
    assert result.failure_code == "approval_dispatch_superseded"
    assert group.approval_dispatch_attempt_id == new_id
    assert group.approval_nonce == "new-nonce"


class _OrchestrationSession:
    def __init__(self, service: object) -> None:
        self.service = service
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_batch_calls"),
    [
        (ApprovalDispatchState.SENT_CURRENT, 1),
        (ApprovalDispatchState.SENT_SUPERSEDED, 0),
    ],
)
async def test_send_orchestration_registers_batch_only_for_current_result(
    monkeypatch,
    state: ApprovalDispatchState,
    expected_batch_calls: int,
) -> None:
    group = SimpleNamespace(
        proposal_id=PROPOSAL_ID,
        approval_dispatch_membership_revision=None,
    )
    first_service = SimpleNamespace(
        set_approval_nonce=AsyncMock(),
        get_proposal=AsyncMock(return_value=(group, [])),
        start_approval_dispatch=AsyncMock(),
    )
    result = TelegramDispatchResult(
        state=state,
        message_id=700,
        status_code=200,
        error_code=None,
        error_classification=None,
        payload_chars=4,
        failure_code=(
            None
            if state is ApprovalDispatchState.SENT_CURRENT
            else "approval_dispatch_superseded"
        ),
    )
    second_service = SimpleNamespace(
        finish_approval_dispatch=AsyncMock(return_value=result)
    )
    sessions = [
        _OrchestrationSession(first_service),
        _OrchestrationSession(second_service),
    ]

    def service_factory():
        return sessions.pop(0)

    monkeypatch.setattr(
        dispatch_module, "OrderProposalsService", lambda session: session.service
    )
    monkeypatch.setattr(
        dispatch_module.settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        "chat",
    )
    monkeypatch.setattr(
        dispatch_module,
        "build_approval_dispatch_messages",
        lambda **_kwargs: ApprovalDispatchMessages(
            context_messages=(),
            approval_text="card",
            inline_keyboard={"inline_keyboard": []},
            payload_chars=4,
        ),
    )
    monkeypatch.setattr(
        dispatch_module,
        "publish_approval_messages",
        AsyncMock(return_value=_published(700)),
    )
    register = AsyncMock()
    monkeypatch.setattr(
        dispatch_module, "_register_and_publish_batch_summary", register
    )

    returned = await send_proposal_for_approval(
        PROPOSAL_ID,
        notifier=SimpleNamespace(),
        now=NOW,
        service_factory=service_factory,
    )

    assert returned is result
    assert register.await_count == expected_batch_calls


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_compensation_calls"),
    [
        (ApprovalDispatchState.FAILED, 1),
        (ApprovalDispatchState.PARTIAL_FAILED, 1),
        (ApprovalDispatchState.SENT_SUPERSEDED, 0),
        (ApprovalDispatchState.FAILED_SUPERSEDED, 0),
    ],
)
async def test_auto_dispatch_compensates_only_current_failed_result(
    monkeypatch,
    state: ApprovalDispatchState,
    expected_compensation_calls: int,
) -> None:
    group = SimpleNamespace(
        proposal_id=PROPOSAL_ID,
        market="equity_kr",
        approval_dispatch_membership_revision=None,
    )
    rung = SimpleNamespace(state="pending_approval")
    first_service = SimpleNamespace(
        acquire_auto_dispatch_lock=AsyncMock(),
        get_proposal=AsyncMock(return_value=(group, [rung])),
        auto_approved_daily_notional=AsyncMock(return_value=Decimal("0")),
        record_auto_approval=AsyncMock(),
        set_approval_nonce=AsyncMock(),
        start_approval_dispatch=AsyncMock(),
    )
    result = TelegramDispatchResult(
        state=state,
        message_id=700 if state is ApprovalDispatchState.SENT_SUPERSEDED else None,
        status_code=200 if state is ApprovalDispatchState.SENT_SUPERSEDED else 500,
        error_code=None,
        error_classification=None,
        payload_chars=4,
        failure_code=(
            "approval_dispatch_superseded"
            if state
            in {
                ApprovalDispatchState.SENT_SUPERSEDED,
                ApprovalDispatchState.FAILED_SUPERSEDED,
            }
            else "approval_card_dispatch_failed"
        ),
    )
    second_service = SimpleNamespace(
        acquire_auto_dispatch_lock=AsyncMock(),
        finish_approval_dispatch=AsyncMock(return_value=result),
        get_proposal=AsyncMock(return_value=(group, [rung])),
        record_auto_notification_failure=AsyncMock(),
    )
    sessions = [
        _OrchestrationSession(first_service),
        _OrchestrationSession(second_service),
    ]

    def service_factory():
        return sessions.pop(0)

    monkeypatch.setattr(
        dispatch_module, "OrderProposalsService", lambda session: session.service
    )
    monkeypatch.setattr(dispatch_module.settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(
        dispatch_module.settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        "chat",
    )
    monkeypatch.setattr(
        dispatch_module,
        "limits_for_market",
        lambda _market: SimpleNamespace(policy_version="probe"),
    )
    monkeypatch.setattr(
        dispatch_module,
        "build_auto_approved_message",
        lambda **_kwargs: ("card", {"inline_keyboard": []}),
    )
    monkeypatch.setattr(
        dispatch_module,
        "publish_approval_messages",
        AsyncMock(
            return_value=ApprovalPublication.failed(
                payload_chars=4,
                failure_code="approval_card_dispatch_failed",
            )
        ),
    )
    acquire_veto_locks = AsyncMock()
    compensate = AsyncMock(return_value=[])
    monkeypatch.setattr(dispatch_module, "acquire_auto_veto_locks", acquire_veto_locks)
    monkeypatch.setattr(dispatch_module, "cancel_auto_submitted_rungs", compensate)
    revalidate = AsyncMock(
        return_value=[RungOutcome(rung_index=0, result="submitted_acked", detail={})]
    )

    returned = await dispatch_proposal(
        PROPOSAL_ID,
        notifier=SimpleNamespace(),
        now=NOW,
        service_factory=service_factory,
        revalidate_fn=revalidate,
    )

    assert returned is result
    assert compensate.await_count == expected_compensation_calls
    assert acquire_veto_locks.await_count == expected_compensation_calls
    assert (
        second_service.record_auto_notification_failure.await_count
        == expected_compensation_calls
    )
