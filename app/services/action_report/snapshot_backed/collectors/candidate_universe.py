"""Candidate-universe snapshot collector (read-only, optional).

For ``market=kr|us`` the collector reads :class:`InvestScreenerSnapshot`
counts via ``InvestScreenerSnapshotsRepository.coverage``. For
``market=crypto`` it falls back to a count + latest-partition probe over
:class:`InvestCryptoScreenerSnapshot`. Either branch is read-only and
degrades to ``unavailable`` on exception.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_crypto_screener_snapshot import InvestCryptoScreenerSnapshot
from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)


class CandidateUniverseSnapshotCollector:
    """Optional ``candidate_universe`` collector backed by screener snapshots."""

    snapshot_kind: str = "candidate_universe"

    def __init__(
        self,
        session: AsyncSession,
        *,
        equity_repository: InvestScreenerSnapshotsRepository | None = None,
    ) -> None:
        self._session = session
        self._equity_repo = equity_repository or InvestScreenerSnapshotsRepository(
            session
        )

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        try:
            if request.market in ("kr", "us"):
                return await self._collect_equity(request, now)
            if request.market == "crypto":
                return await self._collect_crypto(request, now)
        except Exception as exc:  # noqa: BLE001 — optional, fail open
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=(
                        f"candidate_universe query failed: {type(exc).__name__}: {exc}"
                    ),
                    as_of=now,
                )
            ]
        return [
            unavailable_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                origin="auto_trader_db",
                reason=f"no candidate_universe wiring for market={request.market!r}",
                as_of=now,
            )
        ]

    async def _collect_equity(
        self, request: CollectorRequest, now: dt.datetime
    ) -> list[SnapshotCollectResult]:
        today = now.date()
        coverage = await self._equity_repo.coverage(
            market=request.market, today_trading_date=today
        )
        payload: dict[str, Any] = {
            "market": coverage.market,
            "today_trading_date": coverage.today_trading_date.isoformat(),
            "fresh_count": coverage.fresh_count,
            "stale_count": coverage.stale_count,
            "last_computed_at": coverage.last_computed_at,
        }
        if coverage.fresh_count == 0 and coverage.stale_count == 0:
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"fresh_count": 0, "stale_count": 0},
                )
            ]
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                coverage={
                    "fresh_count": coverage.fresh_count,
                    "stale_count": coverage.stale_count,
                },
            )
        ]

    async def _collect_crypto(
        self, request: CollectorRequest, now: dt.datetime
    ) -> list[SnapshotCollectResult]:
        latest_date_row = await self._session.execute(
            select(func.max(InvestCryptoScreenerSnapshot.snapshot_date))
        )
        latest_date = latest_date_row.scalar_one_or_none()
        if latest_date is None:
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload={"market": "crypto", "fresh_count": 0},
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"fresh_count": 0},
                )
            ]
        count_row = await self._session.execute(
            select(func.count()).where(
                InvestCryptoScreenerSnapshot.snapshot_date == latest_date
            )
        )
        count = int(count_row.scalar_one() or 0)
        payload: dict[str, Any] = {
            "market": "crypto",
            "latest_partition": latest_date.isoformat(),
            "fresh_count": count,
        }
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                coverage={"fresh_count": count},
            )
        ]
