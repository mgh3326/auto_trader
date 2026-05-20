"""Candidate-universe snapshot collector (read-only, optional).

For ``market=kr|us`` the collector reads :class:`InvestScreenerSnapshot`
counts via ``InvestScreenerSnapshotsRepository.coverage``. For
``market=crypto`` it falls back to a count + latest-partition probe over
:class:`InvestCryptoScreenerSnapshot`. Either branch is read-only and
degrades to ``unavailable`` on exception.

ROB-278 Phase 2 — payload separates freshness from usefulness:

* ``actionable_count`` is the count the report generator should consult
  before fabricating buy candidates. ``fresh_count`` from the repository
  is the same number, surfaced under an explicit name so the contract
  is unambiguous to downstream code.
* ``usefulness`` is one of ``"useful" | "stale_only" | "empty"``.
  ``stale_only`` means rows exist but none are fresh (the previous
  failure mode was a ``freshness_status="fresh"`` snapshot with
  ``fresh_count=0`` that still encouraged the generator to act).
* ``no_data_reason`` carries a human-readable explanation when
  ``usefulness != "useful"``.
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


def _classify_usefulness(*, actionable: int, stale: int) -> tuple[str, str | None]:
    """Return ``(usefulness, no_data_reason)`` from counts."""
    if actionable > 0:
        return "useful", None
    if stale > 0:
        return (
            "stale_only",
            f"no fresh candidates today; {stale} stale row(s) only",
        )
    return "empty", "candidate_universe has no rows for this market"


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
        usefulness, no_data_reason = _classify_usefulness(
            actionable=coverage.fresh_count, stale=coverage.stale_count
        )
        payload: dict[str, Any] = {
            "market": coverage.market,
            "today_trading_date": coverage.today_trading_date.isoformat(),
            "fresh_count": coverage.fresh_count,
            "actionable_count": coverage.fresh_count,
            "stale_count": coverage.stale_count,
            "last_computed_at": coverage.last_computed_at,
            "usefulness": usefulness,
            "no_data_reason": no_data_reason,
        }
        coverage_meta = {
            "actionable_count": coverage.fresh_count,
            "stale_count": coverage.stale_count,
            "usefulness": usefulness,
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
                    coverage=coverage_meta,
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
                coverage=coverage_meta,
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
            usefulness, no_data_reason = _classify_usefulness(actionable=0, stale=0)
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload={
                        "market": "crypto",
                        "fresh_count": 0,
                        "actionable_count": 0,
                        "stale_count": 0,
                        "usefulness": usefulness,
                        "no_data_reason": no_data_reason,
                    },
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"actionable_count": 0, "usefulness": usefulness},
                )
            ]
        count_row = await self._session.execute(
            select(func.count()).where(
                InvestCryptoScreenerSnapshot.snapshot_date == latest_date
            )
        )
        count = int(count_row.scalar_one() or 0)
        usefulness, no_data_reason = _classify_usefulness(actionable=count, stale=0)
        payload: dict[str, Any] = {
            "market": "crypto",
            "latest_partition": latest_date.isoformat(),
            "fresh_count": count,
            "actionable_count": count,
            "stale_count": 0,
            "usefulness": usefulness,
            "no_data_reason": no_data_reason,
        }
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                coverage={"actionable_count": count, "usefulness": usefulness},
            )
        ]
