"""Read-only fill intent lookup for fill alerts (hk 2834).

Given the broker order id of a committed fill, find the proposal rung that
placed the order and describe it for the alert: strategy, a thesis summary, the
rung's ladder position, and a reference price for slippage.

Read-only: three SELECTs, never a commit, never a write. It never raises into
the alert path. Any failure returns ``LOOKUP_FAILED`` so the alert is still sent
and says the intent is unknown rather than absent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.fill_intent import (
    LOOKUP_FAILED,
    NO_MATCH,
    FillIntent,
    summarize_thesis,
)
from app.services.order_proposals.repository import OrderProposalRepository

logger = logging.getLogger(__name__)


def _positive_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def approval_time_price(source_asof: Any, rung_index: int) -> Decimal | None:
    """The price the auto-approval decision evaluated for this rung, if stored.

    Only ``source_asof["auto_approved"]["eligibility"]`` keeps a price per rung
    (the ``current_price`` of an eligible decision). Manual approvals store no
    price, so they return ``None`` and the slippage line is omitted.
    """
    if not isinstance(source_asof, Mapping):
        return None
    auto_approved = source_asof.get("auto_approved")
    if not isinstance(auto_approved, Mapping):
        return None
    eligibility = auto_approved.get("eligibility")
    if not isinstance(eligibility, Sequence) or isinstance(eligibility, (str, bytes)):
        return None
    for decision in eligibility:
        if not isinstance(decision, Mapping):
            continue
        if type(decision.get("rung_index")) is not int:
            continue
        if decision["rung_index"] != rung_index or decision.get("eligible") is not True:
            continue
        price = _positive_decimal(decision.get("current_price"))
        if price is not None:
            return price
    return None


def build_fill_intent(proposal: Any, rung: Any, rungs: Sequence[Any]) -> FillIntent:
    """Pure assembly of the matched intent from already-read rows."""
    ordered = sorted(rungs, key=lambda item: item.rung_index)
    position = next(
        (
            index
            for index, item in enumerate(ordered, start=1)
            if item.rung_index == rung.rung_index
        ),
        None,
    )

    reference: Decimal | None = None
    reference_kind: str | None = None
    if str(proposal.order_type or "").lower() == "limit":
        reference = _positive_decimal(rung.limit_price)
        if reference is not None:
            reference_kind = "limit"
    else:
        reference = approval_time_price(proposal.source_asof, rung.rung_index)
        if reference is not None:
            reference_kind = "approval_price"

    return FillIntent(
        status="matched",
        strategy=(proposal.strategy or None),
        thesis_summary=summarize_thesis(proposal.thesis),
        rung_position=position,
        rung_total=len(ordered) if ordered else None,
        reference_price=reference,
        reference_kind=reference_kind,  # type: ignore[arg-type]
    )


async def resolve_fill_intent(
    *,
    broker_order_id: str | None,
    account_mode: str | None = None,
    session_factory: Callable[[], Any] | None = None,
    repository_cls: type[OrderProposalRepository] | None = None,
) -> FillIntent:
    """Describe the proposal rung behind a fill, or say why it could not.

    Never raises. Without a broker order id the absence of a rung cannot be
    established, so the answer is ``LOOKUP_FAILED``, not ``NO_MATCH``.
    """
    order_id = (broker_order_id or "").strip() or None
    if order_id is None:
        return LOOKUP_FAILED
    factory = session_factory or AsyncSessionLocal
    repo_cls = repository_cls or OrderProposalRepository
    try:
        async with factory() as db:
            repo = repo_cls(db)
            found = await repo.find_rung_by_evidence(
                correlation_id=None,
                broker_order_id=order_id,
                account_mode=account_mode,
            )
            if found is None:
                return NO_MATCH
            proposal_id, rung = found
            proposal = await repo.get_group_by_proposal_id(proposal_id)
            if proposal is None:
                return LOOKUP_FAILED
            rungs = await repo.list_rungs(proposal.id)
        return build_fill_intent(proposal, rung, rungs)
    except Exception:  # noqa: BLE001 - the alert must still go out
        logger.warning(
            "fill intent lookup failed: broker_order_id=%s account_mode=%s",
            order_id,
            account_mode,
            exc_info=True,
        )
        return LOOKUP_FAILED
