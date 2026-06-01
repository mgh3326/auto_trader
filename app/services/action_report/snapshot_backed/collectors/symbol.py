"""Symbol snapshot collector (read-only, optional).

Resolves per-symbol metadata for the symbols the caller asked about and,
where a venue is wired, enriches each resolved symbol with read-only
quote/orderbook evidence (best bid/ask, spread, depth, venue):

* KR equities: metadata from ``stock_info``; quote enrichment for
  ``(market=kr, account_scope=kis_live)`` with an explicit ``user_id``
  via the KIS domestic quote/orderbook adapter.
* Crypto (ROB-369 2c): metadata from ``upbit_symbol_universe`` (crypto is
  not in ``stock_info``); quote enrichment for
  ``(market=crypto, account_scope=upbit_live)`` via the Upbit orderbook
  adapter. Upbit market-data is public, so NO ``user_id`` is required and
  ``last_price`` is left ``None`` (the orderbook carries no last trade —
  spread/depth is the liquidity signal).

Lockdown invariants (ROB-278):

* No new KIS HTTP surface. Quote enrichment uses an injected client that
  the production registry wires to a thin read-only adapter around the
  existing ``inquire_price`` / ``inquire_orderbook`` methods.
* No broker order-mutation surfaces are reachable. The mutation import
  guard test asserts the symbol module does not pull in any verb tagged
  as forbidden (placement / cancellation / submission / modification).
* user_id missing on ``kis_live`` → quote enrichment is fail-closed
  (``status="unavailable"`` per symbol). The default has not been invented.
* Per-symbol failures fail-open: one symbol's KIS error never crashes the
  others, and the symbol kind stays optional in the policy.
* Quote enrichment is bounded by ``quote_enrichment_limit`` (default 25) to
  cap KIS call volume per report; the overflow carries
  ``status="skipped"`` with a ``"cap"`` reason.

If ``request.symbols`` is empty/None we have no scope to read against, so
the collector returns ``unavailable`` and the optional bucket records it
in ``unavailable_sources``.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis import StockInfo
from app.models.upbit_symbol_universe import UpbitSymbolUniverse
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)

_DEFAULT_QUOTE_ENRICHMENT_LIMIT = 25


class _QuoteOrderbookClient(Protocol):
    """Read-only adapter contract for per-symbol quote/orderbook reads.

    Implemented per venue (KIS domestic for KR equities, Upbit for crypto) and
    wired in the collector registry; tests inject fakes. Implementations MUST
    not call any broker order-mutation path.
    """

    async def fetch_quote_orderbook(
        self, symbol: str, venue: str = "krx"
    ) -> dict[str, Any]: ...


def _is_empty_book(quote: dict[str, Any]) -> bool:
    """Return True when the quote payload carries no usable top-of-book."""
    bid = quote.get("best_bid") or 0
    ask = quote.get("best_ask") or 0
    last = quote.get("last_price") or 0
    return bid <= 0 and ask <= 0 and last <= 0


def _derive_spread(quote: dict[str, Any]) -> tuple[float | None, float | None]:
    bid = quote.get("best_bid")
    ask = quote.get("best_ask")
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None, None
    spread = float(ask) - float(bid)
    mid = (float(ask) + float(bid)) / 2.0
    if mid <= 0:
        return spread, None
    spread_bps = (spread / mid) * 10_000.0
    return spread, spread_bps


class SymbolSnapshotCollector:
    """Optional ``symbol`` collector backed by ``stock_info`` (KR/US) or
    ``upbit_symbol_universe`` (crypto).

    The collector also performs read-only per-venue quote/orderbook enrichment
    on the resolved symbols: KIS for KR live trading, Upbit for crypto.
    """

    snapshot_kind: str = "symbol"

    def __init__(
        self,
        session: AsyncSession,
        *,
        kis_quote_client: _QuoteOrderbookClient | None = None,
        upbit_quote_client: _QuoteOrderbookClient | None = None,
        quote_enrichment_limit: int = _DEFAULT_QUOTE_ENRICHMENT_LIMIT,
    ) -> None:
        self._session = session
        self._kis_quote_client = kis_quote_client
        self._upbit_quote_client = upbit_quote_client
        self._quote_enrichment_limit = quote_enrichment_limit

    def _quote_enrichment_plan(
        self, request: CollectorRequest
    ) -> tuple[_QuoteOrderbookClient | None, bool, str, str] | None:
        """Per-venue enrichment plan, or ``None`` when no enrichment applies.

        Returns ``(client, requires_user_id, default_venue, scope_label)``:
        * KR + ``kis_live`` → KIS client, ``user_id`` required (broker auth).
        * crypto + ``upbit_live`` → Upbit client, NO ``user_id`` (public data).
        """
        if request.market == "kr" and request.account_scope == "kis_live":
            venue = "nxt" if request.market_session == "nxt" else "krx"
            return (self._kis_quote_client, True, venue, "kis_live")
        if request.market == "crypto" and request.account_scope == "upbit_live":
            return (self._upbit_quote_client, False, "upbit", "upbit_live")
        return None

    async def _resolve_symbol_payloads(
        self, market: str, symbols: list[str]
    ) -> list[dict[str, Any]]:
        """Resolve per-symbol base metadata from the market's master source.

        Crypto reads ``upbit_symbol_universe`` (keyed by the ``KRW-XXX`` market
        code); KR/US read ``stock_info``. Both return the same payload shape so
        the enrichment loop is venue-uniform.
        """
        if market == "crypto":
            stmt = select(UpbitSymbolUniverse).where(
                UpbitSymbolUniverse.market.in_(symbols)
            )
            rows = (await self._session.execute(stmt)).scalars().all()
            return [
                {
                    "symbol": row.market,
                    "name": row.korean_name,
                    "instrument_type": "crypto",
                    "exchange": "upbit",
                    "sector": None,
                    "market_cap": None,
                    "is_active": row.is_active,
                }
                for row in rows
            ]
        stmt = select(StockInfo).where(StockInfo.symbol.in_(symbols))
        rows = (await self._session.execute(stmt)).scalars().all()
        payloads = [
            {
                "symbol": row.symbol,
                "name": row.name,
                "instrument_type": row.instrument_type,
                "exchange": row.exchange,
                "sector": row.sector,
                "market_cap": row.market_cap,
                "is_active": row.is_active,
            }
            for row in rows
        ]
        if market == "us":
            resolved_syms = {p["symbol"] for p in payloads}
            remaining = [s for s in symbols if s not in resolved_syms]
            if remaining:
                payloads.extend(
                    await self._resolve_us_universe_payloads(remaining)
                )
        return payloads

    async def _resolve_us_universe_payloads(
        self, symbols: list[str]
    ) -> list[dict[str, Any]]:
        """Resolve US symbols absent from ``stock_info`` against the
        ``us_symbol_universe`` master (active rows only)."""
        stmt = select(USSymbolUniverse).where(
            USSymbolUniverse.symbol.in_(symbols),
            USSymbolUniverse.is_active.is_(True),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            {
                "symbol": row.symbol,
                "name": row.name_kr or row.name_en or row.symbol,
                "instrument_type": "equity_us",
                "exchange": row.exchange,
                "sector": None,
                "market_cap": None,
                "is_active": row.is_active,
            }
            for row in rows
        ]

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        symbols = list(request.symbols or [])
        if not symbols:
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason="no symbols supplied; symbol snapshot has no scope",
                    as_of=now,
                )
            ]

        try:
            base_payloads = await self._resolve_symbol_payloads(request.market, symbols)
        except Exception as exc:  # noqa: BLE001 — optional, fail open
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=(
                        f"symbol query failed ({request.market}): "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    as_of=now,
                )
            ]

        # Per-venue quote enrichment plan (KIS for KR live, Upbit for crypto).
        plan = self._quote_enrichment_plan(request)
        quote_status_default: dict[str, Any] | None = None
        if plan is not None:
            _client, requires_user_id, _venue, scope_label = plan
            if requires_user_id and request.user_id is None:
                # Fail-closed: never invent a default user_id for broker calls.
                quote_status_default = {
                    "status": "unavailable",
                    "unavailable_reason": (
                        f"{scope_label} quote enrichment requires explicit user_id"
                    ),
                }

        results: list[SnapshotCollectResult] = []
        seen_symbols: set[str] = set()
        enriched_count = 0
        for base in base_payloads:
            symbol = base["symbol"]
            seen_symbols.add(symbol)
            payload: dict[str, Any] = dict(base)
            if plan is not None:
                client, requires_user_id, default_venue, scope_label = plan
                quote_payload = await self._maybe_enrich_quote(
                    symbol=symbol,
                    client=client,
                    requires_user_id=requires_user_id,
                    default_venue=default_venue,
                    scope_label=scope_label,
                    user_id_present=request.user_id is not None,
                    enriched_count=enriched_count,
                    quote_status_default=quote_status_default,
                )
                payload["quote"] = quote_payload
                if quote_payload.get("status") == "ok":
                    enriched_count += 1
            results.append(
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    symbol=symbol,
                    coverage={"resolved": True},
                )
            )

        missing = [s for s in symbols if s not in seen_symbols]
        if missing:
            results.append(
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload={"missing_symbols": missing},
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"resolved": False, "missing_count": len(missing)},
                )
            )

        if not results:
            # Every symbol missed — return a single partial summary so the
            # caller still records the attempt without forcing 'unavailable'.
            results.append(
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload={"missing_symbols": symbols},
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"resolved": False, "missing_count": len(symbols)},
                )
            )
        return results

    async def _maybe_enrich_quote(
        self,
        *,
        symbol: str,
        client: _QuoteOrderbookClient | None,
        requires_user_id: bool,
        default_venue: str,
        scope_label: str,
        user_id_present: bool,
        enriched_count: int,
        quote_status_default: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if quote_status_default is not None:
            return dict(quote_status_default)
        if requires_user_id and not user_id_present:
            return {
                "status": "unavailable",
                "unavailable_reason": (
                    f"{scope_label} quote enrichment requires explicit user_id"
                ),
            }
        if client is None:
            return {
                "status": "unavailable",
                "unavailable_reason": f"no quote client configured ({default_venue})",
            }
        if enriched_count >= self._quote_enrichment_limit:
            return {
                "status": "skipped",
                "unavailable_reason": (
                    f"quote enrichment cap ({self._quote_enrichment_limit}) reached"
                ),
            }
        try:
            raw = await client.fetch_quote_orderbook(symbol, venue=default_venue)
        except Exception as exc:  # noqa: BLE001 — optional, per-symbol fail-open
            return {
                "status": "unavailable",
                # Venue-tagged so ops can distinguish KIS vs Upbit fetch failures.
                "unavailable_reason": (
                    f"{scope_label}_error: {type(exc).__name__}: {exc}"
                ),
            }
        if not isinstance(raw, dict):
            return {
                "status": "unavailable",
                "unavailable_reason": "quote client returned non-dict payload",
            }
        if _is_empty_book(raw):
            session = raw.get("session")
            reason = "empty_book"
            if isinstance(session, str) and session != "regular":
                reason = f"empty_book ({session})"
            return {
                "status": "unavailable",
                "unavailable_reason": reason,
                "session": session,
                "venue": raw.get("venue"),
            }
        spread, spread_bps = _derive_spread(raw)
        quote: dict[str, Any] = {
            "status": "ok",
            "last_price": raw.get("last_price"),
            "best_bid": raw.get("best_bid"),
            "best_ask": raw.get("best_ask"),
            "spread": spread,
            "spread_bps": spread_bps,
            "bid_depth": raw.get("bid_depth"),
            "ask_depth": raw.get("ask_depth"),
            "venue": raw.get("venue") or default_venue,
            "nxt_eligible": bool(raw.get("nxt_eligible")),
            "session": raw.get("session"),
            "as_of": raw.get("as_of"),
        }
        return quote
