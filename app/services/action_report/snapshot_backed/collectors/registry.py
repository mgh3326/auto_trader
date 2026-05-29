"""Production collector registry for the snapshot-backed report generator.

This module assembles a :class:`SnapshotCollectorRegistry` populated with
the read-only collectors in this package. It is *separate* from
:func:`app.services.investment_snapshots.collectors.default_collector_registry`,
which intentionally remains empty (Phase 2 invariant) so existing callers
that rely on the bundle service for unrelated purposes are unaffected.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
    CandidateUniverseSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.invest_page import (
    InvestPageSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.journal import (
    JournalSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.market import (
    IndexQuoteFn,
    MarketEventsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.news import (
    NewsFetchFn,
    NewsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.optional_stubs import (
    BrowserProbeStubCollector,
    NaverRemoteDebugStubCollector,
    TossRemoteDebugStubCollector,
)
from app.services.action_report.snapshot_backed.collectors.pending_orders import (
    PendingOrdersSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.portfolio import (
    PortfolioSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.symbol import (
    SymbolSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.watch_context import (
    WatchContextSnapshotCollector,
)
from app.services.brokers.kis.client import KISClient
from app.services.brokers.upbit.orders import (
    fetch_open_orders as _upbit_fetch_open_orders,
)
from app.services.investment_snapshots.collectors import SnapshotCollectorRegistry
from app.services.kr_symbol_universe_service import is_nxt_eligible


class _UpbitOpenOrdersAdapter:
    """Read-only adapter exposing only ``fetch_open_orders``.

    The Upbit broker module also exports order placement/cancellation
    functions. Wrapping just the read function here keeps the registry
    wiring intentionally narrow — the collector cannot reach mutation
    paths via the bound client.
    """

    @staticmethod
    async def fetch_open_orders(market: str | None = None) -> list[dict[str, Any]]:
        return await _upbit_fetch_open_orders(market=market)


class _KISDomesticQuoteOrderbookAdapter:
    """ROB-278 Phase 2 — read-only adapter wrapping the existing KIS quote
    + orderbook calls. Exposes a single ``fetch_quote_orderbook(symbol)``
    method so the symbol collector cannot reach order placement, cancel,
    or modify paths via the bound client.

    The adapter pulls last price from ``inquire_price`` and top-of-book
    from ``inquire_orderbook``; both are existing read-only methods on
    the KIS domestic market data client. No new HTTP surface is added.
    """

    def __init__(self, kis_client: KISClient | None) -> None:
        self._client = kis_client

    async def fetch_quote_orderbook(self, symbol: str) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("kis client unavailable")
        # Two read-only calls — both already exist on the KIS client. No
        # new HTTP surface, no order placement/cancellation paths reached.
        price_df = await self._client.inquire_price(symbol)
        orderbook = await self._client.inquire_orderbook(symbol)

        last_price = float(price_df["close"].iloc[0]) if len(price_df) else 0.0

        # Top-of-book from the 10-step orderbook. KIS field naming uses
        # ``askp1`` / ``bidp1`` for level-1 ask/bid price; ``askp_rsqn1`` /
        # ``bidp_rsqn1`` for residual quantities. Use ``.get`` with defaults
        # so the empty-book branch in the collector takes over cleanly.
        def _num(name: str) -> float:
            value = orderbook.get(name)
            try:
                return float(value) if value is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        best_ask = _num("askp1")
        best_bid = _num("bidp1")
        ask_depth = _num("askp_rsqn1")
        bid_depth = _num("bidp_rsqn1")

        # Best-effort NXT routability flag — symbol-universe lookup is
        # read-only and cheap; failures stay non-fatal.
        try:
            nxt_eligible = await is_nxt_eligible(symbol)
        except Exception:  # noqa: BLE001
            nxt_eligible = False

        return {
            "last_price": last_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "venue": "krx",
            "as_of": None,  # KIS orderbook payload has no clean as_of; UI uses snapshot.as_of
            "session": "regular" if best_bid > 0 and best_ask > 0 else "closed",
            "nxt_eligible": bool(nxt_eligible),
        }


def _build_market_index_quote_fn() -> IndexQuoteFn:
    """Read-only adapter over the deterministic fundamentals index source.

    Given index symbols, returns one row per resolved index by calling the
    yfinance/Naver-backed ``get_market_index`` handler per symbol (concurrently).
    Fail-open per symbol: a symbol whose fetch errors is simply omitted. The
    handler is imported lazily so the heavy yfinance dependency is not pulled at
    registry import time, and this stays a thin pass-through (the per-market
    symbol selection lives in the collector). No order/mutation surface.
    """

    async def _index_quote_fn(symbols: list[str]) -> list[dict[str, Any]]:
        import asyncio

        from app.mcp_server.tooling.fundamentals._market_index import (
            handle_get_market_index,
        )

        async def _one(sym: str) -> list[dict[str, Any]]:
            try:
                result = await handle_get_market_index(
                    symbol=sym, period="day", count=1
                )
            except Exception:  # noqa: BLE001 — best-effort index quote
                return []
            if not isinstance(result, dict):
                return []
            return [r for r in (result.get("indices") or []) if isinstance(r, dict)]

        gathered = await asyncio.gather(*[_one(sym) for sym in symbols])
        return [row for rows in gathered for row in rows]

    return _index_quote_fn


def _build_news_fetch_fn() -> NewsFetchFn:
    """Read-only adapter over the deterministic, market-aware news source.

    ROB-366 B8: given (market, hours, limit), returns recent market-scoped
    ``NewsArticle`` rows mapped to plain dicts in the shape NewsStage reads
    (``articles``). ``get_news_articles`` is imported lazily and uses its own
    read-only session; this stays a thin pass-through with no order/mutation
    surface. The collector wraps the call so a fetch error degrades the
    optional ``news`` kind to ``unavailable``.
    """

    async def _news_fetch_fn(
        market: str, hours: int, limit: int
    ) -> list[dict[str, Any]]:
        from app.services.llm_news_service import get_news_articles

        articles, _total = await get_news_articles(
            market=market, hours=hours, limit=limit
        )
        out: list[dict[str, Any]] = []
        for a in articles:
            published = getattr(a, "article_published_at", None)
            out.append(
                {
                    "title": a.title,
                    "url": a.url,
                    "source": a.source,
                    "feed_source": a.feed_source,
                    "summary": a.summary,
                    "stock_symbol": a.stock_symbol,
                    "stock_name": a.stock_name,
                    "published_at": published.isoformat() if published else None,
                }
            )
        return out

    return _news_fetch_fn


def _build_kis_client_safely() -> KISClient | None:
    """Construct the KIS client used by the pending-orders collector.

    ``KISClient()`` reads credentials lazily and does not perform network
    I/O at construction time, but if settings are misconfigured the
    constructor could still raise. Returning ``None`` on failure keeps
    the registry usable; the collector falls back to ``unavailable``.
    """
    try:
        return KISClient()
    except Exception:  # noqa: BLE001 — registry must not raise on wiring
        return None


def production_collector_registry(session: AsyncSession) -> SnapshotCollectorRegistry:
    """Return a populated registry for the snapshot-backed generator.

    Required-kind collectors are wired to read-only DB-backed services.
    Optional-kind collectors are either thin DB readers (news) or
    fail-open stubs. Adding a new collector here is the single place
    needed to expose it to the generator.
    """
    registry = SnapshotCollectorRegistry()

    # Required kinds — DB-backed, read-only.
    registry.register(PortfolioSnapshotCollector(session))
    registry.register(JournalSnapshotCollector(session))
    registry.register(WatchContextSnapshotCollector(session))
    registry.register(
        MarketEventsSnapshotCollector(
            session, index_quote_fn=_build_market_index_quote_fn()
        )
    )

    # Optional kinds — DB-backed where possible. ROB-366 B8 — wire the
    # market-aware news article source so US (and KR) bundles serve real
    # market-scoped news instead of an empty/KR-bleeding research feed.
    registry.register(
        NewsSnapshotCollector(session, news_fetch_fn=_build_news_fetch_fn())
    )
    # ROB-278 Phase 2 — wire the KIS quote/orderbook adapter so KR + kis_live
    # requests get per-symbol quote evidence. Construction is wrapped (the
    # KIS client can be None when credentials are absent) so the collector
    # cleanly emits per-symbol unavailable rather than crashing.
    registry.register(
        SymbolSnapshotCollector(
            session,
            kis_quote_client=_KISDomesticQuoteOrderbookAdapter(
                _build_kis_client_safely()
            ),
        )
    )
    registry.register(CandidateUniverseSnapshotCollector(session))
    registry.register(InvestPageSnapshotCollector(session))
    # Remote-debug probes remain fail-open stubs — they are operator-driven
    # only, and automated wiring is intentionally out of scope.
    registry.register(NaverRemoteDebugStubCollector())
    registry.register(TossRemoteDebugStubCollector())
    registry.register(BrowserProbeStubCollector())

    # ROB-274 — optional/fail-open. Wires the KIS client + a narrow Upbit
    # read-only adapter (``fetch_open_orders`` only). Construction is
    # wrapped to keep the registry usable when broker credentials are
    # absent or misconfigured; the collector then emits ``unavailable``.
    registry.register(
        PendingOrdersSnapshotCollector(
            kis_client=_build_kis_client_safely(),
            upbit_client=_UpbitOpenOrdersAdapter(),
        )
    )

    return registry
