"""Read-only approval-inbox schemas for the /trader page (task 890, PR A).

Transport-only shapes. Nothing here maps to a write path: the page's approve,
reject and loss-cut confirmation buttons post to the existing /invest web
approval endpoints (``app/routers/invest_loss_cut_approvals.py``), which run
the shared Telegram execution core through ``handle_web_approval``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TraderApprovalRung(BaseModel):
    """One proposal rung, including its post-action broker acceptance state."""

    model_config = ConfigDict(extra="forbid")

    rung_index: int
    side: str
    quantity: str
    limit_price: str | None
    notional: str | None
    #: Rung lifecycle state. After an approval this is the broker acceptance
    #: evidence: ``acked``/``resting`` accepted, ``unverified`` unknown,
    #: ``rejected`` refused, ``needs_reconfirm`` re-card required.
    state: str
    broker_order_id: str | None
    filled_qty: str | None
    void_reason: str | None
    #: Signed ``(limit_price / current_price - 1) * 100`` from the last
    #: auto-approve evaluation's recorded ``current_price``. ``None`` when no
    #: price was recorded; the page never fetches a live quote.
    distance_pct: str | None


class TraderApprovalItem(BaseModel):
    """One proposal as the approval inbox renders it."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    symbol: str
    market: str
    account_mode: str
    broker_account_id: str | None
    side: str
    order_type: str
    action: str
    exit_intent: str | None
    #: ``True`` for ``exit_intent == "loss_cut"``: the first approve click only
    #: issues a server-side confirmation; a second, token-bound click submits.
    requires_two_step: bool
    card_kind: str | None
    lifecycle_state: str
    rungs: list[TraderApprovalRung]
    total_quantity: str | None
    total_notional: str | None
    distance_pct: str | None
    distance_price_asof: str | None
    tier: str | None
    caveats: list[str]
    valid_until: datetime | None
    expires_in_seconds: int | None
    approved_at: datetime | None
    approved_by_channel: str | None
    commit_lease_active: bool
    #: ``True`` only when every inbox inclusion rule holds at ``as_of``. The
    #: page renders approve/reject buttons only for actionable items.
    actionable: bool
    #: Closed-vocabulary reason an item is not actionable (``None`` if it is).
    block_reason: str | None


class TraderApprovalInboxResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    #: Mirrors ``INVEST_APPROVALS_ENABLED``: the /invest web approval
    #: endpoints the buttons call answer 404 while it is off.
    actions_enabled: bool
    #: ``INVEST_APPROVALS_ENABLED`` and ``INVEST_LOSS_CUT_APPROVAL_ENABLED``
    #: (the loss-cut confirmation endpoint requires both).
    loss_cut_actions_enabled: bool
    count: int
    items: list[TraderApprovalItem]


class TraderApprovalDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    actions_enabled: bool
    loss_cut_actions_enabled: bool
    item: TraderApprovalItem
