"""#1067 -- the post-commit boundary owns the one delayed price re-evaluation.

These tests pin *where* the 30s wait runs (a detached task, never inline in
``dispatch_proposal``), that only a first pass may arm it, that cancellation
still produces a card, and that a deferred card is not reported as a failure.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.mcp_server.tooling import order_proposal_tools
from app.monitoring.trade_notifier import notifier as notifier_module
from app.services.order_proposals.auto_approve_price_fallback import (
    AUTO_APPROVE_PRICE_RETRY_DELAY_SECONDS,
)
from app.services.order_proposals.dispatch import (
    AUTO_APPROVE_PRICE_RETRY_SCHEDULED,
    AUTO_APPROVE_PRICE_RETRY_SUPERSEDED,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalDispatchState,
    TelegramDispatchResult,
)

PROPOSAL_ID = uuid.UUID("b1067000-0000-4000-8000-000000000067")


def _result(state, failure_code=None):
    return TelegramDispatchResult(
        state=state,
        message_id=None,
        status_code=None,
        error_code=None,
        error_classification=None,
        payload_chars=0,
        failure_code=failure_code,
    )


@pytest.fixture
def telegram_on(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True)
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "c1")
    monkeypatch.setattr(
        notifier_module, "get_trade_notifier", lambda: SimpleNamespace()
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_pass_arms_the_scheduler_and_retry_pass_never_does(
    monkeypatch, telegram_on
):
    seen: list[dict] = []

    async def fake_dispatch(proposal_id, **kwargs):
        seen.append(kwargs)
        return _result(ApprovalDispatchState.SENT_CURRENT)

    monkeypatch.setattr(order_proposal_tools, "dispatch_proposal", fake_dispatch)

    await order_proposal_tools._dispatch_after_proposal_commit(PROPOSAL_ID)
    await order_proposal_tools._dispatch_after_proposal_commit(
        PROPOSAL_ID, price_retry_token="tok"
    )
    await order_proposal_tools._dispatch_after_proposal_commit(
        PROPOSAL_ID, price_retry_token="tok", price_retry_card_only=True
    )

    first, retry, card_only = seen
    assert (
        first["price_retry_scheduler"]
        is order_proposal_tools._schedule_auto_approve_price_retry
    )
    assert first["price_retry_token"] is None
    assert retry["price_retry_scheduler"] is None
    assert retry["price_retry_token"] == "tok"
    assert retry["price_retry_card_only"] is False
    assert card_only["price_retry_scheduler"] is None
    assert card_only["price_retry_card_only"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_returns_immediately_and_tracks_the_task(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[tuple] = []

    async def fake_run(proposal_id, token):
        runs.append((proposal_id, token))
        started.set()
        await release.wait()

    monkeypatch.setattr(order_proposal_tools, "_run_auto_approve_price_retry", fake_run)

    order_proposal_tools._schedule_auto_approve_price_retry(PROPOSAL_ID, "tok")
    # Scheduling did not wait for the retry: nothing has run yet.
    assert runs == []
    [task] = [
        t
        for t in order_proposal_tools._PRICE_RETRY_TASKS
        if t.get_name().endswith(str(PROPOSAL_ID))
    ]
    await asyncio.wait_for(started.wait(), 1)
    assert runs == [(PROPOSAL_ID, "tok")]
    release.set()
    await asyncio.wait_for(task, 1)
    assert task not in order_proposal_tools._PRICE_RETRY_TASKS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runner_waits_thirty_seconds_then_re_evaluates(monkeypatch):
    slept: list[float] = []
    completed: list[tuple] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    async def fake_dispatch(proposal_id, **kwargs):
        completed.append((proposal_id, kwargs))
        return _result(ApprovalDispatchState.SENT_CURRENT)

    monkeypatch.setattr(
        order_proposal_tools, "_dispatch_after_proposal_commit", fake_dispatch
    )

    await order_proposal_tools._run_auto_approve_price_retry(
        PROPOSAL_ID, "tok", sleep=fake_sleep
    )

    assert AUTO_APPROVE_PRICE_RETRY_DELAY_SECONDS == 30
    assert slept == [30]
    assert completed == [
        (PROPOSAL_ID, {"price_retry_token": "tok", "price_retry_card_only": False})
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_wait_sends_card_only_then_propagates(monkeypatch):
    completed: list[dict] = []

    async def cancelled_sleep(_seconds):
        raise asyncio.CancelledError

    async def fake_dispatch(proposal_id, **kwargs):
        completed.append(kwargs)
        return _result(ApprovalDispatchState.SENT_CURRENT)

    monkeypatch.setattr(
        order_proposal_tools, "_dispatch_after_proposal_commit", fake_dispatch
    )

    with pytest.raises(asyncio.CancelledError):
        await order_proposal_tools._run_auto_approve_price_retry(
            PROPOSAL_ID, "tok", sleep=cancelled_sleep
        )
    assert completed == [{"price_retry_token": "tok", "price_retry_card_only": True}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_exception_is_contained(monkeypatch):
    async def fake_sleep(_seconds):
        return None

    async def broken_dispatch(proposal_id, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(
        order_proposal_tools, "_dispatch_after_proposal_commit", broken_dispatch
    )
    # Detached task: it must log, never raise into the event loop.
    await order_proposal_tools._run_auto_approve_price_retry(
        PROPOSAL_ID, "tok", sleep=fake_sleep
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "alerted"),
    [
        (
            _result(ApprovalDispatchState.PENDING, AUTO_APPROVE_PRICE_RETRY_SCHEDULED),
            False,
        ),
        (
            _result(
                ApprovalDispatchState.FAILED_SUPERSEDED,
                AUTO_APPROVE_PRICE_RETRY_SUPERSEDED,
            ),
            False,
        ),
        (_result(ApprovalDispatchState.SENT_CURRENT), False),
        (_result(ApprovalDispatchState.FAILED, "telegram_error_400"), True),
        # Look-alikes of the two codes on other states still alert.
        (
            _result(ApprovalDispatchState.FAILED, AUTO_APPROVE_PRICE_RETRY_SCHEDULED),
            True,
        ),
        (
            _result(ApprovalDispatchState.PENDING, AUTO_APPROVE_PRICE_RETRY_SUPERSEDED),
            True,
        ),
    ],
)
async def test_deferred_card_is_not_alerted_as_a_delivery_failure(
    monkeypatch, result, alerted
):
    alerts: list[dict] = []

    async def fake_alert(proposal_id, **kwargs):
        alerts.append(kwargs)
        return {"state": "sent"}

    monkeypatch.setattr(order_proposal_tools, "_alert_non_sent_dispatch", fake_alert)

    payload = await order_proposal_tools._approval_dispatch_payload(PROPOSAL_ID, result)

    assert bool(alerts) is alerted
    assert ("operator_alert" in payload) is alerted
    assert payload["failure_code"] == result.failure_code
