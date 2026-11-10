"""``mock_experiments`` — paper/demo strategy journal summary (read only).

Aggregates ``review.mock_strategy_orders`` per (strategy, venue). The journal is
read, never written: the recorder module is not imported and no claim, send or
reconcile happens. Counts are journal statuses, so ``basis=inference`` — a
journal row is not broker evidence of a fill.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mock_strategy import MockStrategyOrder
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    Row,
    Unknowns,
    build_response,
    check_limit,
    check_text,
    dec,
    decode_cursor,
    guarded,
    latest,
    make_ref,
    params_digest,
    read_snapshot,
)

#: Quote currency of each paper/demo venue (money fields of that venue).
VENUE_CURRENCY = {
    "alpaca_paper": "USD",
    "alpaca_paper_crypto": "USD",
    "binance_spot_demo": "USDT",
    "binance_futures_demo": "USDT",
}
UNSENT = ("planned", "rejected")
FILLED = ("filled", "partially_filled", "closed")
UNRESOLVED = ("sending", "uncertain")


def experiment_row(r: Any) -> Row:
    """One (strategy, venue) aggregate row → contract ``ExperimentRow``."""
    hints: dict[str, str] = {}
    pnl = r.pnl_sum
    if r.closed_unpriced:
        pnl = None  # a closed episode without realized P&L: the sum is unknown
        hints["episode_pnl_amount"] = "incomplete_evidence"
    elif not r.closed_count:
        pnl = None
        hints["episode_pnl_amount"] = "not_applicable"
    currency = VENUE_CURRENCY.get(r.venue)
    if currency is None:
        currency = "unknown"
    return (
        {
            "strategy_id": r.strategy_id,
            "experiment_ref": make_ref("experiment", f"{r.strategy_id}|{r.venue}"),
            "environment": r.venue,
            "planned_count": int(r.total),
            "sent_count": int(r.sent),
            "filled_count": int(r.filled),
            "uncertain_count": int(r.unresolved),
            "episode_pnl_amount": dec(pnl),
            "currency": currency,
            "unit": "currency",
            "basis": "inference",
        },
        hints,
    )


async def read_mock_experiments(
    session: AsyncSession,
    *,
    strategy_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    check_limit(limit)
    wanted = check_text(strategy_id, max_len=128) if strategy_id is not None else None
    digest = params_digest(op="mock_experiments", strategy_id=wanted)
    offset = decode_cursor(cursor, digest)
    unknowns = Unknowns()
    as_of = datetime.now(UTC)
    order = MockStrategyOrder
    async with read_snapshot(session):

        async def read() -> list[Any]:
            stmt = (
                select(
                    order.strategy_id,
                    order.venue,
                    func.count().label("total"),
                    func.count().filter(order.status.not_in(UNSENT)).label("sent"),
                    func.count().filter(order.status.in_(FILLED)).label("filled"),
                    func.count()
                    .filter(order.status.in_(UNRESOLVED))
                    .label("unresolved"),
                    func.count().filter(order.status == "closed").label("closed_count"),
                    func.count()
                    .filter(order.status == "closed", order.realized_pnl.is_(None))
                    .label("closed_unpriced"),
                    func.sum(order.realized_pnl)
                    .filter(order.status == "closed")
                    .label("pnl_sum"),
                    func.max(func.coalesce(order.closed_at, order.opened_at)).label(
                        "last_at"
                    ),
                )
                .group_by(order.strategy_id, order.venue)
                .order_by(order.strategy_id, order.venue)
            )
            if wanted is not None:
                stmt = stmt.where(order.strategy_id == wanted)
            return list((await session.execute(stmt)).all())

        found, ok = await guarded(
            session, unknowns, "source.mock_strategy_orders", read, []
        )

    rows = [experiment_row(r) for r in found]
    return build_response(
        as_of=as_of,
        data_as_of=latest(r.last_at for r in found),
        rows=rows,
        total=len(rows) if ok else None,
        limit=limit,
        cursor=cursor,
        digest=digest,
        offset=offset,
        unknowns=unknowns,
    )
