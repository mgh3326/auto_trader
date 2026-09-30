"""Read-only approval inbox for the /trader page (task 890, PR A).

The inbox lists proposals that still need a human decision on a live Telegram
approval card. It is DB-only: no broker preview, no live quote, no Telegram
call. It never approves anything -- the page's buttons post to the existing
/invest web approval endpoints, which run ``handle_web_approval`` and through
it the same ``_handle_approve``/``_handle_deny``/``_handle_loss_cut_first_click``
handlers the Telegram callback runs (pinned by
tests/services/order_proposals/test_trader_page_same_approval_path.py).

Inclusion rule (``inbox_block_reason`` is the single source; the SQL filter is
only a bounded prefilter and every candidate is re-checked in Python):

* not superseded and not in a terminal lifecycle state;
* not auto-approved (``source_asof.auto_approved`` is a dict) -- auto-approved
  proposals only ever carry an auto-veto *notice* card;
* the current card is published (``approval_dispatch_state == sent_current``)
  and is a human approve/deny card (``manual`` or ``reconfirm``). Auto-veto
  notices, batch triggers, and in-flight loss-cut confirmation cards are out;
* the published approval nonce is present and unconsumed;
* ``valid_until`` is set and still in the future at ``now``;
* at least one rung still awaits a decision (``pending_approval`` or
  ``needs_reconfirm``).

This module must not import approval, order, or broker mutation services --
pinned by tests/test_trader_page_safety.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.schemas.trader_approvals import (
    TraderApprovalDetailResponse,
    TraderApprovalInboxResponse,
    TraderApprovalItem,
    TraderApprovalRung,
)
from app.services.order_proposals.auto_approve_audit import (
    project_auto_approve_not_evaluated,
    project_auto_approve_rejections,
)

INBOX_LIMIT = 100

#: Human approve/deny cards. ``auto_veto`` is the auto-approval notice card,
#: ``batch`` is a multi-proposal trigger the web core cannot execute, and
#: ``loss_cut_confirmation`` is an in-flight second step bound to one channel.
INBOX_CARD_KINDS: frozenset[str] = frozenset({"manual", "reconfirm"})
INBOX_RUNG_STATES: frozenset[str] = frozenset({"pending_approval", "needs_reconfirm"})
#: Mirrors ``_APPROVAL_TERMINAL_GROUP_STATES`` in order_proposals/service.py
#: (not imported: that module is the mutation service).
TERMINAL_GROUP_STATES: frozenset[str] = frozenset(
    {"terminal", "rejected", "expired", "voided", "superseded"}
)
PUBLISHED_DISPATCH_STATE = "sent_current"

_MAX_CAVEATS = 10
_MAX_CAVEAT_CHARS = 120
_MAX_TIER_CHARS = 64


def _is_auto_approved(group: Any) -> bool:
    source_asof = getattr(group, "source_asof", None)
    return isinstance(source_asof, Mapping) and isinstance(
        source_asof.get("auto_approved"), Mapping
    )


def inbox_block_reason(
    group: Any, rungs: Sequence[Any], *, now: datetime
) -> str | None:
    """Return why ``group`` must not show approve/reject buttons, else ``None``.

    Closed vocabulary, in precedence order: ``terminal``, ``auto_approved``,
    ``not_published``, ``not_human_card``, ``nonce_used``, ``expired``,
    ``no_pending_rungs``.
    """
    if now.tzinfo is None:
        raise ValueError("inbox_block_reason requires a timezone-aware now")
    if (
        getattr(group, "superseded_by_proposal_id", None) is not None
        or getattr(group, "lifecycle_state", None) in TERMINAL_GROUP_STATES
    ):
        return "terminal"
    if _is_auto_approved(group):
        return "auto_approved"
    if getattr(group, "approval_dispatch_state", None) != PUBLISHED_DISPATCH_STATE:
        return "not_published"
    if getattr(group, "approval_dispatch_card_kind", None) not in INBOX_CARD_KINDS:
        return "not_human_card"
    if (
        not getattr(group, "approval_nonce", None)
        or getattr(group, "approval_nonce_used_at", None) is not None
    ):
        return "nonce_used"
    valid_until = getattr(group, "valid_until", None)
    if valid_until is None or valid_until <= now:
        return "expired"
    if not any(getattr(rung, "state", None) in INBOX_RUNG_STATES for rung in rungs):
        return "no_pending_rungs"
    return None


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _text(value: object) -> str | None:
    parsed = _decimal(value)
    return None if parsed is None else format(parsed, "f")


def _latest_rejection(source_asof: Any) -> dict[str, Any] | None:
    attempts = project_auto_approve_rejections(source_asof)
    return attempts[-1] if attempts else None


def _distance_pct(limit_price: object, current_price: object) -> str | None:
    limit = _decimal(limit_price)
    current = _decimal(current_price)
    if limit is None or current is None or current <= 0:
        return None
    value = (limit / current - Decimal("1")) * Decimal("100")
    return format(value.quantize(Decimal("0.01")), "f")


def _tier(group: Any) -> str | None:
    rationale = getattr(group, "rationale", None)
    candidates = [
        rationale.get("tier") if isinstance(rationale, Mapping) else None,
        getattr(group, "strategy", None),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:_MAX_TIER_CHARS]
    return None


def _caveats(group: Any, latest_rejection: Mapping[str, Any] | None) -> list[str]:
    """Bounded, de-duplicated caveat codes and operator notes."""
    caveats: list[str] = []

    def add(value: object) -> None:
        if not isinstance(value, str):
            return
        normalized = " ".join(value.split())[:_MAX_CAVEAT_CHARS]
        if normalized and normalized not in caveats:
            caveats.append(normalized)

    if getattr(group, "exit_intent", None) == "loss_cut":
        add("loss_cut_two_step")
    action = getattr(group, "action", None) or "place"
    if action != "place":
        add(f"action:{action}")
    if getattr(group, "approval_dispatch_card_kind", None) == "reconfirm":
        add("reconfirm")
    if latest_rejection is not None:
        for rung in latest_rejection.get("rungs", []):
            add(f"auto:{rung.get('reason_code')}")
    not_evaluated = project_auto_approve_not_evaluated(
        getattr(group, "source_asof", None)
    )
    if not_evaluated is not None:
        add(f"auto_not_evaluated:{not_evaluated}")
    rationale = getattr(group, "rationale", None)
    raw_notes = rationale.get("caveats") if isinstance(rationale, Mapping) else None
    if isinstance(raw_notes, str):
        add(raw_notes)
    elif isinstance(raw_notes, Sequence):
        for note in raw_notes:
            add(note)
    return caveats[:_MAX_CAVEATS]


def project_item(
    group: Any, rungs: Sequence[Any], *, now: datetime
) -> TraderApprovalItem:
    """Project one proposal and its rungs; pure and DB-free."""
    block_reason = inbox_block_reason(group, rungs, now=now)
    latest = _latest_rejection(getattr(group, "source_asof", None))
    prices_by_rung: dict[int, str] = {}
    if latest is not None:
        for rung in latest.get("rungs", []):
            price = rung.get("inputs", {}).get("current_price")
            if isinstance(price, str):
                prices_by_rung[int(rung["rung_index"])] = price

    ordered = sorted(rungs, key=lambda rung: rung.rung_index)
    rung_views: list[TraderApprovalRung] = []
    total_quantity = Decimal("0")
    total_notional = Decimal("0")
    quantity_known = bool(ordered)
    notional_known = bool(ordered)
    for rung in ordered:
        quantity = _decimal(rung.quantity)
        limit_price = _decimal(rung.limit_price)
        notional = _decimal(rung.notional)
        if notional is None and quantity is not None and limit_price is not None:
            notional = quantity * limit_price
        if quantity is None:
            quantity_known = False
        else:
            total_quantity += quantity
        if notional is None:
            notional_known = False
        else:
            total_notional += notional
        rung_views.append(
            TraderApprovalRung(
                rung_index=rung.rung_index,
                side=rung.side,
                quantity=_text(rung.quantity) or "0",
                limit_price=_text(rung.limit_price),
                notional=_text(notional),
                state=rung.state,
                broker_order_id=rung.broker_order_id,
                filled_qty=_text(rung.filled_qty),
                void_reason=rung.void_reason,
                distance_pct=_distance_pct(
                    rung.limit_price, prices_by_rung.get(rung.rung_index)
                ),
            )
        )

    first_distance = next(
        (view.distance_pct for view in rung_views if view.distance_pct is not None),
        None,
    )
    valid_until = group.valid_until
    expires_in = (
        max(0, int((valid_until - now).total_seconds()))
        if valid_until is not None
        else None
    )
    lease_until = getattr(group, "commit_lease_until", None)
    return TraderApprovalItem(
        proposal_id=str(group.proposal_id),
        symbol=group.symbol,
        market=group.market,
        account_mode=group.account_mode,
        broker_account_id=group.broker_account_id,
        side=group.side,
        order_type=group.order_type,
        action=group.action or "place",
        exit_intent=group.exit_intent,
        requires_two_step=group.exit_intent == "loss_cut",
        card_kind=group.approval_dispatch_card_kind,
        lifecycle_state=group.lifecycle_state,
        rungs=rung_views,
        total_quantity=format(total_quantity, "f") if quantity_known else None,
        total_notional=format(total_notional, "f") if notional_known else None,
        distance_pct=first_distance,
        distance_price_asof=(
            latest.get("evaluated_at")
            if latest is not None and first_distance is not None
            else None
        ),
        tier=_tier(group),
        caveats=_caveats(group, latest),
        valid_until=valid_until,
        expires_in_seconds=expires_in,
        approved_at=group.approved_at,
        approved_by_channel=group.approved_by_channel,
        commit_lease_active=lease_until is not None and lease_until > now,
        actionable=block_reason is None,
        block_reason=block_reason,
    )


def _actions_enabled() -> tuple[bool, bool]:
    approvals = bool(settings.INVEST_APPROVALS_ENABLED)
    return approvals, approvals and bool(settings.INVEST_LOSS_CUT_APPROVAL_ENABLED)


class TraderApprovalInboxService:
    """Bounded DB reads for the inbox list and the per-row detail."""

    def __init__(self, db: AsyncSession, *, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("TraderApprovalInboxService requires an aware now")
        self._db = db
        self._now = now

    async def _load(
        self, *, proposal_id: uuid.UUID | None
    ) -> list[tuple[OrderProposal, list[OrderProposalRung]]]:
        """Two bounded SELECTs: the groups, then all of their rungs."""
        groups_stmt = select(OrderProposal)
        if proposal_id is not None:
            groups_stmt = groups_stmt.where(OrderProposal.proposal_id == proposal_id)
        else:
            # Bounded prefilter only; ``inbox_block_reason`` decides.
            groups_stmt = (
                groups_stmt.where(
                    OrderProposal.superseded_by_proposal_id.is_(None),
                    OrderProposal.lifecycle_state.not_in(TERMINAL_GROUP_STATES),
                    OrderProposal.approval_dispatch_state == PUBLISHED_DISPATCH_STATE,
                    OrderProposal.approval_dispatch_card_kind.in_(INBOX_CARD_KINDS),
                    OrderProposal.approval_nonce.is_not(None),
                    OrderProposal.approval_nonce_used_at.is_(None),
                    OrderProposal.valid_until.is_not(None),
                    OrderProposal.valid_until > self._now,
                    # NULL-safe: a NULL source_asof is not auto-approved. Any
                    # ``auto_approved`` key excludes (stricter than Python).
                    or_(
                        OrderProposal.source_asof.is_(None),
                        OrderProposal.source_asof.has_key("auto_approved").is_(False),
                    ),
                )
                .order_by(OrderProposal.valid_until.asc(), OrderProposal.id.asc())
                .limit(INBOX_LIMIT)
            )
        groups_stmt = groups_stmt.execution_options(populate_existing=True)
        groups = list((await self._db.scalars(groups_stmt)).all())
        if not groups:
            return []
        rungs_stmt = (
            select(OrderProposalRung)
            .where(OrderProposalRung.proposal_pk.in_([group.id for group in groups]))
            .order_by(OrderProposalRung.proposal_pk, OrderProposalRung.rung_index)
            .execution_options(populate_existing=True)
        )
        rungs_by_group: dict[int, list[OrderProposalRung]] = {
            group.id: [] for group in groups
        }
        for rung in (await self._db.scalars(rungs_stmt)).all():
            rungs_by_group[rung.proposal_pk].append(rung)
        return [(group, rungs_by_group[group.id]) for group in groups]

    async def list_inbox(self) -> TraderApprovalInboxResponse:
        items = [
            item
            for group, rungs in await self._load(proposal_id=None)
            if (item := project_item(group, rungs, now=self._now)).actionable
        ]
        actions, loss_cut_actions = _actions_enabled()
        return TraderApprovalInboxResponse(
            as_of=self._now,
            actions_enabled=actions,
            loss_cut_actions_enabled=loss_cut_actions,
            count=len(items),
            items=items,
        )

    async def get_item(
        self, proposal_id: uuid.UUID
    ) -> TraderApprovalDetailResponse | None:
        grouped = await self._load(proposal_id=proposal_id)
        if not grouped:
            return None
        group, rungs = grouped[0]
        actions, loss_cut_actions = _actions_enabled()
        return TraderApprovalDetailResponse(
            as_of=self._now,
            actions_enabled=actions,
            loss_cut_actions_enabled=loss_cut_actions,
            item=project_item(group, rungs, now=self._now),
        )
