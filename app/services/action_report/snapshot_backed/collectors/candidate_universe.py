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
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.invest_crypto_screener_snapshots.repository import (
    InvestCryptoScreenerSnapshotsRepository,
)
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)
from app.services.screener_evidence import build_candidate_evidence

TOP_N = 10

_FRESHNESS_BY_USEFULNESS = {"useful": "fresh", "stale_only": "stale", "empty": "missing"}


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


def _equity_row_to_input(row: InvestScreenerSnapshot) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "name": row.symbol,
        "source": row.source,
        "change_rate": row.change_rate,
        "price": row.latest_close,
        "daily_volume": row.daily_volume,
        "consecutive_up_days": row.consecutive_up_days,
    }


def _crypto_row_to_input(row: InvestCryptoScreenerSnapshot) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "name": row.name,
        "source": row.source,
        "change_rate": row.change_rate,
        "price": row.latest_close,
        "rsi": row.rsi,
        "adx": row.adx,
        "trade_amount_24h": row.trade_amount_24h,
        "volume_24h": row.volume_24h,
        "market_cap": row.market_cap,
        "market_warning": row.market_warning,
    }


def _source_coverage(evidence: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ev in evidence:
        counts[ev.source] = counts.get(ev.source, 0) + 1
    return counts


def _missing_data(market: str, usefulness: str) -> dict[str, str] | None:
    if usefulness == "useful":
        return None
    market_ko = {"crypto": "암호화폐", "kr": "국내", "us": "미국"}.get(market, market)
    if usefulness == "stale_only":
        return {
            "what": f"{market_ko} 스크리너 스냅샷이 최신 거래일 기준이 아닙니다 (stale).",
            "why": "최신 모멘텀/거래대금 교차검증이 제한되어 신규 후보 판단 신뢰도가 낮아집니다.",
            "next": "스크리너 스냅샷 리프레시가 최신 거래일로 갱신되면 개선됩니다.",
            "confidence_impact": "cap 40",
        }
    return {
        "what": f"{market_ko} 스크리너 스냅샷이 비어 있습니다.",
        "why": "후보 유니버스를 평가할 수 없어 신규 매수 후보 판단 신뢰도가 제한됩니다.",
        "next": "스크리너 스냅샷 리프레시가 활성화되면 개선됩니다.",
        "confidence_impact": "cap 20",
    }


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
        usefulness, _reason = _classify_usefulness(
            actionable=coverage.fresh_count, stale=coverage.stale_count
        )
        rows = await self._equity_repo.list_top_candidates(
            market=request.market, limit=TOP_N
        )
        evidence = build_candidate_evidence(
            market=request.market,
            preset="top_gainers",
            rows=[_equity_row_to_input(r) for r in rows],
        )
        return [
            self._build_candidate_result(
                request=request,
                now=now,
                market=request.market,
                preset="top_gainers",
                evidence=evidence,
                fresh_count=coverage.fresh_count,
                stale_count=coverage.stale_count,
                last_computed_at=coverage.last_computed_at,
                usefulness=usefulness,
            )
        ]

    async def _collect_crypto(
        self, request: CollectorRequest, now: dt.datetime
    ) -> list[SnapshotCollectResult]:
        crypto_repo = InvestCryptoScreenerSnapshotsRepository(self._session)
        cov = await crypto_repo.coverage(today=now.date())
        usefulness, _reason = _classify_usefulness(
            actionable=cov.latest_partition_count, stale=cov.stale_count
        )
        rows = await crypto_repo.list_latest(preset_id="crypto_momentum", limit=TOP_N)
        evidence = build_candidate_evidence(
            market="crypto",
            preset="crypto_momentum",
            rows=[_crypto_row_to_input(r) for r in rows],
        )
        return [
            self._build_candidate_result(
                request=request,
                now=now,
                market="crypto",
                preset="crypto_momentum",
                evidence=evidence,
                fresh_count=cov.latest_partition_count,
                stale_count=cov.stale_count,
                last_computed_at=cov.last_computed_at,
                usefulness=usefulness,
            )
        ]

    def _build_candidate_result(
        self,
        *,
        request: CollectorRequest,
        now: dt.datetime,
        market: str,
        preset: str,
        evidence: list,
        fresh_count: int,
        stale_count: int,
        last_computed_at: dt.datetime | None,
        usefulness: str,
    ) -> SnapshotCollectResult:
        freshness_status = _FRESHNESS_BY_USEFULNESS.get(usefulness, "partial")
        candidates = [e.to_payload_dict() for e in evidence]
        missing = _missing_data(market, usefulness)
        payload: dict[str, Any] = {
            "market": market,
            "preset": preset,
            "as_of": now.isoformat(),
            "freshness_status": freshness_status,
            "source_coverage": _source_coverage(evidence),
            "candidates": candidates,
            "fresh_count": fresh_count,
            "actionable_count": fresh_count,
            "stale_count": stale_count,
            "last_computed_at": last_computed_at.isoformat() if last_computed_at else None,
            "usefulness": usefulness,
            "missing_data": missing,
        }
        return build_result(
            snapshot_kind=self.snapshot_kind,
            market=request.market,
            account_scope=request.account_scope,
            payload=payload,
            origin="auto_trader_db",
            as_of=now,
            freshness_status="fresh" if usefulness == "useful" else "partial",
            coverage={
                "actionable_count": fresh_count,
                "stale_count": stale_count,
                "usefulness": usefulness,
                "candidate_count": len(candidates),
            },
        )
