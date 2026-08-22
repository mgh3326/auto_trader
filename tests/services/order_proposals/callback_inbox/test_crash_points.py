"""W5 — the crash matrix: total broker mutations must never exceed one.

Adversarial review R2. For every point at which the worker process can die
during one callback, this file kills it there, lets recovery do whatever it
would do, and then counts. The count is the whole test: at no crash point may
the pair (first attempt + recovery) produce two submissions.

Why the crash is a ``BaseException``
------------------------------------
``handle_normalized_callback`` catches ``Exception`` and reports
``internal_error``. A ``RuntimeError`` therefore simulates a *handled* failure,
not a dead process. Every crash here raises a class derived from
``BaseException`` so the core cannot swallow it and the stack unwinds exactly
as it would on a real kill, including the advisory lock's release.

Why the fake broker is the counter
----------------------------------
Real submission is fenced by a rung-stable idempotency key
(``review.order_send_intents`` for KIS, ``identifier`` for Upbit,
``clientOrderId`` for Toss). That fence is real and load-bearing, but it lives
one layer below this feature. Counting a *fake* submission removes that safety
net on purpose: if the inbox's own algebra can double-submit, this file fails
even though production might have been rescued downstream.
"""

from __future__ import annotations

import functools
import uuid
from datetime import timedelta

import pytest

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import telegram_callback as callback_module
from app.services.order_proposals.revalidation import RungOutcome
from app.services.order_proposals.telegram_callback import (
    handle_normalized_callback,
)

from .conftest import (
    FakeNotifier,
    load_job,
    make_update,
    proposal_callback_data,
    seed_proposal,
    shape_owned_callback_inbox_row,
)

pytestmark = pytest.mark.integration


class ProcessDied(BaseException):
    """A kill -9, not a bug the callback core is allowed to report."""


class _Broker:
    """Counts simulated submissions; optionally dies right after one."""

    def __init__(self, *, die_after_send: bool = False) -> None:
        self.sends = 0
        self._die_after_send = die_after_send

    async def __call__(self, *, service, proposal_id, now, **kwargs):
        self.sends += 1
        if self._die_after_send:
            raise ProcessDied
        return [RungOutcome(0, "submitted_acked", {})]


class _DiesBeforeSend(_Broker):
    async def __call__(self, *, service, proposal_id, now, **kwargs):
        raise ProcessDied


async def _queue(inbox_cleanup: list[uuid.UUID], group) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 950_000 + uuid.uuid4().int % 100_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(
            data=proposal_callback_data(group),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_no_kick,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


async def _age_for_recovery(job_id: uuid.UUID) -> None:
    """A crashed worker's row: no lock held, old enough for the scan filter."""
    async with AsyncSessionLocal() as session:
        await shape_owned_callback_inbox_row(
            session, job_id, started_at=now_kst() - timedelta(hours=6)
        )
        await session.commit()


async def _nonce_consumed(proposal_id: uuid.UUID) -> bool:
    async with AsyncSessionLocal() as session:
        group, _ = await OrderProposalsService(session).get_proposal(proposal_id)
        return group.approval_nonce_used_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crash_point", "nonce"),
    [
        ("after_claim_commit", "crashpt000"),
        ("before_preflight", "crashpt001"),
        ("after_preflight", "crashpt002"),
        ("after_nonce_consume_before_broker", "crashpt003"),
        ("after_broker_send", "crashpt004"),
        ("after_outcome_recorded_before_core_commit", "crashpt005"),
        ("after_core_commit_before_job_terminal", "crashpt006"),
    ],
)
async def test_no_crash_point_can_produce_a_second_submission(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    crash_point: str,
    nonce: str,
) -> None:
    from app.services.order_proposals.callback_inbox import service as service_module
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_CLAIMABLE_STATES,
    )
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce=nonce, symbol="CRSKR")
    job_id = await _queue(inbox_cleanup, group)

    broker = _Broker()
    notifier = FakeNotifier()
    handler = functools.partial(
        handle_normalized_callback, revalidate_fn=broker, notifier=notifier
    )

    # A scoped context so undoing the crash injection cannot also undo the
    # autouse fixtures (chat allowlist, deterministic approval window) that
    # the recovery leg below still depends on.
    with pytest.MonkeyPatch.context() as crash:
        if crash_point == "after_claim_commit":

            def _die(*args, **kwargs):
                raise ProcessDied

            crash.setattr(worker_module, "resolve_notifier", _die)

        elif crash_point == "before_preflight":

            async def _die_preflight(**kwargs):
                raise ProcessDied

            crash.setattr(
                callback_module, "_preflight_proposal_callback", _die_preflight
            )

        elif crash_point == "after_preflight":
            original_preflight = callback_module._preflight_proposal_callback

            async def _after(**kwargs):
                await original_preflight(**kwargs)
                raise ProcessDied

            crash.setattr(callback_module, "_preflight_proposal_callback", _after)

        elif crash_point == "after_nonce_consume_before_broker":
            broker = _DiesBeforeSend()
            handler = functools.partial(
                handle_normalized_callback, revalidate_fn=broker, notifier=notifier
            )

        elif crash_point == "after_broker_send":
            broker = _Broker(die_after_send=True)
            handler = functools.partial(
                handle_normalized_callback, revalidate_fn=broker, notifier=notifier
            )

        elif crash_point == "after_outcome_recorded_before_core_commit":

            def _die_summary(outcomes):
                raise ProcessDied

            crash.setattr(callback_module, "_build_result_summary", _die_summary)

        elif crash_point == "after_core_commit_before_job_terminal":

            async def _die_verdict(self, *args, **kwargs):
                raise ProcessDied

            crash.setattr(
                service_module.CallbackInboxService,
                "record_handler_verdict",
                _die_verdict,
            )

        with pytest.raises(ProcessDied):
            await process_callback_job(job_id, handler=handler)

    sends_before_recovery = broker.sends
    assert sends_before_recovery <= 1

    await _age_for_recovery(job_id)

    # A brand-new worker picks the row up exactly as recovery would.
    recovered_broker = _Broker()
    recovered = await process_callback_job(
        job_id,
        handler=functools.partial(
            handle_normalized_callback,
            revalidate_fn=recovered_broker,
            notifier=FakeNotifier(),
        ),
        claimable_states=RECOVERY_CLAIMABLE_STATES,
    )

    total = sends_before_recovery + recovered_broker.sends
    assert total <= 1, (
        f"crash point {crash_point!r} produced {total} submissions "
        f"({sends_before_recovery} before recovery, "
        f"{recovered_broker.sends} after; status={recovered['status']})"
    )

    row = await load_job(job_id)
    assert row is not None
    assert row.state in {"succeeded", "discarded", "dead_letter", "retry_wait"}

    if crash_point == "after_claim_commit":
        # Provably pre-entry: recovery may legitimately run it once.
        assert recovered_broker.sends == 1
        assert row.state == "succeeded"
    else:
        # Everything else entered the core, so replay is refused outright.
        assert recovered_broker.sends == 0
        assert row.state == "dead_letter"
        assert row.error_class == "handler_ambiguous"
        assert row.nonce is None
        # The rollback left the approval unspent, which is exactly why a
        # naive "the nonce is free, so re-run it" rule would double-submit.
        if crash_point != "after_core_commit_before_job_terminal":
            assert await _nonce_consumed(group.proposal_id) is False
