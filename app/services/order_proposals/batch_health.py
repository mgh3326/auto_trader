"""Read-only health for the manual approval batch surface (ROB-870).

Two different Telegram batching surfaces share the word "digest", and only one
of them writes approval-batch rows:

* ROB-1052 auto-approve digest is message-only. It buffers ``auto_veto``
  notices inside one dispatch round and sends them as one message; it never
  creates ``order_proposal_approval_batches``/``..._batch_members`` rows.
* ROB-870 manual batch writes one member row per *published, batch-eligible*
  manual/reconfirm card, and publishes the ``전체 승인`` summary when a second
  eligible member joins within the fixed ten-minute window.

``order_proposal_approval_batch_members`` also carries two timestamps that mean
different things:

* ``added_at`` -- the membership writer. This is the cadence signal.
* ``processed_at`` -- the callback *result* recorder, written only when a batch
  click processes that member. A cadence audit that reads this column instead
  of ``added_at`` reports a healthy writer as dead.

That mistake is exactly what the 2026-11-02 ROB-1488 inventory made
("0 rows since 2026-09-08"). Production on the same day had 33 member rows in
30 days with ``max(added_at)`` of that morning.

This module is SELECT-only and aggregate-only: no row payloads, no writes, no
broker or Telegram call. The evaluator is pure so the thresholds can be tested
without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select

from app.models.order_proposals import (
    OrderProposalApprovalBatch,
    OrderProposalApprovalBatchMember,
    OrderProposalApprovalDispatchAttempt,
)

Status = Literal["ok", "gap"]

DEFAULT_RECENT_DAYS = 7
DEFAULT_WINDOW_DAYS = 30

_MANUAL_CARD_KINDS = ("manual", "reconfirm")

# Reasons that are *designed* to keep a proposal out of a batch. Every other
# reason means a published card's group changed state before registration.
INTENTIONAL_BATCH_MEMBER_EXCLUSIONS = frozenset(
    {
        "loss_cut_excluded",
        "cash_funding_excluded",
        "auto_approved_excluded",
    }
)


def batch_member_skip_intentional(reason: str) -> bool:
    """True when a skip reason is a designed exclusion, not a state change."""
    return reason in INTENTIONAL_BATCH_MEMBER_EXCLUSIONS


@dataclass(frozen=True, slots=True)
class BatchHealthWindow:
    """Aggregate counts for one lookback window."""

    days: int
    manual_cards: int = 0
    member_added: int = 0
    member_processed: int = 0
    batches: int = 0
    summaries_sent: int = 0
    frozen_unsent: int = 0
    published_cards_without_member: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "manual_cards": self.manual_cards,
            "member_added": self.member_added,
            "member_processed": self.member_processed,
            "batches": self.batches,
            "summaries_sent": self.summaries_sent,
            "frozen_unsent": self.frozen_unsent,
            "published_cards_without_member": self.published_cards_without_member,
        }


@dataclass(frozen=True, slots=True)
class BatchHealthReport:
    """The facts plus the closed-set verdict derived from them."""

    as_of: datetime
    recent: BatchHealthWindow
    window: BatchHealthWindow
    members_total: int
    zero_member_batches: int
    status: Status
    anomalies: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "status": self.status,
            "anomalies": list(self.anomalies),
            "members_total": self.members_total,
            "zero_member_batches": self.zero_member_batches,
            "recent": self.recent.as_dict(),
            "window": self.window.as_dict(),
            "notes": {
                "member_writer_column": "added_at",
                "result_recorder_column": "processed_at",
                "auto_digest_writes_batch_rows": False,
                "summary_rule": (
                    "전체 승인 summary requires two batch-eligible manual cards "
                    "inside one ten-minute window; a single-member idle batch is "
                    "expected, not a lost card"
                ),
            },
        }


def evaluate_batch_health(
    *,
    recent: BatchHealthWindow,
    zero_member_batches: int,
) -> tuple[Status, tuple[str, ...]]:
    """Return the hard-invariant verdict for the batch member surface.

    Only invariants that cannot hold on a healthy writer are anomalies:

    * a batch row without a single member (registration must accompany the
      batch insert in the same transaction);
    * a published manual card in the recent window with zero member rows
      (the writer stopped while the dispatch surface kept publishing).
    """
    anomalies: list[str] = []
    if zero_member_batches > 0:
        anomalies.append("approval_batch_without_members")
    if recent.manual_cards > 0 and recent.member_added == 0:
        anomalies.append("approval_batch_member_writer_stalled")
    status: Status = "gap" if anomalies else "ok"
    return status, tuple(anomalies)


async def load_batch_health(
    db: Any,
    *,
    now: datetime | None = None,
    recent_days: int = DEFAULT_RECENT_DAYS,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> BatchHealthReport:
    """Read the aggregate batch/member counts. SELECT only."""

    as_of = now or datetime.now(UTC)

    async def _window(days: int) -> BatchHealthWindow:
        cutoff = as_of - timedelta(days=days)
        member_row = (
            await db.execute(
                select(
                    func.count()
                    .filter(OrderProposalApprovalBatchMember.added_at >= cutoff)
                    .label("added"),
                    func.count()
                    .filter(OrderProposalApprovalBatchMember.processed_at >= cutoff)
                    .label("processed"),
                ).select_from(OrderProposalApprovalBatchMember)
            )
        ).one()
        batch_row = (
            await db.execute(
                select(
                    func.count().label("batches"),
                    func.count()
                    .filter(OrderProposalApprovalBatch.summary_dispatch_state == "sent")
                    .label("sent"),
                    func.count()
                    .filter(
                        OrderProposalApprovalBatch.membership_frozen_at.is_not(None),
                        OrderProposalApprovalBatch.summary_dispatch_state != "sent",
                        OrderProposalApprovalBatch.summary_message_id.is_(None),
                    )
                    .label("frozen_unsent"),
                ).where(OrderProposalApprovalBatch.created_at >= cutoff)
            )
        ).one()
        manual_cards = await db.execute(
            select(func.count()).where(
                OrderProposalApprovalDispatchAttempt.card_kind.in_(_MANUAL_CARD_KINDS),
                OrderProposalApprovalDispatchAttempt.state == "sent_current",
                OrderProposalApprovalDispatchAttempt.message_id.is_not(None),
                OrderProposalApprovalDispatchAttempt.attempted_at >= cutoff,
            )
        )
        without_member = await db.execute(
            select(func.count()).where(
                OrderProposalApprovalDispatchAttempt.card_kind.in_(_MANUAL_CARD_KINDS),
                OrderProposalApprovalDispatchAttempt.state == "sent_current",
                OrderProposalApprovalDispatchAttempt.message_id.is_not(None),
                OrderProposalApprovalDispatchAttempt.attempted_at >= cutoff,
                ~(
                    select(OrderProposalApprovalBatchMember.id)
                    .where(
                        OrderProposalApprovalBatchMember.approval_dispatch_attempt_id_snapshot
                        == OrderProposalApprovalDispatchAttempt.attempt_id
                    )
                    .exists()
                ),
            )
        )
        return BatchHealthWindow(
            days=days,
            manual_cards=int(manual_cards.scalar_one()),
            member_added=int(member_row.added),
            member_processed=int(member_row.processed),
            batches=int(batch_row.batches),
            summaries_sent=int(batch_row.sent),
            frozen_unsent=int(batch_row.frozen_unsent),
            published_cards_without_member=int(without_member.scalar_one()),
        )

    recent = await _window(recent_days)
    window = await _window(window_days)

    members_total = await db.execute(
        select(func.count()).select_from(OrderProposalApprovalBatchMember)
    )
    member_counts = (
        select(
            OrderProposalApprovalBatchMember.batch_pk.label("batch_pk"),
            func.count().label("n"),
        )
        .group_by(OrderProposalApprovalBatchMember.batch_pk)
        .subquery()
    )
    zero_member = await db.execute(
        select(func.count())
        .select_from(OrderProposalApprovalBatch)
        .outerjoin(
            member_counts, member_counts.c.batch_pk == OrderProposalApprovalBatch.id
        )
        .where(func.coalesce(member_counts.c.n, 0) == 0)
    )
    zero_member_batches = int(zero_member.scalar_one())

    status, anomalies = evaluate_batch_health(
        recent=recent,
        zero_member_batches=zero_member_batches,
    )
    return BatchHealthReport(
        as_of=as_of,
        recent=recent,
        window=window,
        members_total=int(members_total.scalar_one()),
        zero_member_batches=zero_member_batches,
        status=status,
        anomalies=anomalies,
    )


__all__ = [
    "BatchHealthReport",
    "BatchHealthWindow",
    "DEFAULT_RECENT_DAYS",
    "DEFAULT_WINDOW_DAYS",
    "INTENTIONAL_BATCH_MEMBER_EXCLUSIONS",
    "batch_member_skip_intentional",
    "evaluate_batch_health",
    "load_batch_health",
]
