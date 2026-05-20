"""News snapshot collector (read-only, optional).

Reads recent research reports / news ingestor citations via
:class:`ResearchReportsQueryService`. Optional kind — a soft failure here
degrades the bundle to ``partial`` but never blocks the report.

ROB-278 Phase 2 — when ``request.symbols`` is non-empty (the symbol
derivation already unions held/watch/candidate symbols there) the
collector filters citations to those that touch one of the focus
symbols and exposes a ``symbol_matches`` map per focus symbol. When no
citation matches any focus symbol, an explicit ``no_data_reason`` is
surfaced so the report generator can fall through to no-news rather
than infer signal from absence.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)
from app.services.research_reports.query_service import ResearchReportsQueryService


def _citation_symbols(citation: Any) -> set[str]:
    candidates = getattr(citation, "symbol_candidates", None) or []
    symbols: set[str] = set()
    for cand in candidates:
        symbol = getattr(cand, "symbol", None)
        if isinstance(symbol, str) and symbol:
            symbols.add(symbol)
    return symbols


class NewsSnapshotCollector:
    """Optional ``news`` collector backed by ``research_reports``."""

    snapshot_kind: str = "news"

    def __init__(
        self,
        session: AsyncSession,
        *,
        query_service: ResearchReportsQueryService | None = None,
        lookback_hours: int = 24,
        limit: int = 20,
    ) -> None:
        self._session = session
        self._query = query_service or ResearchReportsQueryService(session)
        self._lookback_hours = max(1, lookback_hours)
        self._limit = max(1, limit)

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        since = now - dt.timedelta(hours=self._lookback_hours)

        try:
            response = await self._query.find_relevant(
                since=since,
                limit=self._limit,
            )
        except Exception as exc:  # noqa: BLE001 — optional, fail open
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="news",
                    reason=f"research_reports query failed: {type(exc).__name__}: {exc}",
                    as_of=now,
                )
            ]

        focus_symbols = [s for s in (request.symbols or []) if s]
        focus_set = set(focus_symbols)
        symbol_matches: dict[str, int] = dict.fromkeys(focus_symbols, 0)

        included_citations: list[Any] = []
        for citation in response.citations:
            cit_symbols = _citation_symbols(citation)
            if focus_set:
                hits = cit_symbols & focus_set
                if not hits:
                    continue
                for s in hits:
                    symbol_matches[s] = symbol_matches.get(s, 0) + 1
                included_citations.append(citation)
            else:
                included_citations.append(citation)

        citations_payload: list[dict[str, Any]] = [
            c.model_dump(mode="json") for c in included_citations
        ]
        no_data_reason: str | None = None
        if focus_set and not citations_payload:
            no_data_reason = (
                "no recent citations touched the focus symbols within lookback window"
            )

        payload: dict[str, Any] = {
            "since": since.isoformat(),
            "count": len(citations_payload),
            "citations": citations_payload,
            "symbol_matches": symbol_matches,
            "no_data_reason": no_data_reason,
        }

        if not citations_payload:
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="news",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"citation_count": 0},
                )
            ]

        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="news",
                as_of=now,
                coverage={"citation_count": len(citations_payload)},
            )
        ]
