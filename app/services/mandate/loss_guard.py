"""I14 — a loss sale of a planned instrument needs the plan's ``thesis_broken`` (§5.2).

Read-only reader for the three places the guard runs (T4c): ``loss_cut`` proposal
creation (under the symbol lock L1 of the live realm), the human approval callback
before the nonce is consumed, and just before the broker call. It only ever adds a
refusal: an instrument with no unreleased live-realm plan is not affected at all,
and the existing ``loss_cut`` checks (retrospective age, approval issue, evidence)
are never skipped by it.

The guard looks at the symbol reservation of the live realm, which is keyed by market
and symbol and **not** by account: a Toss plan whose adopted amount counted a KIS
holding also guards the KIS ``loss_cut`` of that symbol.

A plan passes when its history has a ``thesis_broken`` event and the proposal's
retrospective id and approval issue id are both found in that event's evidence or in
the evidence of a ``thesis_evidence_refreshed`` event that points at it. The state of
the plan (``thesis_broken``, or retired afterwards, or exhausted) does not matter:
while it is unreleased, the condition applies. This module imports no order or
broker module.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mandate import (
    MandatePlanEvent,
    MandateSymbolReservation,
    MandateSymbolReservationRelease,
)
from app.services.mandate import store
from app.services.mandate import vocabulary as v
from app.services.mandate.locks import acquire_symbol_lock
from app.services.strategy_sleeves import store as sleeve_store

LIVE_REALM = "live"


def _same(left: Any, right: Any) -> bool:
    return left is not None and right is not None and str(left) == str(right)


def evidence_matches(
    events: list[MandatePlanEvent],
    *,
    retrospective_id: Any,
    approval_issue_id: Any,
) -> bool:
    """Both ids are in one ``thesis_broken`` event's evidence or in a refresh of it."""
    for broken in (e for e in events if e.event == "thesis_broken"):
        pool = [broken.evidence or {}] + [
            e.evidence or {}
            for e in events
            if e.event == "thesis_evidence_refreshed" and e.thesis_seq == broken.seq
        ]
        for evidence in pool:
            if _same(evidence.get("retrospective_id"), retrospective_id) and _same(
                evidence.get("approval_issue_id"), approval_issue_id
            ):
                return True
    return False


async def unreleased_live_plan_ids(
    session: AsyncSession, *, market: str, symbol: str
) -> list[int]:
    rows = await session.execute(
        select(MandateSymbolReservation.plan_id).where(
            MandateSymbolReservation.realm == LIVE_REALM,
            MandateSymbolReservation.market == market,
            MandateSymbolReservation.symbol == symbol,
            ~exists().where(
                MandateSymbolReservationRelease.reservation_id
                == MandateSymbolReservation.id
            ),
        )
    )
    return sorted(int(r[0]) for r in rows.all())


async def loss_cut_refusal(
    session: AsyncSession,
    *,
    market: str,
    symbol: str,
    retrospective_id: Any,
    approval_issue_id: Any,
    lock_symbol: bool = False,
) -> str | None:
    """``None`` when the loss sale may go on, otherwise a closed refusal code.

    ``lock_symbol`` takes L1 of the live realm first (proposal creation: the plan
    registration takes the same lock, so each sees the other's committed row).
    Callers treat an exception as ``mandate_unreadable`` and refuse.
    """
    norm = sleeve_store.sleeve_symbol(symbol, market)
    if lock_symbol:
        await acquire_symbol_lock(session, LIVE_REALM, market, norm)
    plan_ids = await unreleased_live_plan_ids(session, market=market, symbol=norm)
    for plan_id in plan_ids:
        events = await store.list_plan_events(session, plan_id)
        if not evidence_matches(
            events,
            retrospective_id=retrospective_id,
            approval_issue_id=approval_issue_id,
        ):
            return v.MandateRefusal.THESIS_NOT_BROKEN.value
    return None
