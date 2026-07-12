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
from app.services.action_report.snapshot_backed.collectors.investor_flow import (
    InvestorFlowSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.journal import (
    JournalSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.kr_market_ranking import (
    KrMarketRankingSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.market import (
    AltseasonFn,
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


# ROB-390 — venue -> KIS domestic market-division code. "J"=KRX, "NX"=NXT.
_VENUE_TO_KIS_MARKET_CODE = {"krx": "J", "nxt": "NX"}
_US_OVERSEAS_EXCHANGE_CODES_BY_VENUE = {
    "us": ("NASD", "NYSE", "AMEX"),
    "nasd": ("NASD",),
    "nas": ("NASD",),
    "nasdaq": ("NASD",),
    "nyse": ("NYSE",),
    "nys": ("NYSE",),
    "amex": ("AMEX",),
    "ams": ("AMEX",),
}


class _KISDomesticQuoteOrderbookAdapter:
    """ROB-278 Phase 2 — read-only adapter wrapping existing KIS quote paths.

    KR requests use domestic current-price + orderbook calls. US requests use
    the existing KIS overseas current-price call and intentionally leave
    top-of-book fields ``None`` because that endpoint does not provide depth.
    Exposes a single ``fetch_quote_orderbook(symbol, venue)`` method so the
    symbol collector cannot reach order placement, cancel, or modify paths via
    the bound client.
    """

    def __init__(self, kis_client: KISClient | None) -> None:
        self._client = kis_client

    async def fetch_quote_orderbook(
        self, symbol: str, venue: str = "krx"
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("kis client unavailable")
        if (venue or "").lower() in _US_OVERSEAS_EXCHANGE_CODES_BY_VENUE:
            return await self._fetch_overseas_quote(symbol, venue=venue)
        # Two read-only calls — both already exist on the KIS client. No
        # new HTTP surface, no order placement/cancellation paths reached.
        market_code = _VENUE_TO_KIS_MARKET_CODE.get(venue, "J")
        price_df = await self._client.inquire_price(symbol)
        orderbook = await self._client.inquire_orderbook(symbol, market=market_code)

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
            "venue": venue if venue in _VENUE_TO_KIS_MARKET_CODE else "krx",
            "as_of": None,  # KIS orderbook payload has no clean as_of; UI uses snapshot.as_of
            "session": "regular" if best_bid > 0 and best_ask > 0 else "closed",
            "nxt_eligible": bool(nxt_eligible),
        }

    async def _fetch_overseas_quote(
        self, symbol: str, venue: str = "us"
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("kis client unavailable")
        venue_key = (venue or "us").lower()
        exchange_codes = _US_OVERSEAS_EXCHANGE_CODES_BY_VENUE.get(
            venue_key, _US_OVERSEAS_EXCHANGE_CODES_BY_VENUE["us"]
        )
        last_df = None
        used_exchange = exchange_codes[0]
        for exchange_code in exchange_codes:
            price_df = await self._client.inquire_overseas_price(
                symbol, exchange_code=exchange_code
            )
            last_df = price_df
            used_exchange = exchange_code
            if len(price_df):
                break

        def _df_num(column: str) -> float | int | None:
            if last_df is None or not len(last_df) or column not in last_df:
                return None
            value = last_df[column].iloc[0]
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        last_price = _df_num("close")
        return {
            "last_price": last_price,
            "best_bid": None,
            "best_ask": None,
            "bid_depth": None,
            "ask_depth": None,
            "venue": "us",
            "exchange_code": used_exchange,
            "previous_close": _df_num("previous_close"),
            "volume": _df_num("volume"),
            "as_of": None,
            "session": "delayed" if (last_price or 0) > 0 else "closed",
            "nxt_eligible": False,
        }


class _UpbitQuoteOrderbookAdapter:
    """ROB-369 2c — read-only adapter wrapping the public Upbit orderbook read
    for per-symbol crypto liquidity. Exposes the same
    ``fetch_quote_orderbook(symbol)`` contract as the KIS adapter so the symbol
    collector treats venues uniformly.

    Public market-data only — no Upbit account/auth surface and no order
    placement/cancel paths are reachable. ``last_price`` is left ``None`` (the
    orderbook carries no last trade); the spread/depth derived from the
    top-of-book is the liquidity signal the symbol stage reads.
    """

    async def fetch_quote_orderbook(
        self, symbol: str, venue: str = "krx"
    ) -> dict[str, Any]:
        _ = venue  # Upbit has a single venue; argument kept for protocol parity.
        # Lazy import keeps httpx / the Upbit module out of the registry import
        # graph (mirrors the news / index fns) and narrow to the read function.
        from app.services.upbit_orderbook import fetch_orderbook

        raw = await fetch_orderbook(symbol)
        units = (raw or {}).get("orderbook_units") or []
        top = units[0] if units else {}
        timestamp = (raw or {}).get("timestamp")
        return {
            "last_price": None,
            "best_bid": top.get("bid_price"),
            "best_ask": top.get("ask_price"),
            "bid_depth": top.get("bid_size"),
            "ask_depth": top.get("ask_size"),
            "venue": "upbit",
            "as_of": str(timestamp) if timestamp else None,
            "session": "24h",
            "nxt_eligible": False,
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


def _build_altseason_fn() -> AltseasonFn:
    """Read-only adapter over the Upbit altseason source (ROB-381 PR3).

    Returns the UBAI/UBMI ratio + 24h alt-vs-BTC breadth snapshot. Failures are
    deliberately allowed to reach ``MarketEventsSnapshotCollector`` so it can
    retain the original diagnostic while degrading only the optional breadth
    field. No order/mutation surface.
    """

    async def _altseason_fn() -> dict[str, Any] | None:
        from app.services.external.upbit_index import fetch_upbit_altseason

        return await fetch_upbit_altseason()

    return _altseason_fn


def _build_news_fetch_fn() -> NewsFetchFn:
    """Per-symbol on-demand news adapter over ``symbol_news_service`` (ROB-423).

    Given (symbol, market, limit) returns a normalized ``SymbolNewsFetchResult``.
    Imported lazily; no MCP/LLM/order surface. The collector wraps the call so a
    fetch error degrades the optional ``news`` kind without blocking the bundle.
    """

    async def _news_fetch_fn(symbol: str, market: str, limit: int):
        from app.services.symbol_news_service import fetch_symbol_news

        return await fetch_symbol_news(symbol, market, limit=limit)

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
            session,
            index_quote_fn=_build_market_index_quote_fn(),
            altseason_fn=_build_altseason_fn(),
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
    # ROB-369 2c — also wire the public Upbit orderbook adapter so crypto +
    # upbit_live requests get per-symbol liquidity evidence.
    registry.register(
        SymbolSnapshotCollector(
            session,
            kis_quote_client=_KISDomesticQuoteOrderbookAdapter(
                _build_kis_client_safely()
            ),
            upbit_quote_client=_UpbitQuoteOrderbookAdapter(),
        )
    )
    registry.register(CandidateUniverseSnapshotCollector(session))
    registry.register(KrMarketRankingSnapshotCollector(session))
    registry.register(InvestorFlowSnapshotCollector(session))
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
