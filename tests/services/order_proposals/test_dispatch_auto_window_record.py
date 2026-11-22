"""hk 6062: the auto-approve dispatch path ledgers a blocked approval window.

No database: a fake service records the call sequence so the blocked branch is
proven to (a) write the same ``failed`` + ``<CODE>/<detail>`` dispatch ledger
the manual path writes, and (b) touch no broker, nonce, notifier or revalidation
function. DB round trips live in ``test_dispatch.py`` (CI).
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.order_proposals import dispatch as dispatch_module
from app.services.order_proposals.approval_window import (
    ApprovalWindowCode,
    ApprovalWindowDecision,
    SubmissionSessionEvidence,
)

NOW = datetime(2026, 11, 22, 1, 0, tzinfo=UTC)


class _FakeService:
    instances: list[_FakeService] = []

    valid_until: datetime | None = NOW + timedelta(hours=5)

    def __init__(self, session):
        self.calls: list[tuple[str, dict]] = []
        self.group = SimpleNamespace(
            proposal_id=uuid.uuid4(),
            approval_nonce=None,
            approval_dispatch_membership_revision=0,
            valid_until=_FakeService.valid_until,
        )
        _FakeService.instances.append(self)

    async def acquire_auto_dispatch_lock(self, proposal_id):
        self.calls.append(("lock", {}))

    async def get_proposal(self, proposal_id):
        return self.group, [SimpleNamespace(state="pending_approval")]

    async def start_approval_dispatch(self, proposal_id, **kwargs):
        self.calls.append(("start", kwargs))

    async def finish_approval_dispatch(self, proposal_id, **kwargs):
        self.calls.append(("finish", kwargs))
        return SimpleNamespace(
            ok=False, failure_code=kwargs["publication"].failure_code
        )

    async def expire_mutable_rungs_if_needed(self, proposal_id, *, now):
        self.calls.append(("expire", {"now": now}))

    def __getattr__(self, name):  # any other service call on this branch is a bug
        raise AssertionError(f"unexpected service call on blocked branch: {name}")


class _Session:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _Trap:
    def __init__(self, name):
        self.name = name

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"{self.name} must not be called on a blocked window")

    def __getattr__(self, attr):
        raise AssertionError(f"{self.name}.{attr} must not be used when blocked")


def _decision(code: ApprovalWindowCode, detail: str) -> ApprovalWindowDecision:
    return ApprovalWindowDecision(
        code=code,
        observed_at=NOW,
        valid_until=NOW + timedelta(hours=5),
        policy_stamp="stamp-1",
        market="equity_kr",
        account_mode="kis_live",
        action="place",
        order_type="limit",
        evidence=SubmissionSessionEvidence(
            known=code is not ApprovalWindowCode.CALENDAR_UNKNOWN,
            source="test",
            current_session="closed",
            allowed_sessions=("regular",),
            allowed_now=False,
            detail=None,
        ),
        detail=detail,
    )


BLOCKS = [
    (ApprovalWindowCode.DEFER_SESSION_CLOSED, "submission_session_closed"),
    (
        ApprovalWindowCode.NO_EXECUTABLE_WINDOW,
        "valid_until_not_after_next_allowed_session",
    ),
    (ApprovalWindowCode.CALENDAR_UNKNOWN, "nxt_capability_stale"),
    (ApprovalWindowCode.INVALID_VALID_UNTIL, "valid_until_missing"),
    (ApprovalWindowCode.EXPIRED, "now_at_or_after_valid_until"),
]


@pytest.fixture
def traps(monkeypatch):
    _FakeService.instances.clear()
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_AUTO_APPROVE", True)
    monkeypatch.setattr(dispatch_module, "OrderProposalsService", _FakeService)
    monkeypatch.setattr(dispatch_module, "_generate_nonce", _Trap("nonce"))
    monkeypatch.setattr(dispatch_module, "publish_approval_messages", _Trap("card"))
    monkeypatch.setattr(
        dispatch_module, "send_proposal_for_approval", _Trap("manual sender")
    )
    return SimpleNamespace(
        notifier=_Trap("notifier"),
        revalidate=_Trap("revalidate"),
        cancel=_Trap("cancel_target"),
        fetch=_Trap("fetch_target"),
        veto=_Trap("toss_veto_reconcile"),
        price=_Trap("price_fallback"),
    )


# The boundary re-samples validity from the group itself, so the two validity
# codes are produced by the group's ``valid_until`` (as in production).
_VALID_UNTIL = {
    ApprovalWindowCode.INVALID_VALID_UNTIL: None,
    ApprovalWindowCode.EXPIRED: NOW,
}


async def _run(code, detail, traps):
    session = _Session()
    _FakeService.valid_until = _VALID_UNTIL.get(code, NOW + timedelta(hours=5))

    @contextlib.asynccontextmanager
    async def factory():
        yield session

    async def evaluator(group, *, now):
        return _decision(code, detail)

    result = await dispatch_module.dispatch_proposal(
        uuid.uuid4(),
        notifier=traps.notifier,
        now=NOW,
        service_factory=factory,
        revalidate_fn=traps.revalidate,
        cancel_target_fn=traps.cancel,
        fetch_target_fn=traps.fetch,
        toss_veto_reconcile_fn=traps.veto,
        window_evaluator=evaluator,
        price_fallback_fn=traps.price,
    )
    return result, session, _FakeService.instances[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "detail"), BLOCKS)
async def test_auto_path_ledgers_every_window_block_like_manual(traps, code, detail):
    result, session, service = await _run(code, detail, traps)

    assert isinstance(result, ApprovalWindowDecision)
    assert result.code is code
    names = [name for name, _ in service.calls]
    expected = ["lock", "start", "finish"] + (
        ["expire"] if code is ApprovalWindowCode.EXPIRED else []
    )
    assert names == expected
    start = dict(service.calls[1][1])
    finish = dict(service.calls[2][1])
    # one fenced attempt shared by start and finish
    assert start["attempt_id"] == finish["attempt_id"]
    assert start["binding"].attempt_id == start["attempt_id"]
    publication = finish["publication"]
    assert publication.card_published is False
    assert publication.failure_code == f"{code.value}/{detail}"
    assert finish["chat_id"] is None
    assert finish["approval_window_policy_stamp"] == "stamp-1"
    assert start["payload_chars"] == 0
    assert session.commits == 1


@pytest.mark.asyncio
async def test_auto_path_window_block_failure_code_matches_manual_helper(traps):
    """Same helper as the manual branch: the code is exactly the manual one."""
    decision = _decision(ApprovalWindowCode.DEFER_SESSION_CLOSED, "x")
    assert (
        dispatch_module.approval_window_failure_code(decision)
        == "DEFER_SESSION_CLOSED/x"
    )
