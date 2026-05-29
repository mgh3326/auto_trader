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
from collections.abc import Awaitable, Callable
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

# ROB-366 B8 — read-only market-scoped news article fetch (market, hours, limit)
# → list of article dicts. Wired in registry.py over the deterministic
# get_news_articles source so this module imports no MCP tooling.
NewsFetchFn = Callable[[str, int, int], Awaitable[list[dict[str, Any]]]]


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
        news_fetch_fn: NewsFetchFn | None = None,
        lookback_hours: int = 24,
        limit: int = 20,
    ) -> None:
        self._session = session
        self._query = query_service or ResearchReportsQueryService(session)
        self._news_fetch_fn = news_fetch_fn
        self._lookback_hours = max(1, lookback_hours)
        self._limit = max(1, limit)

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        since = now - dt.timedelta(hours=self._lookback_hours)

        # ROB-366 B8 — when a market-aware article source is wired, the news
        # dimension serves real (market-scoped) articles in the shape NewsStage
        # reads (``articles``). Falls back to the research_reports citation path
        # when no source is injected (back-compat / tests).
        if self._news_fetch_fn is not None:
            return await self._collect_articles(request, now=now, since=since)

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

    async def _collect_articles(
        self, request: CollectorRequest, *, now: dt.datetime, since: dt.datetime
    ) -> list[SnapshotCollectResult]:
        """Market-scoped news articles path (ROB-366 B8). Fail-open like the
        research path: a fetch error degrades the optional ``news`` kind to
        ``unavailable`` rather than blocking the bundle. An empty result is
        ``partial`` (queried, nothing within the window) — never fabricated."""
        assert self._news_fetch_fn is not None
        try:
            articles = await self._news_fetch_fn(
                request.market, self._lookback_hours, self._limit
            )
        except Exception as exc:  # noqa: BLE001 — optional, fail open
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="news",
                    reason=f"news article fetch failed: {type(exc).__name__}: {exc}",
                    as_of=now,
                )
            ]

        articles_payload = [a for a in (articles or []) if isinstance(a, dict)]
        payload: dict[str, Any] = {
            "since": since.isoformat(),
            "count": len(articles_payload),
            "articles": articles_payload,
            "source": "news_articles",
            "market": request.market,
        }
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="news",
                as_of=now,
                freshness_status="fresh" if articles_payload else "partial",
                coverage={"article_count": len(articles_payload)},
            )
        ]
