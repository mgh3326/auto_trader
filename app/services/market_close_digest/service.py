"""Assemble and optionally send the ROB-1297 market-close digest.

Read-only aggregation. Send goes through ``TradeNotifier.notify_agent_message``
only. Holiday skip reuses ``session_calendar.is_trading_session``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from app.services.market_close_digest.calendar import (
    infer_session_date,
    session_window,
    should_skip_holiday,
)
from app.services.market_close_digest.flags import improvement_flags
from app.services.market_close_digest.formatter import format_digest_message
from app.services.market_close_digest.mutation import attach_mutation_counter
from app.services.market_close_digest.oversell import collect_oversell_blocks
from app.services.market_close_digest.queries import (
    DigestSources,
    SqlAlchemyDigestSources,
    merge_retro_pnl,
)
from app.services.market_close_digest.types import (
    DigestRunResult,
    DigestSnapshot,
    LedgerFill,
    Market,
    ProposalRow,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.monitoring.trade_notifier.notifier import TradeNotifier


def _card_count(proposals: tuple[ProposalRow, ...]) -> int:
    seen: set[tuple[str, datetime]] = set()
    count = 0
    for row in proposals:
        if not row.card_kind:
            continue
        key = (row.symbol, row.created_at)
        if key in seen:
            continue
        seen.add(key)
        count += 1
    return count


def _auto_approve_count(proposals: tuple[ProposalRow, ...]) -> int:
    seen: set[tuple[str, datetime]] = set()
    count = 0
    for row in proposals:
        if not row.auto_approved:
            continue
        key = (row.symbol, row.created_at)
        if key in seen:
            continue
        seen.add(key)
        count += 1
    return count


def assemble_snapshot(
    *,
    market: Market,
    session_date: date,
    fills: Sequence[LedgerFill],
    proposals: tuple[ProposalRow, ...],
    window_start: datetime | None,
    window_end: datetime | None,
) -> DigestSnapshot:
    oversell = collect_oversell_blocks(proposals)
    status = "zero_fills" if len(fills) == 0 else "ok"
    snapshot = DigestSnapshot(
        market=market,
        session_date=session_date,
        status=status,
        fills=tuple(fills),
        oversell_blocked=oversell,
        auto_approve_count=_auto_approve_count(proposals),
        card_count=_card_count(proposals),
        window_start=window_start,
        window_end=window_end,
    )
    return DigestSnapshot(
        market=snapshot.market,
        session_date=snapshot.session_date,
        status=snapshot.status,
        fills=snapshot.fills,
        oversell_blocked=snapshot.oversell_blocked,
        auto_approve_count=snapshot.auto_approve_count,
        card_count=snapshot.card_count,
        flags=improvement_flags(snapshot),
        window_start=snapshot.window_start,
        window_end=snapshot.window_end,
    )


async def send_digest_message(
    message: str,
    *,
    market: Market,
    session_date: date,
    notifier: TradeNotifier,
) -> bool:
    """Existing TradeNotifier surface only — no new bot/channel/transport."""
    return await notifier.notify_agent_message(
        message,
        parse_mode=None,
        market_type=market,
        mirror_telegram=True,
        correlation_id=f"market-close-digest:{market}:{session_date.isoformat()}",
    )


async def run_market_close_digest(
    *,
    market: Market,
    session_date: date | None = None,
    now: datetime | None = None,
    sources: DigestSources | None = None,
    session: AsyncSession | None = None,
    send: bool = False,
    notifier: TradeNotifier | None = None,
) -> DigestRunResult:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    day = session_date or infer_session_date(market, moment)

    if should_skip_holiday(market, day):
        empty = DigestSnapshot(
            market=market, session_date=day, status="skipped_holiday"
        )
        return DigestRunResult(
            market=market,
            session_date=day,
            status="skipped_holiday",
            message="",
            sent=False,
            mutation_count=0,
            snapshot=empty,
        )

    window = session_window(market, day)
    if window is None:
        empty = DigestSnapshot(
            market=market, session_date=day, status="skipped_holiday"
        )
        return DigestRunResult(
            market=market,
            session_date=day,
            status="skipped_holiday",
            message="",
            sent=False,
            mutation_count=0,
            snapshot=empty,
        )
    window_start, window_end = window

    counter = None
    if session is not None:
        counter = attach_mutation_counter(session)
        if sources is None:
            sources = SqlAlchemyDigestSources(session)
    if sources is None:
        raise ValueError("sources or session is required")

    try:
        fills = await sources.list_fills(market, window_start, window_end)
        proposals = await sources.list_proposals(market, window_start, window_end)
        retros = await sources.list_retros(market, window_start, window_end)
        fills = merge_retro_pnl(fills, retros)
        snapshot = assemble_snapshot(
            market=market,
            session_date=day,
            fills=fills,
            proposals=proposals,
            window_start=window_start,
            window_end=window_end,
        )
        message = format_digest_message(snapshot)
        mutation_count = counter.total if counter is not None else 0
        if mutation_count != 0:
            return DigestRunResult(
                market=market,
                session_date=day,
                status="aborted_mutation",
                message=message,
                sent=False,
                mutation_count=mutation_count,
                snapshot=snapshot,
            )
        sent = False
        status = snapshot.status
        if send:
            if notifier is None:
                raise ValueError("notifier is required when send=True")
            sent = await send_digest_message(
                message, market=market, session_date=day, notifier=notifier
            )
            if not sent:
                status = "send_failed"
        return DigestRunResult(
            market=market,
            session_date=day,
            status=status,
            message=message,
            sent=sent,
            mutation_count=mutation_count,
            snapshot=snapshot,
        )
    finally:
        if counter is not None:
            counter.detach()
