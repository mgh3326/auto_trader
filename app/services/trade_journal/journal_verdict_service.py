"""ROB-405 Slice B — record verdicts for closed mock trade_journals.

Auto verdicts (pnl_pct policy) for closed account_type='mock' journals lacking
one; manual verdicts are operator overrides. Idempotent via partial-unique
(journal_id) WHERE verdict_source='auto'. Default off.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.models.review import TradeJournalReview
from app.models.trade_journal import TradeJournal
from app.services.trade_journal.journal_verdict_policy import (
    classify_journal_verdict,
)

logger = logging.getLogger(__name__)

_VALID_VERDICTS = frozenset({"good", "neutral", "bad"})


async def sync_journal_verdicts(db, *, force: bool = False) -> dict[str, Any]:
    """Record auto verdicts for closed mock journals without one."""
    if not force and not settings.JOURNAL_VERDICT_AUTO_ENABLED:
        return {"status": "disabled", "created": 0}

    journals = (
        await db.execute(
            select(TradeJournal).where(
                TradeJournal.status == "closed",
                TradeJournal.account_type == "mock",
            )
        )
    ).scalars().all()

    created = 0
    for j in journals:
        existing = (
            await db.execute(
                select(TradeJournalReview.id).where(
                    TradeJournalReview.journal_id == j.id,
                    TradeJournalReview.verdict_source == "auto",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(
            TradeJournalReview(
                journal_id=j.id,
                verdict=classify_journal_verdict(j.pnl_pct),
                verdict_source="auto",
                pnl_pct=j.pnl_pct,
            )
        )
        created += 1
    await db.commit()
    return {"status": "ok", "created": created}


async def record_manual_verdict(
    db, *, journal_id: int, verdict: str, comment: str | None = None
) -> dict[str, Any]:
    """Record an operator (manual) verdict override."""
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    db.add(
        TradeJournalReview(
            journal_id=journal_id,
            verdict=verdict,
            verdict_source="manual",
            comment=comment,
        )
    )
    await db.commit()
    return {"status": "ok"}
