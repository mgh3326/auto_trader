"""ROB-405 Slice C — counterfactual sync for watch-driven mock roundtrips.

For each closed account_type='mock' journal with a correlation_id that has a
matching InvestmentWatchEvent, records trigger/actual-fill/no-action prices and
deltas. no_action_price is a live quote at sync time (injectable). Idempotent
via unique correlation_id. price_fn failure is fail-open (null no_action).
Default off.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.jobs.watch_market_data import get_price as _default_get_price
from app.models.investment_reports import InvestmentWatchEvent
from app.models.review import TradeJournalCounterfactual
from app.models.trade_journal import TradeJournal

logger = logging.getLogger(__name__)

PriceFn = Callable[[str, str], Awaitable[float | None]]


def _pct(numer: Decimal, denom: Decimal | None) -> Decimal | None:
    if denom is None or denom == 0:
        return None
    return (numer / denom * 100).quantize(Decimal("0.0001"))


async def sync_journal_counterfactuals(
    db, *, force: bool = False, price_fn: PriceFn = _default_get_price
) -> dict[str, Any]:
    """Record counterfactual rows for watch-driven closed mock roundtrips."""
    if not force and not settings.JOURNAL_COUNTERFACTUAL_ENABLED:
        return {"status": "disabled", "created": 0}

    journals = (
        (
            await db.execute(
                select(TradeJournal).where(
                    TradeJournal.status == "closed",
                    TradeJournal.account_type == "mock",
                    TradeJournal.correlation_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    created = 0
    for j in journals:
        existing = (
            await db.execute(
                select(TradeJournalCounterfactual.id).where(
                    TradeJournalCounterfactual.correlation_id == j.correlation_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        event = (
            await db.execute(
                select(InvestmentWatchEvent)
                .where(InvestmentWatchEvent.correlation_id == j.correlation_id)
                .order_by(InvestmentWatchEvent.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if event is None:
            continue  # not rule-driven → no counterfactual

        trigger_price = Decimal(str(event.threshold))
        fill = j.entry_price
        no_action: Decimal | None = None
        no_action_as_of: datetime | None = None
        try:
            raw = await price_fn(event.symbol, event.market)
            no_action_as_of = datetime.now(tz=UTC)
            no_action = Decimal(str(raw)) if raw is not None else None
        except Exception:  # noqa: BLE001 - one bad quote must not break the sync
            logger.warning(
                "counterfactual price_fn failed for %s/%s", event.symbol, event.market
            )

        fill_vs_trigger = _pct(fill - trigger_price, trigger_price) if fill else None
        no_action_vs_fill = (
            _pct(no_action - fill, fill) if (no_action is not None and fill) else None
        )

        db.add(
            TradeJournalCounterfactual(
                journal_id=j.id,
                correlation_id=j.correlation_id,
                symbol=event.symbol,
                market=event.market,
                trigger_price=trigger_price,
                triggered_value=event.current_value,
                actual_fill_price=fill,
                no_action_price=no_action,
                no_action_as_of=no_action_as_of,
                fill_vs_trigger_pct=fill_vs_trigger,
                no_action_vs_fill_pct=no_action_vs_fill,
            )
        )
        created += 1

    await db.commit()
    return {"status": "ok", "created": created}
