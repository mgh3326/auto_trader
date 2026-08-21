"""Durable ingress: authenticate, validate, commit, ACK.

The order is the contract:

1. the webhook token middleware has already run (``AuthMiddleware``);
2. shape / callback-data parser / action allowlist / chat allowlist, via the
   *existing* :func:`normalize_callback_update` -- no new parser, no new
   action vocabulary;
3. insert the normalized envelope and **commit**;
4. a bounded, best-effort Redis kick;
5. 200.

A failure at step 3 raises :class:`CallbackInboxUnavailable`, the caller
answers a sanitized 503, and step 4 never happens -- Telegram retries and
nothing has been half-accepted. A failure at step 4 changes nothing: the
committed row is the durable acknowledgement, and the recovery sweep will find
it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.order_proposals.callback_inbox.contracts import (
    DeliveryIdentityMissing,
    build_update_digest,
)
from app.services.order_proposals.callback_inbox.service import CallbackInboxService
from app.services.order_proposals.telegram_callback import (
    CallbackNotNormalizable,
    NormalizedCallback,
    normalize_callback_update,
)

logger = logging.getLogger(__name__)

EnqueueFn = Callable[[uuid.UUID], Awaitable[None]]
SessionFactory = Callable[[], Any]


class CallbackInboxUnavailable(Exception):
    """The durable envelope could not be committed.

    Carries no detail the caller may surface: the HTTP layer turns this into a
    generic 503 precisely so a driver message or a row value cannot reach a
    Telegram-visible response.
    """


@dataclass(frozen=True, slots=True)
class IngressResult:
    accepted: bool
    duplicate: bool
    job_id: uuid.UUID | None
    reason: str
    enqueued: bool


async def _default_enqueue(job_id: uuid.UUID) -> None:
    """Kick the per-job worker. Imported lazily to avoid an import cycle."""
    from app.tasks.telegram_callback_inbox_tasks import run_telegram_callback_job

    await run_telegram_callback_job.kiq(str(job_id))


def _round_trippable(normalized: NormalizedCallback) -> bool:
    """Refuse anything the worker could not faithfully rebuild.

    The inline path passes ``chat_id``/``message_id`` straight through to the
    notifier. Storing them means round-tripping them through text/bigint
    columns, so an update whose values do not survive that trip is rejected
    rather than stored in a shape the worker would have to guess at.
    """
    chat_id = normalized.chat_id
    if not isinstance(chat_id, int | str) or isinstance(chat_id, bool):
        return False
    if not str(chat_id).strip():
        return False
    message_id = normalized.message_id
    if message_id is not None and (
        isinstance(message_id, bool) or not isinstance(message_id, int)
    ):
        return False
    callback_query_id = normalized.callback_query_id
    return not (
        callback_query_id is not None and not isinstance(callback_query_id, str)
    )


def _matches(row: Any, normalized: NormalizedCallback) -> bool:
    """Is the stored row the same call, or a different one reusing the id?"""
    callback = normalized.callback
    return (
        row.chat_id == str(normalized.chat_id)
        and row.action == callback.action
        and row.subject_short == callback.subject_short
        and row.dispatch_attempt_id == callback.attempt_id
        and row.membership_revision == callback.membership_revision
        and row.membership_digest == callback.membership_digest
        and row.nonce == callback.nonce
    )


async def ingest_callback_update(
    update: dict[str, Any],
    *,
    now: datetime,
    session_factory: SessionFactory | None = None,
    enqueue_fn: EnqueueFn | None = None,
    enqueue_timeout_seconds: float | None = None,
) -> IngressResult:
    """Persist one approval click and best-effort kick its worker."""
    try:
        normalized = normalize_callback_update(update)
    except CallbackNotNormalizable as rejection:
        return IngressResult(False, False, None, rejection.reason, False)

    if not _round_trippable(normalized):
        return IngressResult(False, False, None, "unstorable_envelope", False)

    callback_query = update.get("callback_query") or {}
    try:
        update_digest = build_update_digest(
            update_id=update.get("update_id"),
            callback_query_id=callback_query.get("id"),
        )
    except DeliveryIdentityMissing:
        return IngressResult(False, False, None, "no_delivery_identity", False)

    factory = session_factory or AsyncSessionLocal
    try:
        job_id, duplicate, conflict = await _persist(
            factory, normalized=normalized, update_digest=update_digest, now=now
        )
    except CallbackInboxUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - one sanitized failure mode
        logger.error(
            "order_proposals.telegram.callback_inbox_persist_failed",
            extra={"exception_type": type(exc).__name__},
        )
        raise CallbackInboxUnavailable("callback inbox persist failed") from exc

    if conflict:
        # Same delivery identity, different call. Not a benign retry: do not
        # overwrite, do not queue, do not report it as already-accepted.
        logger.error("order_proposals.telegram.callback_delivery_conflict")
        return IngressResult(False, False, None, "delivery_conflict", False)

    if duplicate:
        return IngressResult(True, True, job_id, "duplicate", False)

    enqueued = await _kick(
        job_id,
        enqueue_fn=enqueue_fn or _default_enqueue,
        timeout_seconds=(
            enqueue_timeout_seconds
            if enqueue_timeout_seconds is not None
            else settings.ORDER_PROPOSALS_TELEGRAM_CALLBACK_ENQUEUE_TIMEOUT_SECONDS
        ),
    )
    return IngressResult(True, False, job_id, "queued", enqueued)


async def _persist(
    factory: SessionFactory,
    *,
    normalized: NormalizedCallback,
    update_digest: str,
    now: datetime,
) -> tuple[uuid.UUID | None, bool, bool]:
    """Insert, or resolve an existing row. Returns ``(job_id, dup, conflict)``."""
    callback = normalized.callback
    async with factory() as session:
        service = CallbackInboxService(session)
        try:
            row = await service.enqueue(
                update_digest=update_digest,
                now=now,
                callback_query_id=normalized.callback_query_id,
                chat_id=str(normalized.chat_id),
                message_id=normalized.message_id,
                telegram_user_id=(
                    None
                    if normalized.telegram_user_id is None
                    else str(normalized.telegram_user_id)
                ),
                action=callback.action,
                subject_short=callback.subject_short,
                dispatch_attempt_id=callback.attempt_id,
                membership_revision=callback.membership_revision,
                membership_digest=callback.membership_digest,
                nonce=callback.nonce,
            )
            job_id = row.job_id
            await session.commit()
            return job_id, False, False
        except IntegrityError:
            await session.rollback()

    # Read the winner back through an INDEPENDENT session: the one above is
    # poisoned by the failed insert, and a rolled-back session cannot be
    # trusted to report what actually committed.
    async with factory() as session:
        existing = await CallbackInboxService(session).get_by_update_digest(
            update_digest
        )
        if existing is None:
            # The unique violation came from somewhere we cannot explain.
            raise CallbackInboxUnavailable("callback inbox dedupe unresolved")
        matched = _matches(existing, normalized)
        job_id = existing.job_id
        await session.rollback()
    return (job_id, True, False) if matched else (None, False, True)


async def _kick(
    job_id: uuid.UUID, *, enqueue_fn: EnqueueFn, timeout_seconds: float
) -> bool:
    """Best-effort, bounded. A dead broker costs latency, never a click."""
    try:
        await asyncio.wait_for(enqueue_fn(job_id), timeout=max(timeout_seconds, 0.01))
    except TimeoutError:
        logger.error(
            "order_proposals.telegram.callback_job_enqueue_timeout",
            extra={"callback_job.id": str(job_id)},
        )
        return False
    except Exception as exc:  # noqa: BLE001 - the row is already durable
        logger.error(
            "order_proposals.telegram.callback_job_enqueue_failed",
            extra={
                "callback_job.id": str(job_id),
                "exception_type": type(exc).__name__,
            },
        )
        return False
    return True


__all__ = [
    "CallbackInboxUnavailable",
    "IngressResult",
    "ingest_callback_update",
]
