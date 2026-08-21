"""Shared fixtures for the durable Telegram callback inbox (W5).

Every fixture here talks to the *run-owned* test database through the real
``AsyncSessionLocal``/``engine`` — the inbox's whole point is that the
PostgreSQL row and the PostgreSQL advisory lock are the authority, so a
session-level double or an in-memory fake would prove nothing about the
contract under test.

No Telegram, broker, provider or Redis client is ever constructed: the
enqueue side effect is injected, the notifier is a fake, and
``revalidate_and_submit`` is replaced everywhere a proposal is exercised.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import approval_message as approval_messages
from app.services.order_proposals import revalidation as revalidation_module
from app.services.order_proposals import telegram_callback as callback_module
from app.services.order_proposals.callback_inbox.contracts import (
    RECOVERY_CLAIMABLE_STATES,
    SCRUBBED_ON_TERMINAL,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalPublication,
    DispatchBinding,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.service import RungInput
from app.telegram_contract import TelegramMethodResult, telegram_text_length
from tests.services.order_proposals.window_fakes import allow_known_session

CHAT_ID = 42
USER_ID = 777


@pytest.fixture(autouse=True)
def _known_market_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the approval window deterministic, exactly as the sibling suite does."""
    monkeypatch.setattr(
        callback_module, "evaluate_approval_window", allow_known_session
    )
    monkeypatch.setattr(
        revalidation_module, "evaluate_approval_window", allow_known_session
    )


@pytest.fixture(autouse=True)
def _allowlisted_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        str(CHAT_ID),
        raising=False,
    )


class FakeNotifier:
    """Records every Telegram side effect; performs none."""

    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, dict | None, str]] = []
        self.answered: list[tuple[str, str | None]] = []
        self.edited: list[tuple[Any, int, str, dict | None]] = []
        self._next_message_id = 9000

    async def send_approval_message(
        self, text, inline_keyboard, *, chat_id, parse_mode="Markdown"
    ):
        self._next_message_id += 1
        self.sent_messages.append((text, inline_keyboard, chat_id))
        return TelegramMethodResult(
            ok=True,
            message_id=self._next_message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )

    async def answer_callback(self, callback_query_id, text=None):
        self.answered.append((callback_query_id, text))
        return True

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text, reply_markup))
        return TelegramMethodResult(
            ok=True,
            message_id=message_id,
            status_code=200,
            error_code=None,
            error_classification=None,
            payload_chars=telegram_text_length(text),
        )

    @property
    def external_calls(self) -> int:
        return len(self.sent_messages) + len(self.answered) + len(self.edited)


def make_update(
    *,
    data: str,
    chat_id: Any = CHAT_ID,
    user_id: Any = USER_ID,
    callback_id: str = "cbq-1",
    update_id: int = 1,
    message_id: int = 555,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": message_id},
            "data": data,
        },
    }


def session_factory_for(session) -> Callable[[], Any]:
    @contextlib.asynccontextmanager
    async def _factory() -> AsyncIterator[Any]:
        yield session

    return _factory


def _successful_publication(message_id: int) -> ApprovalPublication:
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


async def _publish_fixture_card(
    service: OrderProposalsService,
    group,
    *,
    nonce: str,
    card_kind: ApprovalCardKind = ApprovalCardKind.MANUAL,
    message_id: int = 555,
) -> DispatchBinding:
    if group.approval_nonce != nonce:
        await service.set_approval_nonce(group.proposal_id, nonce)
    attempt_id = uuid.uuid4()
    now = min(datetime.now(UTC), group.valid_until - timedelta(microseconds=1))
    window = await allow_known_session(group, now=now)
    binding = build_proposal_dispatch_binding(
        proposal_id=group.proposal_id,
        nonce=nonce,
        attempt_id=attempt_id,
        card_kind=card_kind,
        current_membership_revision=group.approval_dispatch_membership_revision,
    )
    await service.start_approval_dispatch(
        group.proposal_id,
        attempt_id=attempt_id,
        binding=binding,
        now=now,
        payload_chars=100,
        context_message_count=0,
    )
    result = await service.finish_approval_dispatch(
        group.proposal_id,
        attempt_id=attempt_id,
        publication=_successful_publication(message_id),
        chat_id=str(CHAT_ID),
        now=now,
        approval_window_policy_stamp=window.policy_stamp,
    )
    assert result.ok
    return binding


async def seed_proposal(
    session, *, nonce: str = "nonce-abc123", symbol: str = "A", rungs: int = 1
):
    """Seed one published, approvable proposal and COMMIT it.

    Committed on purpose: the durable worker opens its own sessions, so a
    proposal parked in an uncommitted transaction would be invisible to it.
    """
    service = OrderProposalsService(session)
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[
            RungInput(i, "buy", Decimal("10"), Decimal("100"), None)
            for i in range(rungs)
        ],
    )
    dispatched_at = datetime.now(UTC)
    window = await allow_known_session(group, now=dispatched_at)
    await service.record_approval_dispatch(
        group.proposal_id,
        message_id=555,
        chat_id=str(CHAT_ID),
        now=dispatched_at,
        approval_window_policy_stamp=window.policy_stamp,
    )
    await service.set_approval_nonce(group.proposal_id, nonce)
    await _publish_fixture_card(service, group, nonce=nonce)
    await session.commit()
    return group


def proposal_callback_data(group, *, action: str = "op") -> str:
    return approval_messages.build_callback_data(
        action=action,
        proposal_id=group.proposal_id,
        nonce=group.approval_nonce,
        binding=DispatchBinding(
            attempt_id=group.approval_dispatch_attempt_id,
            card_kind=ApprovalCardKind(group.approval_dispatch_card_kind),
            membership_revision=group.approval_dispatch_membership_revision,
            membership_digest=group.approval_dispatch_membership_digest,
        ),
    )


async def seed_auto_veto_proposal(session, *, nonce: str, symbol: str = "005930"):
    """A resting auto-submitted order carrying a `vc` (veto/cancel) card."""
    service = OrderProposalsService(session)
    now = datetime.now(UTC)
    group = await service.create_proposal(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        thesis="w5 auto-veto fixture",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("97000"), None)],
        source_asof={
            "auto_approved": {
                "policy_version": "test-policy",
                "approved_at": now.isoformat(),
                "eligibility": [],
                "outcomes": ["submitted_resting"],
            }
        },
    )
    await service.transition_rung(group.proposal_id, 0, new_state="revalidating")
    await service.transition_rung(group.proposal_id, 0, new_state="approved")
    await service.transition_rung(group.proposal_id, 0, new_state="submitting")
    await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id=f"broker-{uuid.uuid4().hex[:8]}",
        correlation_id=f"corr-{uuid.uuid4().hex[:8]}",
        idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        approval_hash_digest="digest-auto-1",
        now=now,
    )
    await service.set_approval_nonce(group.proposal_id, nonce)
    await _publish_fixture_card(
        service, group, nonce=nonce, card_kind=ApprovalCardKind.AUTO_VETO
    )
    await session.commit()
    return group


async def seed_loss_cut_proposal(
    session,
    monkeypatch,
    *,
    nonce: str,
    card_kind: ApprovalCardKind = ApprovalCardKind.LOSS_CUT_CONFIRMATION,
):
    """A loss-cut proposal.

    The default card is the ``lc`` confirmation (the *second* click). Pass
    ``ApprovalCardKind.MANUAL`` for the first click, which is an ``op`` on a
    loss-cut proposal and is what opens the preview.
    """
    retro = type(
        "Retro",
        (),
        {
            "id": 42,
            "symbol": "AAPL",
            "trigger_type": "stop_loss",
            "created_at": datetime.now(UTC),
            "lesson": "손절 기준을 늦추지 않는다",
        },
    )()

    async def fake_lookup(_session, retrospective_id):
        assert retrospective_id == 42
        return retro

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id", fake_lookup
    )
    service = OrderProposalsService(session)
    group = await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "sell", Decimal("1"), Decimal("99"), None)],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_issue_id="ROB-1285",
    )
    dispatched_at = datetime.now(UTC)
    window = await allow_known_session(group, now=dispatched_at)
    await service.record_approval_dispatch(
        group.proposal_id,
        message_id=555,
        chat_id=str(CHAT_ID),
        now=dispatched_at,
        approval_window_policy_stamp=window.policy_stamp,
    )
    await service.set_approval_nonce(group.proposal_id, nonce)
    await _publish_fixture_card(service, group, nonce=nonce, card_kind=card_kind)
    await session.commit()
    return group


async def seed_approval_batch(session, *, member_count: int = 2):
    """A `ba` (batch) card covering several published proposals."""
    from app.services.order_proposals.approval_message import build_batch_callback_data

    service = OrderProposalsService(session)
    now = datetime.now(UTC)
    groups = []
    registration = None
    for index in range(member_count):
        group = await seed_proposal(
            session,
            nonce=f"bm{uuid.uuid4().hex[:9]}",
            symbol=f"BW{index}",
        )
        groups.append(group)
        registration = await service.register_approval_batch_member(
            group.proposal_id,
            chat_id=str(CHAT_ID),
            approval_message_id=7100 + index,
            now=now + timedelta(seconds=index),
            summary_member_threshold=member_count,
        )
    await session.commit()
    assert registration is not None and registration.binding is not None
    batch = registration.batch
    result = await service.finish_approval_batch_dispatch(
        batch.batch_id,
        attempt_id=registration.binding.attempt_id,
        publication=_successful_publication(7999),
        now=now + timedelta(seconds=member_count),
    )
    assert result.ok
    await session.commit()
    data = build_batch_callback_data(
        batch_id=batch.batch_id,
        nonce=batch.approval_nonce,
        binding=registration.binding,
    )
    return batch, groups, data


async def consume_nonce(proposal_id: uuid.UUID) -> None:
    """Spend an approval out of band, as a first click would have."""
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE review.order_proposals SET approval_nonce_used_at = now() "
                "WHERE proposal_id = :pid"
            ),
            {"pid": proposal_id},
        )
        await session.commit()


_OWNED_INBOX_JOB_IDS: ContextVar[set[uuid.UUID] | None] = ContextVar(
    "owned_callback_inbox_job_ids", default=None
)


class _OwnedInboxJobIds(list[uuid.UUID]):
    """Fixture-owned job IDs that also authorize narrow test row shaping."""

    def __init__(self, owned: set[uuid.UUID]) -> None:
        super().__init__()
        self._owned = owned

    def append(self, job_id: uuid.UUID) -> None:
        if not isinstance(job_id, uuid.UUID):
            raise TypeError("inbox cleanup accepts UUID job ids only")
        self._owned.add(job_id)
        super().append(job_id)


# Crash/recovery tests need to reproduce only these durable shapes. This is
# intentionally not a generic test mutation API: immutable ``job_id`` and
# ``update_digest`` identity/digest material remain outside the allowlist, and
# terminal authority material (including ``update_identity_digest``) is
# scrub-only below. The listed timing and durable-marker fields are deliberately
# available to model crash/recovery shapes. Normal ORM flushes still enforce
# every live marker/cross-field constraint.
_TEST_OWNED_INBOX_SHAPE_FIELDS: frozenset[str] = frozenset(
    {
        "attempt_count",
        "available_at",
        "action",
        "callback_query_id",
        "chat_id",
        "dispatch_attempt_id",
        "error_class",
        "handler_completed_at",
        "handler_entered_at",
        "membership_digest",
        "membership_revision",
        "message_id",
        "nonce",
        "outcome",
        "received_at",
        "started_at",
        "state",
        "subject_short",
        "terminal_state_pending",
        "telegram_user_id",
        "update_identity_digest",
    }
)

# Keep the generic shaper incapable of re-arming any production terminal
# authority.  Deriving this set makes a future production scrub field fail
# safely here too, rather than turning test shaping into a bypass.
_TEST_OWNED_TERMINAL_SCRUB_ONLY_FIELDS: frozenset[str] = frozenset(SCRUBBED_ON_TERMINAL)

# The one test that needs a corrupt-but-still-active envelope may only use this
# fixed value.  It cannot be parsed as the eight-character hexadecimal subject
# short accepted by the worker.
_TEST_OWNED_INVALID_SUBJECT_SHORT = "zzzzzzzz"


async def shape_owned_callback_inbox_row(
    session: AsyncSession,
    job_id: uuid.UUID,
    **fields: Any,
) -> Any:
    """Shape one fixture-owned row through normal ORM constraint enforcement."""
    unknown = set(fields) - _TEST_OWNED_INBOX_SHAPE_FIELDS
    if unknown:
        raise ValueError(f"unexpected inbox shape field names: {sorted(unknown)}")
    if not fields:
        raise ValueError("an inbox shape needs explicit fields")
    rearmed = {
        field
        for field in _TEST_OWNED_TERMINAL_SCRUB_ONLY_FIELDS
        if field in fields and fields[field] is not None
    }
    if rearmed:
        raise ValueError(
            f"terminal authority fields may only be scrubbed: {sorted(rearmed)}"
        )

    owned = _OWNED_INBOX_JOB_IDS.get()
    if owned is None or job_id not in owned:
        raise PermissionError("test row shaping requires an inbox_cleanup-owned job")

    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    row = await session.scalar(
        select(TelegramCallbackInboxJob).where(
            TelegramCallbackInboxJob.job_id == job_id
        )
    )
    if row is None:
        raise LookupError(f"owned inbox row was not found: {job_id}")
    for field, value in fields.items():
        setattr(row, field, value)
    await session.flush()
    return row


async def degrade_owned_callback_subject_short(
    session: AsyncSession,
    job_id: uuid.UUID,
) -> Any:
    """Replace one owned active subject with the fixed invalid test value.

    This is deliberately narrower than ``shape_owned_callback_inbox_row``:
    a terminal-scrubbed ``None`` can never be re-armed, and callers cannot
    choose a replacement authority value.
    """
    owned = _OWNED_INBOX_JOB_IDS.get()
    if owned is None or job_id not in owned:
        raise PermissionError("test row shaping requires an inbox_cleanup-owned job")

    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    row = await session.scalar(
        select(TelegramCallbackInboxJob).where(
            TelegramCallbackInboxJob.job_id == job_id
        )
    )
    if row is None:
        raise LookupError(f"owned inbox row was not found: {job_id}")
    if type(row.state) is not str or row.state not in RECOVERY_CLAIMABLE_STATES:
        raise ValueError("only a recovery-claimable callback subject may be degraded")
    if row.subject_short is None:
        raise ValueError("a scrubbed subject_short may never be re-armed")
    if type(row.subject_short) is not str:
        raise ValueError("only an exact persisted subject_short may be degraded")

    from app.services.order_proposals.callback_inbox.worker import _SUBJECT_SHORT

    if _SUBJECT_SHORT.fullmatch(row.subject_short) is None:  # noqa: SLF001
        raise ValueError("only a reconstructable subject_short may be degraded")
    if _SUBJECT_SHORT.fullmatch(_TEST_OWNED_INVALID_SUBJECT_SHORT):  # noqa: SLF001
        raise AssertionError("the test-only subject corruption must remain invalid")
    row.subject_short = _TEST_OWNED_INVALID_SUBJECT_SHORT
    await session.flush()
    return row


def lock_is_released_for_test(lock: Any) -> bool:
    """Observe private holder state without a shipped introspection API."""
    return getattr(lock, "_connection", None) is None


def held_lock_connection_for_test(lock: Any) -> Any:
    """Return the private dedicated connection only for test observation."""
    connection = getattr(lock, "_connection", None)
    if connection is None:
        raise RuntimeError("advisory lock is not held")
    return connection


async def held_lock_backend_pid_for_test(lock: Any) -> int:
    """Read the real holder PID through the private test-held connection."""
    connection = held_lock_connection_for_test(lock)
    return int((await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def commit_held_lock_for_test(lock: Any) -> None:
    """Commit the private lock connection to prove session-lock survival."""
    await held_lock_connection_for_test(lock).commit()


async def simulate_lock_process_death_for_test(lock: Any) -> None:
    """Exercise the real private discard path without shipping a crash API."""
    from app.services.order_proposals.callback_inbox import locks as locks_module

    connection = getattr(lock, "_connection", None)
    if connection is None:
        return
    terminated, during = await locks_module._hard_discard(connection)  # noqa: SLF001
    if not terminated:
        raise locks_module.LockTerminationUnproven("simulate_process_death")
    lock._connection = None  # noqa: SLF001 - test-only crash bookkeeping
    if during is not None:
        raise during


def quarantined_handles_for_test() -> set[Any]:
    """Inspect the private R27 quarantine without exporting it from app code."""
    from app.services.order_proposals.callback_inbox import locks as locks_module

    return locks_module._QUARANTINE  # noqa: SLF001 - R27 retention assertion


@pytest_asyncio.fixture
async def inbox_cleanup(_bootstrap_test_schema) -> AsyncIterator[list[uuid.UUID]]:
    """Delete every fixture-owned inbox row, whatever the test outcome."""
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    owned: set[uuid.UUID] = set()
    token = _OWNED_INBOX_JOB_IDS.set(owned)
    created = _OwnedInboxJobIds(owned)
    try:
        yield created
    finally:
        try:
            if created:
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        delete(TelegramCallbackInboxJob).where(
                            TelegramCallbackInboxJob.job_id.in_(created)
                        )
                    )
                    await session.commit()
        finally:
            _OWNED_INBOX_JOB_IDS.reset(token)


async def load_job(job_id: uuid.UUID):
    """Read one inbox row through a fresh session (never a cached identity map)."""
    from sqlalchemy import select

    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(TelegramCallbackInboxJob).where(
                    TelegramCallbackInboxJob.job_id == job_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        session.expunge(row)
        return row


RETRY_BUDGET_CONSTRAINT = "ck_telegram_callback_inbox_retry_budget"

# R34's hostile-row fixtures model a row written before the fixed cross-field
# budget invariant existed.  The fixture below drops these checks only to
# insert rows it owns, then re-adds the *target* checks as NOT VALID before it
# yields control back to any production worker/service code.  PostgreSQL keeps
# legacy rows under NOT VALID but applies the checks to every UPDATE, which is
# exactly the real repair condition: terminalisation has to normalise the
# entire budget in the same statement as the scrub.
ATTEMPT_BUDGET_CONSTRAINTS: tuple[str, ...] = (
    "ck_telegram_callback_inbox_retry_budget",
    "ck_telegram_callback_inbox_attempt_count",
    "ck_telegram_callback_inbox_max_attempts",
)

_RETRY_BUDGET_SQL = (
    "CASE WHEN state = 'retry_wait' THEN attempt_count < max_attempts ELSE true END"
)

_R34_NOT_VALID_CHECKS: dict[str, str] = {
    "ck_telegram_callback_inbox_retry_budget": (
        f"CHECK ({_RETRY_BUDGET_SQL}) NOT VALID"
    ),
    "ck_telegram_callback_inbox_attempt_count": (
        "CHECK (attempt_count >= 0 AND attempt_count <= max_attempts) NOT VALID"
    ),
    "ck_telegram_callback_inbox_max_attempts": "CHECK (max_attempts = 3) NOT VALID",
}

_ATTEMPT_BUDGET_POISON_FIELDS: frozenset[str] = frozenset(
    {
        "state",
        "attempt_count",
        "max_attempts",
        "available_at",
        "started_at",
        "handler_entered_at",
        "handler_completed_at",
        "terminal_state_pending",
        "outcome",
        "error_class",
    }
)


@contextlib.asynccontextmanager
async def without_the_retry_budget_check() -> AsyncIterator[None]:
    """Briefly drop the retry-budget CHECK so a legacy-shaped row can commit.

    An exhausted ``retry_wait`` row is unconstructible once the constraint
    exists -- that is the point of it (R25). But a database written by an
    older binary can still hold one, and the recovery sweep has to cope, so
    the only honest way to test that half is to make the row the old way.

    Safe here: every xdist worker owns its own database, and the constraint is
    restored in ``finally``. Rows that would block the restore are test rows
    this helper's callers created, so they are cleared first.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "ALTER TABLE review.telegram_callback_inbox "
                f"DROP CONSTRAINT IF EXISTS {RETRY_BUDGET_CONSTRAINT}"
            )
        )
        await session.commit()
    try:
        yield
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "DELETE FROM review.telegram_callback_inbox "
                    "WHERE state = 'retry_wait' AND attempt_count >= max_attempts"
                )
            )
            await session.execute(
                text(
                    "ALTER TABLE review.telegram_callback_inbox "
                    f"ADD CONSTRAINT {RETRY_BUDGET_CONSTRAINT} "
                    f"CHECK ({_RETRY_BUDGET_SQL})"
                )
            )
            await session.commit()


class AttemptBudgetPoisonRows:
    """Owned legacy rows plus an explicit NOT VALID re-arm boundary.

    Calling :meth:`insert` is the sole time a test can write an invalid budget.
    It is intentionally separate from :meth:`enforce_for_processing`: every
    test must re-arm the desired R34 checks before calling worker, recovery, or
    either CAS service method.  The context manager validates the target
    checks on exit after removing only still-invalid rows this instance owns.
    """

    def __init__(self) -> None:
        self._owned_job_ids: list[uuid.UUID] = []
        self._enforced = False

    async def insert(self, job_id: uuid.UUID, **fields: Any) -> None:
        if self._enforced:
            raise RuntimeError("attempt budget poison fixture is already re-armed")
        unknown = set(fields) - _ATTEMPT_BUDGET_POISON_FIELDS
        if unknown:
            raise ValueError(f"unexpected poison field names: {sorted(unknown)}")
        if not fields:
            raise ValueError("a poison insert needs explicit fields")

        if job_id not in self._owned_job_ids:
            self._owned_job_ids.append(job_id)
        assignments = ", ".join(f"{field} = :{field}" for field in sorted(fields))
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "UPDATE review.telegram_callback_inbox "
                    f"SET {assignments} WHERE job_id = :job_id"
                ),
                {"job_id": job_id, **fields},
            )
            if result.rowcount != 1:
                raise LookupError(f"owned poison row was not found: {job_id}")
            await session.commit()

    async def enforce_for_processing(self) -> None:
        """Install fixed R34 checks as NOT VALID before production code runs."""
        if self._enforced:
            return
        async with AsyncSessionLocal() as session:
            for name in ATTEMPT_BUDGET_CONSTRAINTS:
                await session.execute(
                    text(
                        "ALTER TABLE review.telegram_callback_inbox "
                        f"ADD CONSTRAINT {name} {_R34_NOT_VALID_CHECKS[name]}"
                    )
                )
            await session.commit()
        self._enforced = True

    async def run_after_enforcing(self, operation: Callable[[], Any]) -> Any:
        """Run production code only after target checks protect every UPDATE."""
        await self.enforce_for_processing()
        assert self._enforced
        return await operation()

    async def cleanup_and_validate(self) -> None:
        """Remove only this fixture's unhealed rows, then validate R34 DDL."""
        if not self._enforced:
            await self.enforce_for_processing()
        async with AsyncSessionLocal() as session:
            for job_id in self._owned_job_ids:
                await session.execute(
                    text(
                        "DELETE FROM review.telegram_callback_inbox "
                        "WHERE job_id = :job_id "
                        "AND (max_attempts <> 3 "
                        "OR attempt_count < 0 "
                        "OR attempt_count > max_attempts "
                        "OR (state = 'retry_wait' "
                        "AND attempt_count >= max_attempts))"
                    ),
                    {"job_id": job_id},
                )
            for name in ATTEMPT_BUDGET_CONSTRAINTS:
                await session.execute(
                    text(
                        "ALTER TABLE review.telegram_callback_inbox "
                        f"VALIDATE CONSTRAINT {name}"
                    )
                )
            await session.commit()


@contextlib.asynccontextmanager
async def attempt_budget_poison_rows() -> AsyncIterator[AttemptBudgetPoisonRows]:
    """Insert owned legacy rows, then require a NOT VALID re-arm before use."""
    async with AsyncSessionLocal() as session:
        for name in ATTEMPT_BUDGET_CONSTRAINTS:
            await session.execute(
                text(
                    f"ALTER TABLE review.telegram_callback_inbox DROP CONSTRAINT {name}"
                )
            )
        await session.commit()

    rows = AttemptBudgetPoisonRows()
    try:
        yield rows
    finally:
        await rows.cleanup_and_validate()
