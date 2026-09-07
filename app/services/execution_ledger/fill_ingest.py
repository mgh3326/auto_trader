"""Shared execution-ledger fill ingest orchestration (fillwire P0).

Before this module the websocket monitor owned the whole post-upsert story:
commit the row, project the Upbit proposal rung, then decide whether the fill
still deserves a notification. The Go ``fillwire`` daemon writes through
``POST /trading/api/execution-ledger/fills/ingest`` instead of the DB, so that
story had to become callable from two places without being written twice.

Everything here is deliberately *hook-injectable*: ``websocket_monitor``
delegates with its own module-level ``AsyncSessionLocal`` /
``ExecutionLedgerRepository`` / ``OrderProposalsService`` /
``get_trade_notifier`` / ``fetch_fill_enrichment`` names so the monitor's
existing patch points keep working unchanged, while the HTTP route gets the
same behaviour from the defaults.

Invariants preserved from the monitor:

* the ledger commit is authoritative — downstream work is best-effort and a
  downstream failure never rolls back or re-inserts a committed fill;
* a duplicate row (``updated``/``unchanged``) suppresses the notification,
  except the Upbit small-fill proposal-rung recovery case;
* the Upbit proposal projection runs on its own committed session.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.db import AsyncSessionLocal
from app.monitoring.trade_notifier import get_trade_notifier
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.execution_ledger.repository import (
    ExecutionLedgerRepository,
    UpsertStatus,
)
from app.services.fill_enrichment import fetch_fill_enrichment
from app.services.fill_notification import FillOrder, is_fill_notifiable
from app.services.order_proposals import OrderProposalsService

logger = logging.getLogger(__name__)

#: Upsert statuses that mean "this fill was already durable" and therefore must
#: not raise a second notification.
DUPLICATE_STATUSES: frozenset[str] = frozenset({"updated", "unchanged"})


async def commit_fill(
    fill: ExecutionLedgerUpsert,
    *,
    session_factory: Callable[[], Any] | None = None,
    repository_cls: type[ExecutionLedgerRepository] | None = None,
) -> tuple[UpsertStatus, int]:
    """Durably upsert one fill through the repository and commit it.

    This is the only ledger write both the websocket monitor and the HTTP
    ingest route use, so the ``(broker, account_mode, venue, broker_order_id,
    fill_seq)`` idempotency key stays identical on both paths.
    """
    factory = session_factory or AsyncSessionLocal
    repo_cls = repository_cls or ExecutionLedgerRepository
    async with factory() as db:
        status, row_id = await repo_cls(db).upsert_fill(fill)
        await db.commit()
    return status, row_id


async def project_upbit_proposal_fill(
    order_data: dict[str, Any],
    *,
    session_factory: Callable[[], Any] | None = None,
    proposals_service_cls: Any = None,
) -> bool:
    """Best-effort projection of committed Upbit evidence into one rung."""
    factory = session_factory or AsyncSessionLocal
    service_cls = proposals_service_cls or OrderProposalsService

    state = str(order_data.get("state") or "")
    terminal_state = {
        "trade": "partially_filled",
        "done": "filled",
    }.get(state)
    if terminal_state is None:
        return False

    broker_order_id = str(order_data.get("uuid") or "").strip() or None
    identifier = str(order_data.get("identifier") or "").strip() or None
    try:
        filled_qty = Decimal(str(order_data.get("executed_volume") or "0"))
        if filled_qty <= 0:
            logger.info(
                "Upbit proposal rung projection skipped: missing cumulative fill "
                "order_id=%s identifier=%s state=%s",
                broker_order_id,
                identifier,
                state,
            )
            return False
        async with factory() as db:
            rung = await service_cls(db).record_fill_evidence(
                idempotency_key=identifier,
                broker_order_id=broker_order_id,
                filled_qty=filled_qty,
                terminal_state=terminal_state,
                now=datetime.now(UTC),
                account_mode="upbit",
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - ledger commit remains authoritative
        logger.error(
            "Upbit proposal rung projection failed: order_id=%s identifier=%s "
            "state=%s error=%s",
            broker_order_id,
            identifier,
            state,
            exc,
            exc_info=True,
        )
        return False

    if rung is None:
        logger.info(
            "Upbit proposal rung projection found no matching proposal rung: "
            "order_id=%s identifier=%s state=%s",
            broker_order_id,
            identifier,
            state,
        )
        return False
    logger.info(
        "Upbit proposal rung projected: order_id=%s identifier=%s state=%s "
        "rung_state=%s cumulative_filled_qty=%s",
        broker_order_id,
        identifier,
        state,
        rung.state,
        filled_qty,
    )
    return True


async def send_fill_notification(
    order: FillOrder,
    *,
    correlation_id: str | None = None,
    proposal_rung_fill: bool = False,
    notifier_factory: Callable[[], Any] | None = None,
    enrichment_fetcher: Callable[[FillOrder], Awaitable[Any]] | None = None,
) -> bool:
    """체결 알림: 통화 임계 → best-effort 보강 → TradeNotifier (fire-and-forget).

    Returns whether the notifier reported a delivered message, so the caller
    (the monitor) can keep owning its own forwarded-fill counters.
    """
    notifier = notifier_factory or get_trade_notifier
    fetch_enrichment = enrichment_fetcher or fetch_fill_enrichment

    if not proposal_rung_fill and not is_fill_notifiable(order):
        logger.info(
            "Fill notification skipped: below threshold symbol=%s amount=%s currency=%s",
            order.symbol,
            order.filled_amount,
            order.currency,
        )
        return False

    enrichment = None
    try:
        enrichment = await fetch_enrichment(order)
    except Exception:
        logger.warning(
            "Fill enrichment error (fail-open): symbol=%s",
            order.symbol,
            exc_info=True,
        )

    from app.core.portfolio_links import build_position_detail_url

    detail_url = build_position_detail_url(order.symbol, order.market_type)

    logger.info(
        "Fill notification send start: correlation_id=%s symbol=%s account=%s amount=%s",
        correlation_id,
        order.symbol,
        order.account,
        order.filled_amount,
    )
    try:
        ok = await notifier().notify_fill(
            order,
            enrichment=enrichment,
            detail_url=detail_url,
        )
        if ok:
            logger.info(
                "Fill notification sent: correlation_id=%s symbol=%s result=success",
                correlation_id,
                order.symbol,
            )
            return True
        logger.warning(
            "Fill notification not delivered: correlation_id=%s symbol=%s",
            correlation_id,
            order.symbol,
        )
        return False
    except Exception as e:
        logger.error(
            "Fill notification error: correlation_id=%s symbol=%s error=%s",
            correlation_id,
            order.symbol,
            e,
            exc_info=True,
        )
        return False


@dataclass(frozen=True)
class DownstreamHooks:
    """Callables the post-upsert orchestration drives.

    The websocket monitor injects its own bound methods (so its counters and
    the existing test patch points keep working); the HTTP route leaves these
    ``None`` and gets the module-level implementations above.
    """

    project_upbit_proposal_fill: Callable[[dict[str, Any]], Awaitable[bool]] | None = (
        None
    )
    send_fill_notification: Callable[..., Awaitable[Any]] | None = None


@dataclass(frozen=True)
class PostUpsertOutcome:
    proposal_rung_fill: bool
    notification_attempted: bool
    duplicate: bool


async def run_post_upsert_downstream(
    *,
    broker: str,
    upsert_status: UpsertStatus | str | None,
    fill_order: FillOrder | None,
    raw_event: dict[str, Any] | None,
    correlation_id: str | None = None,
    hooks: DownstreamHooks | None = None,
) -> PostUpsertOutcome:
    """Run the shared post-upsert work for exactly one committed fill.

    ``upsert_status is None`` means "no ledger row was written" (the commit
    gate is off): that is not duplicate evidence, so the notification still
    fires — preserving the monitor's pre-existing behaviour.
    """
    hooks = hooks or DownstreamHooks()
    project = hooks.project_upbit_proposal_fill or project_upbit_proposal_fill
    notify = hooks.send_fill_notification or send_fill_notification

    duplicate = upsert_status in DUPLICATE_STATUSES
    proposal_rung_fill = False
    if broker == "upbit" and upsert_status is not None and raw_event is not None:
        proposal_rung_fill = await project(raw_event)

    if broker == "upbit":
        # A duplicate ledger row normally suppresses the alert, but a rung that
        # only projected on the *second* delivery would otherwise never be
        # announced for a sub-threshold fill. Large fills already alerted on
        # the first delivery, so they stay suppressed.
        recover_suppressed_small_alert = (
            duplicate
            and proposal_rung_fill
            and fill_order is not None
            and not is_fill_notifiable(fill_order)
        )
        should_notify = not duplicate or recover_suppressed_small_alert
    else:
        should_notify = not duplicate

    if not should_notify:
        logger.info(
            "Fill notification skipped for duplicate ledger row: broker=%s "
            "correlation_id=%s status=%s",
            broker,
            correlation_id,
            upsert_status,
        )
        return PostUpsertOutcome(
            proposal_rung_fill=proposal_rung_fill,
            notification_attempted=False,
            duplicate=duplicate,
        )

    if fill_order is None:
        # A producer that posts a fill without a replayable raw payload gets a
        # durable ledger row but no notification; say so instead of pretending.
        logger.info(
            "Fill notification skipped: no notifiable order context broker=%s "
            "correlation_id=%s",
            broker,
            correlation_id,
        )
        return PostUpsertOutcome(
            proposal_rung_fill=proposal_rung_fill,
            notification_attempted=False,
            duplicate=duplicate,
        )

    if broker == "upbit":
        await notify(fill_order, proposal_rung_fill=proposal_rung_fill)
    else:
        await notify(fill_order, correlation_id=correlation_id)
    return PostUpsertOutcome(
        proposal_rung_fill=proposal_rung_fill,
        notification_attempted=True,
        duplicate=duplicate,
    )
