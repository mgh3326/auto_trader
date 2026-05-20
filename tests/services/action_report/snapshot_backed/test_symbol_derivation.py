"""ROB-278 — Symbol derivation service tests.

The derivation service unions caller-supplied seed symbols with symbols
inferred from the user's portfolio, active trade journals, active watch
alerts, and the fresh candidate universe. It enforces a max-symbols cap
and records per-source provenance.

Lockdown policy:
- Seed (request-supplied) symbols are preserved — never dropped.
- Derived symbols are unioned with seed.
- Cap applies to the union; overflow records dropped_by_cap in provenance.
- Each emitted symbol is attributed to one or more sources.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.action_report.snapshot_backed.symbol_derivation import (
    SymbolDerivation,
    SymbolDerivationService,
)


# ---------------------------------------------------------------------------
# Fakes — keep tests focused on the derivation contract, not data sources.
# ---------------------------------------------------------------------------
class _FakeManualHoldingsRepo:
    def __init__(self, tickers: list[str]) -> None:
        self.tickers = tickers
        self.calls: list[str] = []

    async def list_tickers(self, *, market: str) -> list[str]:
        self.calls.append(market)
        return list(self.tickers)


class _FakeJournalRepo:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols

    async def list_active_journal_symbols(self, *, market: str) -> list[str]:
        return list(self.symbols)


class _FakeWatchRepo:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols

    async def list_active_watch_symbols(self, *, market: str) -> list[str]:
        return list(self.symbols)


class _FakeCandidateRepo:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols

    async def list_fresh_candidate_symbols(
        self, *, market: str, limit: int
    ) -> list[str]:
        return list(self.symbols[:limit])


def _make_service(
    *,
    manual: list[str] | None = None,
    journal: list[str] | None = None,
    watch: list[str] | None = None,
    candidate: list[str] | None = None,
    max_symbols: int = 50,
    top_held: int = 20,
    top_candidates: int = 20,
) -> SymbolDerivationService:
    return SymbolDerivationService(
        session=MagicMock(),
        manual_holdings_repo=_FakeManualHoldingsRepo(manual or []),
        journal_repo=_FakeJournalRepo(journal or []),
        watch_repo=_FakeWatchRepo(watch or []),
        candidate_repo=_FakeCandidateRepo(candidate or []),
        max_symbols=max_symbols,
        top_held=top_held,
        top_candidates=top_candidates,
    )


# ---------------------------------------------------------------------------
# Seed preservation + source attribution.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_seed_symbols_preserved_when_no_derived_sources():
    service = _make_service()
    result = await service.derive(
        market="kr", account_scope="kis_live", user_id=42, seed_symbols=["005930"]
    )
    assert isinstance(result, SymbolDerivation)
    assert result.symbols == ["005930"]
    assert result.provenance["sources"]["seed"] == ["005930"]
    assert result.provenance["dropped_by_cap"] == []


@pytest.mark.asyncio
async def test_seed_unioned_with_derived_sources():
    service = _make_service(
        manual=["005930", "000660"],
        journal=["035420"],
        watch=["005380"],
        candidate=["028260"],
    )
    result = await service.derive(
        market="kr", account_scope="kis_live", user_id=42, seed_symbols=["096770"]
    )
    assert set(result.symbols) == {
        "005930",
        "000660",
        "035420",
        "005380",
        "028260",
        "096770",
    }
    sources = result.provenance["sources"]
    assert sources["seed"] == ["096770"]
    assert set(sources["portfolio"]) == {"005930", "000660"}
    assert sources["journal"] == ["035420"]
    assert sources["watch"] == ["005380"]
    assert sources["candidate"] == ["028260"]


@pytest.mark.asyncio
async def test_derived_when_seed_is_none():
    service = _make_service(manual=["005930"], watch=["035420"])
    result = await service.derive(
        market="kr", account_scope="kis_live", user_id=42, seed_symbols=None
    )
    assert set(result.symbols) == {"005930", "035420"}
    assert result.provenance["sources"]["seed"] == []


# ---------------------------------------------------------------------------
# Caps — seed always survives, overflow recorded.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_max_symbols_cap_records_dropped():
    # 60 candidate symbols, cap 50 → 10 dropped.
    candidates = [f"C{i:04d}" for i in range(60)]
    service = _make_service(candidate=candidates, max_symbols=50, top_candidates=60)
    result = await service.derive(
        market="kr", account_scope="kis_live", user_id=42, seed_symbols=None
    )
    assert len(result.symbols) == 50
    assert len(result.provenance["dropped_by_cap"]) == 10
    # No overlap between final symbols and dropped ones.
    assert set(result.symbols).isdisjoint(set(result.provenance["dropped_by_cap"]))
    assert result.provenance["cap"] == 50


@pytest.mark.asyncio
async def test_seed_never_dropped_even_when_over_cap():
    """Operator-supplied seed must survive the cap (drop derived instead)."""
    seeds = [f"S{i:04d}" for i in range(40)]
    candidates = [f"C{i:04d}" for i in range(40)]
    service = _make_service(candidate=candidates, max_symbols=50, top_candidates=40)
    result = await service.derive(
        market="kr",
        account_scope="kis_live",
        user_id=42,
        seed_symbols=seeds,
    )
    # All 40 seeds present.
    assert set(seeds).issubset(set(result.symbols))
    # Only 10 of 40 candidates fit alongside seeds.
    assert len(result.symbols) == 50
    dropped = set(result.provenance["dropped_by_cap"])
    assert dropped.issubset(set(candidates))
    # No seed got dropped.
    assert dropped.isdisjoint(set(seeds))


# ---------------------------------------------------------------------------
# Per-source bounding — candidate source is capped via ``top_candidates``.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_top_candidates_bound_is_passed_to_repo():
    repo = _FakeCandidateRepo([f"C{i:04d}" for i in range(100)])
    service = SymbolDerivationService(
        session=MagicMock(),
        manual_holdings_repo=_FakeManualHoldingsRepo([]),
        journal_repo=_FakeJournalRepo([]),
        watch_repo=_FakeWatchRepo([]),
        candidate_repo=repo,
        max_symbols=50,
        top_held=20,
        top_candidates=15,
    )
    result = await service.derive(
        market="kr", account_scope="kis_live", user_id=42, seed_symbols=None
    )
    # Only top 15 candidates requested.
    assert len(result.symbols) == 15
    assert result.provenance["sources"]["candidate"] == [f"C{i:04d}" for i in range(15)]


# ---------------------------------------------------------------------------
# Idempotency — duplicate symbols across sources collapse to single attribution.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_duplicate_symbols_across_sources_dedup_with_multi_attribution():
    """A symbol present in two sources counts once in the union but appears
    in both source lists for audit."""
    service = _make_service(
        manual=["005930"], journal=["005930"], watch=[], candidate=[]
    )
    result = await service.derive(
        market="kr", account_scope="kis_live", user_id=42, seed_symbols=None
    )
    assert result.symbols == ["005930"]
    assert result.provenance["sources"]["portfolio"] == ["005930"]
    assert result.provenance["sources"]["journal"] == ["005930"]


@pytest.mark.asyncio
async def test_provenance_exposes_cap_and_counts():
    service = _make_service(manual=["A"], journal=["B"], watch=["C"], candidate=["D"])
    result = await service.derive(
        market="kr", account_scope="kis_live", user_id=42, seed_symbols=["E"]
    )
    assert result.provenance["cap"] == 50
    assert "sources" in result.provenance
    assert "dropped_by_cap" in result.provenance
    assert result.provenance["total_unique"] == 5
