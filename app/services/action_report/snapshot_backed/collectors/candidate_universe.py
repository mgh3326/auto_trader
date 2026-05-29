"""Candidate-universe snapshot collector (read-only, optional).

For ``market=kr|us`` the collector loads the latest ``InvestScreenerSnapshot``
partition's top movers; for ``market=crypto`` the latest
``InvestCryptoScreenerSnapshot`` partition. Rows are normalized into
``CandidateEvidence`` (symbols, 0-10 scores, Korean reasons, source
provenance) and serialized into the payload alongside coverage counts,
source provenance, and structured Korean missing-data. Either branch is
read-only and degrades to ``unavailable`` on exception.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Hashable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
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
from app.services.screener_evidence import CandidateEvidence, build_candidate_evidence

TOP_N = 10

_FRESHNESS_BY_USEFULNESS = {
    "useful": "fresh",
    "stale_only": "stale",
    "empty": "missing",
}


def _classify_usefulness(*, actionable: int, stale: int) -> str:
    """Map fresh/stale counts to the usefulness contract.

    ``useful`` means actionable (fresh) rows exist; ``stale_only`` means
    only stale rows exist (candidates can still be surfaced but freshness
    is degraded); ``empty`` means no rows at all.
    """
    if actionable > 0:
        return "useful"
    if stale > 0:
        return "stale_only"
    return "empty"


def _candidate_limit(request: CollectorRequest) -> int:
    if request.candidate_limit is None:
        return TOP_N
    return max(0, request.candidate_limit)


def _dedupe_rows(rows: list[Any], *, key: Callable[[Any], Hashable]) -> list[Any]:
    """Order-preserving dedupe on ``key(row)``.

    ROB-352 Slice C — screener rows can repeat one instrument under symbol
    format variants (BRK.B / BRK-B / BRK/B). Rows arrive ordered by
    ``change_rate DESC``, so keeping the first occurrence keeps the
    highest-ranked one. Hygiene only — no ranking/filter changes.
    """
    seen: set[Any] = set()
    out: list[Any] = []
    for row in rows:
        k = key(row)
        if k in seen:
            continue
        seen.add(k)
        out.append(row)
    return out


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


def _source_coverage(evidence: list[CandidateEvidence]) -> dict[str, int]:
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
        crypto_repository: InvestCryptoScreenerSnapshotsRepository | None = None,
    ) -> None:
        self._session = session
        self._equity_repo = equity_repository or InvestScreenerSnapshotsRepository(
            session
        )
        self._crypto_repo = (
            crypto_repository or InvestCryptoScreenerSnapshotsRepository(session)
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
        coverage = await self._equity_repo.coverage(
            market=request.market, today_trading_date=now.date()
        )
        usefulness = _classify_usefulness(
            actionable=coverage.fresh_count, stale=coverage.stale_count
        )
        limit = _candidate_limit(request)
        rows = await self._equity_repo.list_top_candidates(
            market=request.market, limit=limit
        )
        rows = _dedupe_rows(rows, key=lambda r: to_db_symbol(r.symbol))
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
                candidate_limit=limit,
                fresh_count=coverage.fresh_count,
                stale_count=coverage.stale_count,
                last_computed_at=coverage.last_computed_at,
                usefulness=usefulness,
            )
        ]

    async def _collect_crypto(
        self, request: CollectorRequest, now: dt.datetime
    ) -> list[SnapshotCollectResult]:
        cov = await self._crypto_repo.coverage(today=now.date())
        usefulness = _classify_usefulness(
            actionable=cov.latest_partition_count, stale=cov.stale_count
        )
        limit = _candidate_limit(request)
        rows = await self._crypto_repo.list_latest(
            preset_id="crypto_momentum", limit=limit
        )
        # Upbit market codes (KRW-BTC, …) are canonical from a single source;
        # no symbol-format normalization needed (unlike equity tickers).
        rows = _dedupe_rows(rows, key=lambda r: r.symbol)
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
                candidate_limit=limit,
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
        evidence: list[CandidateEvidence],
        candidate_limit: int,
        fresh_count: int,
        stale_count: int,
        last_computed_at: dt.datetime | None,
        usefulness: str,
    ) -> SnapshotCollectResult:
        freshness_status = _FRESHNESS_BY_USEFULNESS.get(usefulness, "partial")
        candidates = [
            {**e.to_payload_dict(), "rank": rank, "candidate_rank": rank}
            for rank, e in enumerate(evidence, start=1)
        ]
        universe_count = fresh_count + stale_count
        capped = universe_count > candidate_limit
        payload: dict[str, Any] = {
            "market": market,
            "preset": preset,
            "as_of": now.isoformat(),
            "freshness_status": freshness_status,
            "source_coverage": _source_coverage(evidence),
            "candidate_limit": candidate_limit,
            "universe_count": universe_count,
            "capped": capped,
            "candidates": candidates,
            "fresh_count": fresh_count,
            "actionable_count": fresh_count,
            "stale_count": stale_count,
            "last_computed_at": last_computed_at.isoformat()
            if last_computed_at
            else None,
            "usefulness": usefulness,
            "missing_data": _missing_data(market, usefulness),
        }
        return build_result(
            snapshot_kind=self.snapshot_kind,
            market=request.market,
            account_scope=request.account_scope,
            payload=payload,
            origin="auto_trader_db",
            as_of=now,
            # Optional kind: non-useful degrades the bundle to ``partial``,
            # never fails it.
            freshness_status="fresh" if usefulness == "useful" else "partial",
            coverage={
                "actionable_count": fresh_count,
                "stale_count": stale_count,
                "usefulness": usefulness,
                "candidate_count": len(candidates),
                "candidate_limit": candidate_limit,
                "universe_count": universe_count,
                "capped": capped,
            },
        )
