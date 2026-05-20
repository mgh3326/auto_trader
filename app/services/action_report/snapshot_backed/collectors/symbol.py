"""Symbol snapshot collector (read-only, optional).

Reads ``stock_info`` master rows for the symbols the caller asked about,
and for ``(market=kr, account_scope=kis_live)`` with an explicit
``user_id``, enriches each resolved symbol with read-only KIS
quote/orderbook evidence (last price, best bid/ask, spread, depth, venue).

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


class _KISQuoteOrderbookClient(Protocol):
    """Read-only adapter contract for per-symbol KIS quote/orderbook reads.

    The default production wiring lives in the collector registry; tests
    inject fakes. Implementations MUST not call any broker mutation path.
    """

    async def fetch_quote_orderbook(self, symbol: str) -> dict[str, Any]: ...


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
    """Optional ``symbol`` collector backed by ``stock_info``.

    The collector also performs read-only KIS quote/orderbook enrichment
    on the resolved symbols when the request targets KR live trading.
    """

    snapshot_kind: str = "symbol"

    def __init__(
        self,
        session: AsyncSession,
        *,
        kis_quote_client: _KISQuoteOrderbookClient | None = None,
        quote_enrichment_limit: int = _DEFAULT_QUOTE_ENRICHMENT_LIMIT,
    ) -> None:
        self._session = session
        self._kis_quote_client = kis_quote_client
        self._quote_enrichment_limit = quote_enrichment_limit

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
            stmt = select(StockInfo).where(StockInfo.symbol.in_(symbols))
            rows = (await self._session.execute(stmt)).scalars().all()
        except Exception as exc:  # noqa: BLE001 — optional, fail open
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"stock_info query failed: {type(exc).__name__}: {exc}",
                    as_of=now,
                )
            ]

        # Quote enrichment policy (ROB-278 Phase 2).
        wants_quote = request.market == "kr" and request.account_scope == "kis_live"
        quote_status_default: dict[str, Any] | None
        if wants_quote and request.user_id is None:
            # Fail-closed: never invent a default user_id for broker calls.
            quote_status_default = {
                "status": "unavailable",
                "unavailable_reason": (
                    "kis_live quote enrichment requires explicit user_id"
                ),
            }
        else:
            quote_status_default = None

        results: list[SnapshotCollectResult] = []
        seen_symbols: set[str] = set()
        enriched_count = 0
        for row in rows:
            seen_symbols.add(row.symbol)
            payload: dict[str, Any] = {
                "symbol": row.symbol,
                "name": row.name,
                "instrument_type": row.instrument_type,
                "exchange": row.exchange,
                "sector": row.sector,
                "market_cap": row.market_cap,
                "is_active": row.is_active,
            }
            if wants_quote:
                quote_payload = await self._maybe_enrich_quote(
                    symbol=row.symbol,
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
                    symbol=row.symbol,
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
        user_id_present: bool,
        enriched_count: int,
        quote_status_default: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if quote_status_default is not None:
            return dict(quote_status_default)
        if not user_id_present:
            return {
                "status": "unavailable",
                "unavailable_reason": (
                    "kis_live quote enrichment requires explicit user_id"
                ),
            }
        if self._kis_quote_client is None:
            return {
                "status": "unavailable",
                "unavailable_reason": "no KIS quote client configured",
            }
        if enriched_count >= self._quote_enrichment_limit:
            return {
                "status": "skipped",
                "unavailable_reason": (
                    f"quote enrichment cap ({self._quote_enrichment_limit}) reached"
                ),
            }
        try:
            raw = await self._kis_quote_client.fetch_quote_orderbook(symbol)
        except Exception as exc:  # noqa: BLE001 — optional, per-symbol fail-open
            return {
                "status": "unavailable",
                "unavailable_reason": f"kis_error: {type(exc).__name__}: {exc}",
            }
        if not isinstance(raw, dict):
            return {
                "status": "unavailable",
                "unavailable_reason": "kis returned non-dict quote payload",
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
            "venue": raw.get("venue") or "krx",
            "nxt_eligible": bool(raw.get("nxt_eligible")),
            "session": raw.get("session"),
            "as_of": raw.get("as_of"),
        }
        return quote
