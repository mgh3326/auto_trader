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

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.action_report.snapshot_backed.symbol_derivation import (
    SymbolDerivation,
    SymbolDerivationService,
    _DefaultLiveHoldingsRepo,
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


class _FakeLiveHoldingsRepo:
    """ROB-357 — read-only live-holdings source (e.g. Upbit balances)."""

    def __init__(self, symbols: list[str], *, raises: Exception | None = None) -> None:
        self.symbols = symbols
        self.raises = raises
        self.calls: list[str] = []

    async def list_held_symbols(self, *, market: str, user_id: int | None) -> list[str]:
        self.calls.append(market)
        if self.raises is not None:
            raise self.raises
        return list(self.symbols)


def _make_service(
    *,
    manual: list[str] | None = None,
    journal: list[str] | None = None,
    watch: list[str] | None = None,
    candidate: list[str] | None = None,
    live_holdings: _FakeLiveHoldingsRepo | list[str] | None = None,
    max_symbols: int = 50,
    top_held: int = 20,
    top_candidates: int = 20,
) -> SymbolDerivationService:
    if isinstance(live_holdings, list) or live_holdings is None:
        live_repo = _FakeLiveHoldingsRepo(live_holdings or [])
    else:
        live_repo = live_holdings
    return SymbolDerivationService(
        session=MagicMock(),
        manual_holdings_repo=_FakeManualHoldingsRepo(manual or []),
        journal_repo=_FakeJournalRepo(journal or []),
        watch_repo=_FakeWatchRepo(watch or []),
        candidate_repo=_FakeCandidateRepo(candidate or []),
        live_holdings_repo=live_repo,
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


# ---------------------------------------------------------------------------
# ROB-357 — crypto portfolio source must include live (Upbit) holdings, not
# just manual_holdings rows.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_crypto_portfolio_source_includes_live_holdings():
    service = _make_service(
        manual=["KRW-ETH", "KRW-SOL"],
        live_holdings=["KRW-BTC", "KRW-XRP", "KRW-LINK", "KRW-DOT"],
    )
    result = await service.derive(
        market="crypto", account_scope="upbit_live", user_id=42, seed_symbols=None
    )
    assert set(result.provenance["sources"]["portfolio"]) == {
        "KRW-ETH",
        "KRW-SOL",
        "KRW-BTC",
        "KRW-XRP",
        "KRW-LINK",
        "KRW-DOT",
    }
    assert {"KRW-BTC", "KRW-XRP", "KRW-LINK", "KRW-DOT"}.issubset(set(result.symbols))


@pytest.mark.asyncio
async def test_crypto_portfolio_dedups_manual_and_live_overlap():
    service = _make_service(
        manual=["KRW-ETH"],
        live_holdings=["KRW-ETH", "KRW-BTC"],
    )
    result = await service.derive(
        market="crypto", account_scope="upbit_live", user_id=42, seed_symbols=None
    )
    # KRW-ETH appears once despite being in both sources.
    assert result.provenance["sources"]["portfolio"].count("KRW-ETH") == 1
    assert set(result.provenance["sources"]["portfolio"]) == {"KRW-ETH", "KRW-BTC"}


@pytest.mark.asyncio
async def test_live_holdings_repo_not_consulted_for_kr():
    live = _FakeLiveHoldingsRepo(["KRW-BTC"])
    service = _make_service(manual=["005930"], live_holdings=live)
    await service.derive(
        market="kr", account_scope="kis_live", user_id=42, seed_symbols=None
    )
    # The crypto live-holdings source is crypto-only; KR uses the KIS-live
    # collector path, not this repo.
    assert live.calls == []


@pytest.mark.asyncio
async def test_live_holdings_error_is_soft_and_recorded():
    live = _FakeLiveHoldingsRepo([], raises=RuntimeError("upbit creds missing"))
    service = _make_service(manual=["KRW-ETH"], live_holdings=live)
    result = await service.derive(
        market="crypto", account_scope="upbit_live", user_id=42, seed_symbols=None
    )
    # Manual still present; derivation does not blow up.
    assert "KRW-ETH" in result.provenance["sources"]["portfolio"]
    assert "portfolio_live" in result.provenance.get("source_errors", {})


# ---------------------------------------------------------------------------
# ROB-357 — an empty candidate source must be explained, not silently blank.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_candidate_empty_reason_recorded_when_no_fresh_universe():
    service = _make_service(manual=["005930"], candidate=[])
    result = await service.derive(
        market="kr", account_scope="kis_live", user_id=42, seed_symbols=None
    )
    coverage = result.provenance["source_coverage"]
    assert coverage["candidate"]["count"] == 0
    assert coverage["candidate"]["empty_reason"] == "no_fresh_candidate_universe"


@pytest.mark.asyncio
async def test_candidate_non_empty_has_count_and_no_empty_reason():
    service = _make_service(manual=["005930"], candidate=["000660", "035420"])
    result = await service.derive(
        market="kr", account_scope="kis_live", user_id=42, seed_symbols=None
    )
    coverage = result.provenance["source_coverage"]
    assert coverage["candidate"]["count"] == 2
    assert "empty_reason" not in coverage["candidate"]


# ---------------------------------------------------------------------------
# ROB-357 — real default adapter path (Hermes-found blocker). UpbitHomeReader
# returns holdings on ``result.holdings`` (NOT ``result.account.holdings``);
# the adapter must read the right attribute or live holdings silently vanish.
# ---------------------------------------------------------------------------
class _FakeUpbitHomeReader:
    """Stand-in for UpbitHomeReader: fetch() returns ``.holdings`` directly."""

    last_user_id: int | None = None

    def __init__(self, session) -> None:  # noqa: ANN001 — test stub
        self._session = session

    async def fetch(self, *, user_id: int):
        type(self).last_user_id = user_id
        # Mixed shapes: bare currency, lowercase, already KRW-prefixed, blank.
        return SimpleNamespace(
            holdings=[
                SimpleNamespace(symbol="BTC"),
                SimpleNamespace(symbol="krw-eth"),
                SimpleNamespace(symbol="KRW-XRP"),
                SimpleNamespace(symbol="  link  "),
                SimpleNamespace(symbol=""),
                SimpleNamespace(symbol=None),
            ]
        )


@pytest.mark.asyncio
async def test_default_live_holdings_repo_reads_result_holdings(monkeypatch):
    monkeypatch.setattr(
        "app.services.invest_home_readers.UpbitHomeReader",
        _FakeUpbitHomeReader,
    )
    repo = _DefaultLiveHoldingsRepo(session=MagicMock())

    symbols = await repo.list_held_symbols(market="crypto", user_id=42)

    # Bare currency → KRW-prefixed; lowercase normalized; already-prefixed kept;
    # whitespace trimmed; empty/None skipped.
    assert symbols == ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-LINK"]
    assert _FakeUpbitHomeReader.last_user_id == 42


@pytest.mark.asyncio
async def test_default_live_holdings_repo_skips_non_crypto(monkeypatch):
    called = {"n": 0}

    class _ShouldNotFetch(_FakeUpbitHomeReader):
        async def fetch(self, *, user_id: int):
            called["n"] += 1
            return SimpleNamespace(holdings=[])

    monkeypatch.setattr(
        "app.services.invest_home_readers.UpbitHomeReader", _ShouldNotFetch
    )
    repo = _DefaultLiveHoldingsRepo(session=MagicMock())

    assert await repo.list_held_symbols(market="kr", user_id=42) == []
    assert called["n"] == 0
