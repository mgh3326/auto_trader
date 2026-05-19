"""Journal snapshot collector (read-only).

Reads recent and active ``trade_journals`` rows for the live account scope.
The collector never writes journals — :class:`TradeJournalWriteService` is
the only allowed write path, and is intentionally not imported here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade_journal import TradeJournal
from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)

_ACTIVE_STATUSES: tuple[str, ...] = ("draft", "active")
_RETROSPECTIVE_STATUSES: tuple[str, ...] = ("closed", "stopped", "expired")
_DEFAULT_RECENT_LIMIT: int = 20


class JournalSnapshotCollector:
    """Required-kind ``journal`` collector backed by ``trade_journals``."""

    snapshot_kind: str = "journal"

    def __init__(
        self, session: AsyncSession, *, recent_limit: int | None = None
    ) -> None:
        self._session = session
        self._recent_limit = recent_limit or _DEFAULT_RECENT_LIMIT

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()

        active_stmt = (
            select(TradeJournal)
            .where(
                TradeJournal.account_type == "live",
                TradeJournal.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(desc(TradeJournal.updated_at))
        )
        recent_stmt = (
            select(TradeJournal)
            .where(
                TradeJournal.account_type == "live",
                TradeJournal.status.in_(_RETROSPECTIVE_STATUSES),
            )
            .order_by(desc(TradeJournal.updated_at))
            .limit(self._recent_limit)
        )

        active_rows = (await self._session.execute(active_stmt)).scalars().all()
        recent_rows = (await self._session.execute(recent_stmt)).scalars().all()

        payload: dict[str, Any] = {
            "active": [_journal_to_dict(j) for j in active_rows],
            "recent_retrospective": [_journal_to_dict(j) for j in recent_rows],
            "active_count": len(active_rows),
            "retrospective_count": len(recent_rows),
            "recent_limit": self._recent_limit,
        }

        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                coverage={
                    "active_count": len(active_rows),
                    "retrospective_count": len(recent_rows),
                },
            )
        ]


def _journal_to_dict(j: TradeJournal) -> dict[str, Any]:
    return {
        "id": j.id,
        "symbol": j.symbol,
        "instrument_type": j.instrument_type.value
        if hasattr(j.instrument_type, "value")
        else str(j.instrument_type),
        "side": j.side,
        "status": j.status,
        "entry_price": j.entry_price,
        "quantity": j.quantity,
        "thesis": j.thesis,
        "strategy": j.strategy,
        "target_price": j.target_price,
        "stop_loss": j.stop_loss,
        "hold_until": j.hold_until,
        "exit_price": j.exit_price,
        "exit_reason": j.exit_reason,
        "pnl_pct": j.pnl_pct,
        "account_type": j.account_type,
        "created_at": j.created_at,
        "updated_at": j.updated_at,
    }
