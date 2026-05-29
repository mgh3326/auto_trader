"""ROB-278 — Symbol derivation for snapshot-backed reports.

Derives a bounded list of relevant symbols for a report by unioning
caller-supplied seed symbols with symbols inferred from:

* the user's portfolio (manual_holdings for the requested market)
* active trade journals
* active watch alerts
* the fresh candidate universe

Contract:

* Seed (caller-supplied) symbols are preserved verbatim and never dropped.
* Derived symbols are unioned with seed and capped at ``max_symbols``.
* Per-source attribution and overflow (``dropped_by_cap``) are returned in
  ``provenance`` so the report viewer / audit can show why each symbol is
  in scope.

This module is read-only by construction — it does not import any broker
mutation or watch-activation surface. Tests in ``test_collectors.py``
import-guard the module.
"""

from __future__ import annotations

from typing import Any, Protocol

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.manual_holdings import ManualHolding, MarketType
from app.models.trade_journal import TradeJournal
from app.services.investment_reports.repository import InvestmentReportsRepository

_MARKET_TO_TYPES: dict[str, tuple[MarketType, ...]] = {
    "kr": (MarketType.KR,),
    "us": (MarketType.US,),
    "crypto": (MarketType.CRYPTO,),
}


def _normalize_crypto_symbol(raw: object) -> str | None:
    """Normalize a crypto symbol to the pipeline's ``KRW-XXX`` market code.

    ``UpbitHomeReader`` emits the bare currency (e.g. ``"BTC"``) while
    ``manual_holdings`` already store the KRW market code. Trim/uppercase,
    prefix bare currencies with ``KRW-``, keep already-prefixed codes, and
    drop blanks so the portfolio source unions cleanly.
    """
    text = str(raw or "").strip().upper()
    if not text:
        return None
    if text.startswith("KRW-"):
        return text
    return f"KRW-{text}"


class SymbolDerivation(BaseModel):
    """Result of a single derivation pass."""

    model_config = ConfigDict(extra="forbid")

    symbols: list[str]
    provenance: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Repository protocols — keep the service decoupled from concrete data sources
# so tests can substitute fakes that don't touch the DB.
# ---------------------------------------------------------------------------
class _ManualHoldingsRepoProtocol(Protocol):
    async def list_tickers(self, *, market: str) -> list[str]: ...


class _JournalRepoProtocol(Protocol):
    async def list_active_journal_symbols(self, *, market: str) -> list[str]: ...


class _WatchRepoProtocol(Protocol):
    async def list_active_watch_symbols(self, *, market: str) -> list[str]: ...


class _CandidateRepoProtocol(Protocol):
    async def list_fresh_candidate_symbols(
        self, *, market: str, limit: int
    ) -> list[str]: ...


class _LiveHoldingsRepoProtocol(Protocol):
    # ROB-357 — live (broker-held) positions for markets whose holdings do
    # not live in ``manual_holdings`` (crypto/Upbit). Read-only.
    async def list_held_symbols(
        self, *, market: str, user_id: int | None
    ) -> list[str]: ...


# ---------------------------------------------------------------------------
# Default DB-backed repository adapters. Each is a narrow read-only wrapper
# around an existing model so the derivation service does no broker I/O.
# ---------------------------------------------------------------------------
class _DefaultManualHoldingsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_tickers(self, *, market: str) -> list[str]:
        market_types = _MARKET_TO_TYPES.get(market)
        if not market_types:
            return []
        stmt = sa.select(ManualHolding.ticker).where(
            ManualHolding.market_type.in_(market_types)
        )
        result = await self._session.execute(stmt)
        return [t for (t,) in result.all()]


class _DefaultJournalRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active_journal_symbols(self, *, market: str) -> list[str]:
        # TradeJournal model has no market column; we filter by instrument_type
        # downstream if needed. For PR1 derivation, we include all active
        # journals because they are operator-curated and rare.
        stmt = (
            sa.select(TradeJournal.symbol)
            .where(TradeJournal.status == "active")
            .distinct()
        )
        result = await self._session.execute(stmt)
        return [s for (s,) in result.all()]


class _DefaultWatchRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = InvestmentReportsRepository(session)

    async def list_active_watch_symbols(self, *, market: str) -> list[str]:
        alerts = await self._repo.list_active_alerts(market=market)
        return [a.symbol for a in alerts if a.symbol]


class _DefaultLiveHoldingsRepo:
    """ROB-357 — read-only live-holdings adapter.

    Crypto holdings live on the exchange (Upbit), not in ``manual_holdings``,
    so the symbol scope previously omitted them. We read them through the
    sanctioned read-only ``UpbitHomeReader`` (the same abstraction the
    portfolio collector uses for KIS), never the low-level broker client, and
    fail soft: any error propagates to the service's best-effort ``_safe``
    wrapper which records it under ``source_errors`` and returns ``[]``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_held_symbols(self, *, market: str, user_id: int | None) -> list[str]:
        if market != "crypto":
            return []
        from app.services.invest_home_readers import UpbitHomeReader

        reader = UpbitHomeReader(self._session)
        result = await reader.fetch(user_id=user_id or 0)
        # ``UpbitHomeReader.fetch`` returns a ``_SourceFetchResult`` whose
        # positions live on ``result.holdings`` (a flat ``list[Holding]``) —
        # there is no ``result.account``. Reading the wrong attribute silently
        # drops every live holding (ROB-357 Hermes review).
        holdings = getattr(result, "holdings", None) or []
        symbols: list[str] = []
        seen: set[str] = set()
        for holding in holdings:
            normalized = _normalize_crypto_symbol(getattr(holding, "symbol", None))
            if normalized is None or normalized in seen:
                continue
            seen.add(normalized)
            symbols.append(normalized)
        return symbols


class _DefaultCandidateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_fresh_candidate_symbols(
        self, *, market: str, limit: int
    ) -> list[str]:
        # Order by snapshot_date desc to get the freshest rows, then
        # dedupe symbols in Python. PostgreSQL forbids ``DISTINCT`` over
        # SELECT columns combined with ``ORDER BY`` on columns that are
        # NOT in the select list, so we fetch ``symbol`` + ``snapshot_date``
        # and collapse client-side. Overfetch slightly to absorb dupes.
        overfetch = max(limit * 4, limit)
        stmt = (
            sa.select(
                InvestScreenerSnapshot.symbol, InvestScreenerSnapshot.snapshot_date
            )
            .where(InvestScreenerSnapshot.market == market)
            .order_by(
                InvestScreenerSnapshot.snapshot_date.desc(),
                InvestScreenerSnapshot.symbol.asc(),
            )
            .limit(overfetch)
        )
        result = await self._session.execute(stmt)
        out: list[str] = []
        seen: set[str] = set()
        for symbol, _snapshot_date in result.all():
            if symbol in seen:
                continue
            seen.add(symbol)
            out.append(symbol)
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class SymbolDerivationService:
    """Derive a bounded symbol scope for a snapshot-backed report.

    Construction is dependency-injected so production wiring uses the
    DB-backed repos while tests pass in fakes. The service never reaches
    outside its injected repositories.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        manual_holdings_repo: _ManualHoldingsRepoProtocol | None = None,
        journal_repo: _JournalRepoProtocol | None = None,
        watch_repo: _WatchRepoProtocol | None = None,
        candidate_repo: _CandidateRepoProtocol | None = None,
        live_holdings_repo: _LiveHoldingsRepoProtocol | None = None,
        max_symbols: int = 50,
        top_held: int = 20,
        top_candidates: int = 20,
    ) -> None:
        self._manual = manual_holdings_repo or _DefaultManualHoldingsRepo(session)
        self._journal = journal_repo or _DefaultJournalRepo(session)
        self._watch = watch_repo or _DefaultWatchRepo(session)
        self._candidate = candidate_repo or _DefaultCandidateRepo(session)
        self._live = live_holdings_repo or _DefaultLiveHoldingsRepo(session)
        self._max_symbols = max_symbols
        self._top_held = top_held
        self._top_candidates = top_candidates

    async def derive(
        self,
        *,
        market: str,
        account_scope: str | None,
        user_id: int | None,
        seed_symbols: list[str] | None,
    ) -> SymbolDerivation:
        seed = [s for s in (seed_symbols or []) if s]
        source_errors: dict[str, str] = {}

        # Each per-source DB query runs inside a SAVEPOINT (when the
        # outer session supports one) so a query failure — e.g., a SQL
        # bug or a missing column — doesn't leave the outer transaction
        # in an aborted state. The outer caller's subsequent writes,
        # and the stage-runner / generator pipeline that follows, must
        # remain usable even if one optional derivation source raises.
        outer_session = getattr(self._manual, "_session", None) or getattr(
            self._candidate, "_session", None
        )
        can_savepoint = hasattr(outer_session, "begin_nested")

        async def _safe(name: str, coro):
            if not can_savepoint:
                try:
                    return await coro
                except Exception as exc:  # noqa: BLE001 — derivation is best-effort
                    source_errors[name] = f"{type(exc).__name__}: {exc}"
                    return []
            try:
                async with outer_session.begin_nested():
                    return await coro
            except Exception as exc:  # noqa: BLE001 — derivation is best-effort
                source_errors[name] = f"{type(exc).__name__}: {exc}"
                return []

        # Portfolio = manual_holdings ∪ live broker holdings. For crypto the
        # held positions live on Upbit (not manual_holdings), so we union in
        # the read-only live source; manual rows still come first for KR/US.
        manual_portfolio = await _safe(
            "portfolio", self._manual.list_tickers(market=market)
        )
        live_portfolio: list[str] = []
        if market == "crypto":
            live_portfolio = await _safe(
                "portfolio_live",
                self._live.list_held_symbols(market=market, user_id=user_id),
            )
        portfolio: list[str] = []
        _portfolio_seen: set[str] = set()
        for sym in [*manual_portfolio, *live_portfolio]:
            if sym in _portfolio_seen:
                continue
            _portfolio_seen.add(sym)
            portfolio.append(sym)
        portfolio = portfolio[: self._top_held]
        journal = await _safe(
            "journal", self._journal.list_active_journal_symbols(market=market)
        )
        watch = await _safe(
            "watch", self._watch.list_active_watch_symbols(market=market)
        )
        candidate = await _safe(
            "candidate",
            self._candidate.list_fresh_candidate_symbols(
                market=market, limit=self._top_candidates
            ),
        )

        sources = {
            "seed": seed,
            "portfolio": portfolio,
            "journal": journal,
            "watch": watch,
            "candidate": candidate,
        }

        # Seed first, then derived sources in priority order so the cap
        # drops candidate-derived symbols before more important sources.
        ordered_sources: list[list[str]] = [
            seed,
            portfolio,
            journal,
            watch,
            candidate,
        ]
        union: list[str] = []
        seen: set[str] = set()
        for source in ordered_sources:
            for sym in source:
                if sym in seen:
                    continue
                seen.add(sym)
                union.append(sym)

        seed_set = set(seed)
        if len(union) <= self._max_symbols:
            kept = union
            dropped: list[str] = []
        else:
            # Seeds always survive the cap; truncate from the derived tail.
            seeds_in_union = [s for s in union if s in seed_set]
            derived = [s for s in union if s not in seed_set]
            remaining_room = max(self._max_symbols - len(seeds_in_union), 0)
            kept_derived = derived[:remaining_room]
            dropped = derived[remaining_room:]
            kept = seeds_in_union + kept_derived

        # ROB-357 — per-source coverage so an empty source (especially the
        # candidate universe) is explained rather than silently blank.
        source_coverage: dict[str, Any] = {}
        for name, syms in sources.items():
            entry: dict[str, Any] = {"count": len(syms)}
            if name in source_errors:
                entry["error"] = source_errors[name]
            source_coverage[name] = entry
        if not candidate:
            source_coverage["candidate"]["empty_reason"] = (
                "fetch_error"
                if "candidate" in source_errors
                else "no_fresh_candidate_universe"
            )

        provenance: dict[str, Any] = {
            "sources": sources,
            "source_coverage": source_coverage,
            "dropped_by_cap": dropped,
            "cap": self._max_symbols,
            "total_unique": len(union),
        }
        if source_errors:
            provenance["source_errors"] = source_errors
        return SymbolDerivation(symbols=kept, provenance=provenance)
