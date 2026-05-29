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


def _gainers_row_to_input(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a consecutive_gainers loader row into builder input.

    The loader emits ``close``/``week_change_rate``/``consecutive_up_days``/
    ``volume`` plus ``_screener_snapshot_state`` (ROB-363). The builder's
    momentum branch reads these via the close/volume fallbacks and appends the
    연속 상승 reason.
    """
    return {
        "symbol": row.get("symbol"),
        "name": row.get("name") or row.get("symbol"),
        "source": row.get("source") or "kis",
        "change_rate": row.get("change_rate"),
        "close": row.get("close"),
        "volume": row.get("volume"),
        "consecutive_up_days": row.get("consecutive_up_days"),
    }


def _preset_row_to_input(preset: str, row: dict[str, Any]) -> dict[str, Any]:
    """Normalize any KR Toss-parity loader row into builder input, carrying the
    fundamental fields each preset's builder branch needs (ROB-363)."""
    return {
        "symbol": row.get("symbol"),
        "name": row.get("name") or row.get("symbol"),
        "source": row.get("source") or "kis",
        "change_rate": row.get("change_rate"),
        "close": row.get("close") or row.get("latest_close"),
        "latest_close": row.get("latest_close") or row.get("close"),
        "volume": row.get("volume") or row.get("daily_volume"),
        "consecutive_up_days": row.get("consecutive_up_days"),
        "foreign_consecutive_buy_days": row.get("foreign_consecutive_buy_days"),
        "roe": row.get("roe"),
        "per": row.get("per"),
        "pbr": row.get("pbr"),
    }


def _merge_evidence(
    evidence: list[CandidateEvidence], *, key: Callable[[CandidateEvidence], Hashable]
) -> list[CandidateEvidence]:
    """Merge duplicate-symbol CandidateEvidence across presets (ROB-363).

    Keeps the first occurrence's scalar fields (symbol/score/source_preset) and
    UNIONS ``reasons`` + ``risk_flags`` (order-preserving, deduped) from every
    preset that surfaced the symbol, so per-source provenance is preserved.
    Order-preserving on first occurrence."""
    import dataclasses

    order: list[Hashable] = []
    merged: dict[Hashable, CandidateEvidence] = {}
    for ev in evidence:
        k = key(ev)
        if k not in merged:
            merged[k] = ev
            order.append(k)
            continue
        prev = merged[k]
        reasons = list(prev.reasons)
        for r in ev.reasons:
            if r not in reasons:
                reasons.append(r)
        flags = list(prev.risk_flags)
        for f in ev.risk_flags:
            if f not in flags:
                flags.append(f)
        merged[k] = dataclasses.replace(prev, reasons=reasons, risk_flags=flags)
    return [merged[k] for k in order]


def _toss_parity_status(preset: str, market: str) -> str:
    """ROB-359 Scope E — map the candidate ranking/preset to its Toss-parity
    status so the report can state honestly where a candidate came from.

    The current collector sources candidates from a top-movers ranking
    (``top_gainers`` / ``crypto_momentum``), which is NOT a Toss-parity preset,
    so this returns ``not_toss_parity``. When candidate sourcing is wired to the
    actual Toss-parity catalog presets (candidate strategy — ROB-363; US universe is ROB-346), a real
    ``full``/``partial``/``mismatch`` status flows through automatically.
    """
    from app.services.invest_view_model.screener_presets import get_preset

    preset_def = get_preset(preset, market="crypto" if market == "crypto" else "kr")
    if preset_def is None or preset_def.presetOrigin != "toss_parity":
        return "not_toss_parity"
    return preset_def.parityStatus or "not_toss_parity"


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
        limit = _candidate_limit(request)
        if request.market == "kr":
            preset_result = await self._collect_kr_presets(request, now, limit)
            if preset_result is not None:
                return preset_result
        # US, or KR with no preset rows -> top_gainers momentum fallback.
        return await self._collect_top_gainers(request, now, limit)

    async def _collect_top_gainers(
        self, request: CollectorRequest, now: dt.datetime, limit: int
    ) -> list[SnapshotCollectResult]:
        coverage = await self._equity_repo.coverage(
            market=request.market, today_trading_date=now.date()
        )
        usefulness = _classify_usefulness(
            actionable=coverage.fresh_count, stale=coverage.stale_count
        )
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

    async def _collect_kr_presets(
        self, request: CollectorRequest, now: dt.datetime, limit: int
    ) -> list[SnapshotCollectResult] | None:
        """KR Toss-parity preset sourcing (ROB-363). Returns None when no preset
        produced any rows, so the caller falls back to top_gainers."""
        from app.services.invest_view_model import (
            double_buy_screener,
            high_yield_value_screener,
            screener_service,
        )

        # (preset_id, module, attr) — resolved via getattr at loop time so
        # monkeypatch.setattr(module, attr, fake) is honoured in tests.
        pool_limit = max(limit * 3, limit + 20)
        loaders = [
            (
                "consecutive_gainers",
                screener_service,
                "load_consecutive_gainers_from_snapshots",
            ),
            ("double_buy", double_buy_screener, "load_double_buy_from_snapshots"),
            (
                "high_yield_value",
                high_yield_value_screener,
                "load_high_yield_value_from_snapshots",
            ),
        ]
        evidence: list[CandidateEvidence] = []
        per_state: dict[str, str] = {}  # db_symbol -> fresh|stale
        any_rows = False
        for preset_id, module, attr in loaders:
            loader = getattr(module, attr)
            rows = await loader(self._session, market="kr", limit=pool_limit)
            if not rows:  # None (missing) or [] (stale-empty)
                continue
            any_rows = True
            built = build_candidate_evidence(
                market="kr",
                preset=preset_id,
                rows=[_preset_row_to_input(preset_id, r) for r in rows],
            )
            # Map freshness by symbol from the raw loader rows — NOT by zipping
            # against ``built``, which build_candidate_evidence re-sorts by score
            # (the positions no longer line up). Keyed by db symbol so it stays
            # correct once PR2 fans in multiple presets. A fresh state from ANY
            # preset wins over stale (a symbol fresh in one source is fresh).
            for src_row in rows:
                k = to_db_symbol(str(src_row.get("symbol")))
                state = src_row.get("_screener_snapshot_state") or "fresh"
                if per_state.get(k) != "fresh":
                    per_state[k] = state
            evidence.extend(built)
        if not any_rows:
            return None

        evidence = _merge_evidence(evidence, key=lambda e: to_db_symbol(e.symbol))
        # Distinct evaluated symbols (pre-slice) = the candidate universe size,
        # so ``capped`` reflects a pool wider than the displayed limit.
        universe_count = len(per_state)
        evidence = evidence[:limit]
        return [
            self._build_preset_candidate_result(
                request=request,
                now=now,
                evidence=evidence,
                per_state=per_state,
                candidate_limit=limit,
                universe_count=universe_count,
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
        # ROB-359 Scope E — stamp universe-level lineage onto each candidate dict
        # so a new-buy report item is self-describing (preset hit / freshness /
        # Toss parity status) without needing the universe payload for context.
        toss_parity_status = _toss_parity_status(preset, market)
        candidates = [
            {
                **e.to_payload_dict(),
                "rank": rank,
                "candidate_rank": rank,
                "data_state": freshness_status,
                "toss_parity_status": toss_parity_status,
            }
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

    def _build_preset_candidate_result(
        self,
        *,
        request: CollectorRequest,
        now: dt.datetime,
        evidence: list[CandidateEvidence],
        per_state: dict[str, str],
        candidate_limit: int,
        universe_count: int,
    ) -> SnapshotCollectResult:
        fresh_count = sum(1 for v in per_state.values() if v == "fresh")
        stale_count = sum(1 for v in per_state.values() if v == "stale")
        usefulness = _classify_usefulness(actionable=fresh_count, stale=stale_count)
        candidates: list[dict[str, Any]] = []
        for rank, e in enumerate(evidence, start=1):
            db_sym = to_db_symbol(e.symbol)
            candidates.append(
                {
                    **e.to_payload_dict(),
                    "rank": rank,
                    "candidate_rank": rank,
                    "data_state": per_state.get(db_sym, "fresh"),
                    "toss_parity_status": _toss_parity_status(
                        e.source_preset or "top_gainers", "kr"
                    ),
                }
            )
        capped = universe_count > candidate_limit
        source_coverage = _source_coverage(evidence)
        payload: dict[str, Any] = {
            "market": "kr",
            "preset": "toss_parity_multi",
            "as_of": now.isoformat(),
            "freshness_status": _FRESHNESS_BY_USEFULNESS.get(usefulness, "partial"),
            "source_coverage": source_coverage,
            "candidate_limit": candidate_limit,
            "universe_count": universe_count,
            "capped": capped,
            "candidates": candidates,
            "fresh_count": fresh_count,
            "actionable_count": fresh_count,
            "stale_count": stale_count,
            "last_computed_at": None,
            "usefulness": usefulness,
            "missing_data": _missing_data("kr", usefulness),
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
                "candidate_limit": candidate_limit,
                "universe_count": universe_count,
                "capped": capped,
            },
        )
