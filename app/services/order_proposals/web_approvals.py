"""Read projections for the authenticated /invest approval hub.

This module is intentionally DB-only.  In particular, opening a card must
not refresh a broker preview; the preview recorded on the proposal rungs is
the bounded display input and the execution core performs its own fresh
preview immediately before submission.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.errors import OrderProposalNotFound


def _decimal_text(value: object | None) -> str | None:
    if value is None:
        return None
    value_as_decimal = Decimal(str(value))
    return format(value_as_decimal, "f")


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _status(group: Any, *, now: datetime) -> str:
    if group.commit_lease_until is not None and group.commit_lease_until > now:
        return "processing"
    if (
        group.approval_nonce
        and group.approval_nonce_used_at is None
        and group.valid_until is not None
        and group.valid_until > now
    ):
        return "pending"
    return "terminal"


class WebApprovalService:
    """Build a compact proposal-card projection from one joined DB result."""

    def __init__(self, proposals: OrderProposalsService, *, now: datetime) -> None:
        self._proposals = proposals
        self._now = now

    async def list_cards(self) -> list[dict[str, Any]]:
        rows = await self._proposals.list_web_approval_card_rows(limit=100)
        return self._project(rows)

    async def get_card(self, proposal_id: uuid.UUID) -> dict[str, Any]:
        rows = await self._proposals.list_web_approval_card_rows(
            proposal_id=proposal_id, limit=1
        )
        cards = self._project(rows)
        if not cards:
            raise OrderProposalNotFound(str(proposal_id))
        return cards[0]

    def _project(self, rows: list[tuple[Any, Any | None]]) -> list[dict[str, Any]]:
        cards: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for group, rung in rows:
            key = str(group.proposal_id)
            card = cards.get(key)
            if card is None:
                card = {
                    "proposal_id": key,
                    "market": group.market,
                    "account_mode": group.account_mode,
                    "symbol": group.symbol,
                    "side": group.side,
                    "action": group.action or "place",
                    "rung_summary": [],
                    "preview": [],
                    "valid_until": _timestamp(group.valid_until),
                    "status": _status(group, now=self._now),
                    "approval_hash_present": False,
                    "approval_channel": group.approved_by_channel,
                    "approved_at": _timestamp(group.approved_at),
                    "recent_result": [],
                }
                cards[key] = card
            if rung is None:
                continue
            quantity = _decimal_text(rung.quantity)
            limit_price = _decimal_text(rung.limit_price)
            notional = _decimal_text(rung.notional)
            if notional is None and quantity is not None and limit_price is not None:
                notional = _decimal_text(Decimal(quantity) * Decimal(limit_price))
            rung_view = {
                "rung_index": rung.rung_index,
                "quantity": quantity,
                "limit_price": limit_price,
                "expected_amount": notional,
                "state": rung.state,
            }
            card["rung_summary"].append(rung_view)
            card["preview"].append(
                {
                    "quantity": quantity,
                    "limit_price": limit_price,
                    "expected_amount": notional,
                }
            )
            card["recent_result"].append(
                {
                    "rung_index": rung.rung_index,
                    "state": rung.state,
                    "void_reason": rung.void_reason,
                }
            )
            card["approval_hash_present"] = bool(
                card["approval_hash_present"] or rung.approval_hash_digest
            )
        return list(cards.values())
