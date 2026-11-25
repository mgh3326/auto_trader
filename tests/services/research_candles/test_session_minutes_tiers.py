"""hk 6299: tiered budgets for the session-minute collector.

Order: fixed (holdings + fixed list, never cut) > intraday promotion (hk 6154 KR
first captures, hk 6510 US first captures) > pre-open screener snapshot > pick log
remainder, under one run cap. Unused budget spills to the next tier; symbols cut by a tier are reported
with that tier and the closed reason ``budget_cut``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.services.invest_momentum_events import intraday
from app.services.research_candles import session_minutes_collector as sc
from app.services.research_candles.session_minutes_collector import TierSpec

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 25, 6, 10, tzinfo=UTC)  # 15:10 KST
CAPTURED_0905 = datetime(2026, 11, 25, 0, 5, 11, tzinfo=UTC)  # 09:05:11 KST


def _syms(start: int, count: int) -> list[str]:
    return [f"{start + i:06d}" for i in range(count)]


def _mover(symbol: str, rank: int, captured=CAPTURED_0905):
    return SimpleNamespace(
        symbol=symbol,
        name=None,
        market="kr",
        surface="domestic_market_stock_default",
        snapshot_at=captured,
        trade_type="KRX",
        market_type="ALL",
        order_type="up",
        rank=rank,
        change_rate=None,
        trade_value=None,
    )


class _Result:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _Session:
    """Dispatches by statement text: first captures, minute rows today, pick log."""

    def __init__(self, pick=(), movers=(), have_today=(), us_earliest=()):
        self.pick = list(pick)
        self.movers = list(movers)
        self.have_today = list(have_today)
        self.us_earliest = list(us_earliest)
        self.statements: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        text = str(statement)
        self.statements.append(text)
        self.calls.append((text, params or {}))
        if "invest_momentum_event_snapshots" in text:
            return _Result(self.movers)
        if "kr_candles_1m" in text:
            return _Result(self.have_today)
        if "us_candles_1m" in text:
            return _Result(self.us_earliest)
        return _Result(self.pick)

    async def rollback(self):
        pass


class _Kis:
    def __init__(self, kr=(), us=()):
        self.kr = list(kr)
        self.us = list(us)

    async def fetch_my_stocks(self):
        return [{"pdno": s} for s in self.kr]

    async def fetch_my_us_stocks(self):
        return [{"ovrs_pdno": s} for s in self.us]


@pytest.fixture(autouse=True)
def _no_manual_holdings(monkeypatch):
    import app.services.manual_holdings_service as manual_module

    class _Manual:
        def __init__(self, session):
            pass

        async def get_holdings_by_user(self, user_id, market_type=None):
            return []

    monkeypatch.setattr(manual_module, "ManualHoldingsService", _Manual)


async def _targets(
    *,
    session,
    kis,
    market="kr",
    fixed="069500,229200",
    screener=(),
    screener_top_n=80,
    max_symbols=150,
    **kwargs,
):
    async def snapshot(**call):
        rows = [{"symbol": s, "rank": i} for i, s in enumerate(screener, start=1)]
        return {"results": rows[: call["limit"]]}

    params = {
        "market": market,
        "session": session,
        "kis": kis,
        "user_id": 1,
        "fixed_symbols_raw": fixed,
        "now": NOW,
        "screener_top_n": screener_top_n,
        "max_symbols": max_symbols,
        "snapshot_fn": snapshot,
        "presets_fn": lambda _market: [
            SimpleNamespace(id="alpha", availability="active")
        ],
    }
    params.update(kwargs)
    return await sc.build_targets(**params)


def _tier(targets, name):
    return next(t for t in targets.tiers if t.tier == name)


# ------------------------------------------------- pure selector (select_tiered)


def test_budgeted_tier_takes_its_budget_in_order_and_cuts_the_rest() -> None:
    selection = sc.select_tiered(
        [TierSpec("a", [1, 2, 3, 4, 5], budget=3), TierSpec("b", [6, 7], budget=None)],
        10,
    )
    assert selection.selected == [1, 2, 3, 6, 7]
    assert selection.cuts == [
        {"symbol": 4, "tier": "a", "reason": "budget_cut"},
        {"symbol": 5, "tier": "a", "reason": "budget_cut"},
    ]
    assert selection.truncated == {"a": 2}


def test_unused_budget_carries_to_the_next_tier() -> None:
    selection = sc.select_tiered(
        [TierSpec("a", [1], budget=3), TierSpec("b", [2, 3, 4, 5], budget=1)], 10
    )
    # a takes 1 of 3 -> 2 spill to b, which allows 1 + 2 = 3
    assert selection.selected == [1, 2, 3, 4]
    assert [t.allowance for t in selection.tiers] == [3, 3]
    assert selection.cuts == [{"symbol": 5, "tier": "b", "reason": "budget_cut"}]


def test_exempt_tier_is_never_cut_but_consumes_the_run_cap() -> None:
    selection = sc.select_tiered(
        [
            TierSpec("fixed", [1, 2, 3, 4], budget=2, exempt=True),
            TierSpec("b", [5, 6], budget=None),
        ],
        3,
    )
    assert selection.selected == [1, 2, 3, 4]
    assert selection.cuts == [
        {"symbol": 5, "tier": "b", "reason": "budget_cut"},
        {"symbol": 6, "tier": "b", "reason": "budget_cut"},
    ]
    fixed = selection.tiers[0]
    assert (fixed.budget, fixed.allowance, fixed.selected, fixed.cut) == (2, None, 4, 0)


def test_protected_symbols_are_kept_past_the_budget_but_not_the_cap() -> None:
    tiers = [TierSpec("m", [1, 2, 3, 4], budget=1, protected=frozenset({1, 2}))]
    assert sc.select_tiered(tiers, 10).selected == [1, 2]
    assert sc.select_tiered(tiers, 1).selected == [1]  # the cap still binds


def test_a_symbol_cut_by_one_tier_stays_eligible_for_a_lower_tier() -> None:
    selection = sc.select_tiered(
        [TierSpec("a", [1, 2], budget=1), TierSpec("b", [2], budget=None)], 10
    )
    assert selection.selected == [1, 2]
    assert selection.cuts == []
    assert selection.truncated == {}


def test_select_with_cap_is_still_cap_only() -> None:
    kept, truncated = sc.select_with_cap([("x", [1, 2, 3]), ("y", [4])], 2)
    assert kept == [1, 2] and truncated == {"x": 1, "y": 1}


# --------------------------------------------- build_targets (KR and US tiers)


@pytest.mark.asyncio
async def test_resembling_11_25_keeps_the_benchmarks_and_early_movers_and_cuts_by_rank() -> (
    None
):
    """Acceptance 1: 48 holdings + 2 benchmarks fill the fixed budget; 45 movers
    captured in one 09:05 run (005380 and 307950 first); 80 screener candidates; 30
    pick-log symbols; cap 150."""
    holdings = _syms(200000, 48)
    mover_symbols = ["005380", "307950", *_syms(310000, 43)]
    movers = [_mover(s, rank=i + 1) for i, s in enumerate(mover_symbols)]
    screener = _syms(100000, 80)
    pick = _syms(400000, 30)
    targets = await _targets(
        session=_Session(pick=pick, movers=movers),
        kis=_Kis(kr=holdings),
        screener=screener,
    )
    for symbol in ("005380", "307950", "069500", "229200"):
        assert symbol in targets.symbols, symbol
    assert len(targets.symbols) == 150
    # movers: budget 40 (fixed used exactly 50, no spill); ranks 41-45 are cut
    cut_movers = [c["symbol"] for c in targets.cuts if c["tier"] == "intraday_movers"]
    assert cut_movers == mover_symbols[40:]
    assert targets.truncated == {
        "intraday_movers": 5,
        "screener_snapshot": 20,
        "screener_pick_log": 30,
    }
    assert _tier(targets, "intraday_movers").selected == 40
    assert _tier(targets, "screener_snapshot").selected == 60
    assert _tier(targets, "screener_pick_log").selected == 0


@pytest.mark.asyncio
async def test_unused_fixed_budget_spills_to_the_intraday_tier() -> None:
    """Acceptance 1 (spill): the fixed tier uses 2 of 50, so 48 spill to movers."""
    movers = [_mover(s, rank=i + 1) for i, s in enumerate(_syms(310000, 60))]
    targets = await _targets(session=_Session(movers=movers), kis=_Kis())
    tier = _tier(targets, "intraday_movers")
    assert (tier.budget, tier.allowance, tier.selected, tier.cut) == (40, 88, 60, 0)
    assert "intraday_movers" not in targets.truncated


@pytest.mark.asyncio
async def test_holdings_are_never_cut_even_when_they_exceed_the_fixed_budget() -> None:
    """Acceptance 2: 60 holdings exceed the fixed budget of 50 and even a cap of 20."""
    holdings = _syms(200000, 60)
    movers = [_mover(s, rank=i + 1) for i, s in enumerate(_syms(310000, 10))]
    targets = await _targets(
        session=_Session(movers=movers),
        kis=_Kis(kr=holdings),
        max_symbols=20,
    )
    assert set(holdings) <= set(targets.symbols)
    assert "069500" in targets.symbols and "229200" in targets.symbols
    assert all(c["symbol"] not in holdings for c in targets.cuts)
    assert _tier(targets, "fixed").cut == 0
    # the exempt tier already used the whole cap: every mover is cut, nothing else
    # is listed (the screener and pick log have no candidates in this fixture)
    assert {c["tier"] for c in targets.cuts} == {"intraday_movers"}
    assert len(targets.cuts) == 10


@pytest.mark.asyncio
async def test_a_mover_with_rows_today_is_not_demoted_by_the_budget() -> None:
    """Stickiness: rank 45 is already collected today, so it keeps its place."""
    mover_symbols = _syms(310000, 45)
    movers = [_mover(s, rank=i + 1) for i, s in enumerate(mover_symbols)]
    sticky = mover_symbols[44]
    targets = await _targets(
        session=_Session(movers=movers, have_today=[sticky]),
        kis=_Kis(kr=_syms(200000, 48)),
    )
    assert sticky in targets.symbols
    cut_movers = [c["symbol"] for c in targets.cuts if c["tier"] == "intraday_movers"]
    assert cut_movers == mover_symbols[39:44]  # ranks 40-44 go instead
    assert sticky not in targets.backfill  # it already has rows: no earlier page


@pytest.mark.asyncio
async def test_a_holding_that_is_also_a_protected_mover_does_not_raise_the_budget() -> (
    None
):
    """A protected symbol the fixed tier already took is not an intraday candidate,
    so it must not lift the intraday allowance (it would ratchet run after run)."""
    holdings = _syms(200000, 50)  # fixed tier = 50 exactly, no spill
    held_movers = holdings[:5]  # 5 holdings that are also movers with rows today
    protected = _syms(310000, 40)  # 40 other movers already collected today
    unprotected = _syms(311000, 10)  # 10 movers with no rows yet
    ordered = [*held_movers, *protected, *unprotected]
    movers = [_mover(s, rank=i + 1) for i, s in enumerate(ordered)]
    targets = await _targets(
        session=_Session(movers=movers, have_today=[*held_movers, *protected]),
        kis=_Kis(kr=holdings),
        fixed="",
    )
    tier = _tier(targets, "intraday_movers")
    assert (tier.budget, tier.allowance, tier.selected, tier.cut) == (40, 40, 40, 10)
    assert set(protected) <= set(targets.symbols)
    assert not set(unprotected) & set(targets.symbols)
    assert sorted(c["symbol"] for c in targets.cuts) == sorted(unprotected)


@pytest.mark.asyncio
async def test_promotion_order_is_earliest_capture_then_best_rank() -> None:
    later = datetime(2026, 11, 25, 0, 10, tzinfo=UTC)
    movers = [
        _mover("000001", rank=1, captured=later),  # best rank, but captured later
        _mover("000002", rank=5, captured=CAPTURED_0905),
        _mover("000003", rank=2, captured=CAPTURED_0905),
    ]
    targets = await _targets(
        session=_Session(movers=movers),
        kis=_Kis(),
        fixed="",
        max_symbols=2,
        intraday_budget=2,
    )
    assert targets.symbols == ["000002", "000003"]
    assert targets.cuts == [
        {"symbol": "000001", "tier": "intraday_movers", "reason": "budget_cut"}
    ]


@pytest.mark.asyncio
async def test_us_selects_the_fixed_defaults_and_its_own_movers_tier(
    monkeypatch,
) -> None:
    """hk 6510 acceptance 3: US keeps its fixed defaults and gains the movers tier.

    The first-capture read is US-scoped (``market='us'``, ET trading day), so a KR
    capture never promotes a US symbol.
    """
    calls: list[tuple[str, object]] = []

    async def _captures(session, trading_date, market="kr"):
        calls.append((market, trading_date))
        return [_mover("DELL", rank=1)]

    monkeypatch.setattr(intraday, "first_captures", _captures)
    session = _Session(pick=["AAPL"])
    targets = await _targets(
        market="us",
        session=session,
        kis=_Kis(us=[]),
        fixed="SPY,QQQ,SMH",
        screener=["MSFT"],
    )
    assert {"SPY", "QQQ", "SMH"} <= set(targets.symbols)
    assert "intraday_movers" in [t.tier for t in targets.tiers]
    assert targets.by_origin["intraday_movers"] == 1
    assert "DELL" in targets.symbols
    # US-scoped first captures on the ET trading day: NOW is 2026-11-25 06:10 UTC
    # = 01:10 ET on the 25th (the ET calendar date of that instant).
    assert calls == [("us", NOW.astimezone(ZoneInfo("America/New_York")).date())]


@pytest.mark.asyncio
async def test_us_backfill_retries_until_the_earliest_bar_reaches_the_session_start(
    monkeypatch,
) -> None:
    """hk 6510 r2: a failed earlier page must stay eligible for the next run.

    US backfill completion is the earliest bar of today vs 04:00 ET, not row
    presence: a mover that only has the newest page (09:30 ET) is retried, one
    whose earliest bar is the session start is done.
    """

    async def _captures(session, trading_date, market="kr"):
        return [_mover("DELL", rank=1), _mover("URBN", rank=2)]

    monkeypatch.setattr(intraday, "first_captures", _captures)
    # NOW is 2026-11-25 06:10 UTC = 01:10 ET on the 25th, so that ET day's open is
    # 04:00 ET = 09:00 UTC on the 25th.
    session_start_utc = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
    session = _Session(
        us_earliest=[
            ("DELL", session_start_utc),  # paged back to the open: done
            ("URBN", datetime(2026, 11, 25, 14, 30, tzinfo=UTC)),  # 09:30 ET only
        ]
    )
    targets = await _targets(
        market="us", session=session, kis=_Kis(us=[]), fixed="SPY", screener=[]
    )
    assert set(targets.symbols) == {"SPY", "DELL", "URBN"}
    assert targets.backfill == ["URBN"]


@pytest.mark.asyncio
async def test_report_lists_every_cut_with_tier_and_reason() -> None:
    """Acceptance 3 (collector side): the run report carries tier counts and cuts."""
    movers = [_mover(s, rank=i + 1) for i, s in enumerate(_syms(310000, 45))]
    targets = await _targets(
        session=_Session(movers=movers), kis=_Kis(kr=_syms(200000, 48))
    )
    report = sc.targets_report(targets)
    assert report["targets_truncated_total"] == sum(
        report["targets_truncated"].values()
    )
    assert len(report["targets_cuts"]) == report["targets_truncated_total"] == 5
    assert all(
        set(c) == {"symbol", "tier", "reason"} and c["reason"] == "budget_cut"
        for c in report["targets_cuts"]
    )
    tiers = {t["tier"]: t for t in report["targets_tiers"]}
    assert set(tiers) == {
        "fixed",
        "intraday_movers",
        "screener_snapshot",
        "screener_pick_log",
    }
    assert tiers["intraday_movers"] == {
        "tier": "intraday_movers",
        "budget": 40,
        "allowance": 40,
        "candidates": 45,
        "selected": 40,
        "cut": 5,
    }
    assert report["targeted_symbols"] == targets.symbols


def test_settings_defaults_are_the_benchmarks_and_the_tier_budgets() -> None:
    fields = Settings.model_fields
    assert fields["SESSION_MINUTES_FIXED_SYMBOLS_KR"].default == "069500,229200"
    assert fields["SESSION_MINUTES_FIXED_SYMBOLS_US"].default == "SPY,QQQ,SMH"
    assert fields["SESSION_MINUTES_BUDGET_FIXED"].default == 50
    assert fields["SESSION_MINUTES_BUDGET_INTRADAY"].default == 40
    assert fields["SESSION_MINUTES_BUDGET_SCREENER"].default == 60
    assert fields["SESSION_MINUTES_MAX_SYMBOLS_KR"].default == 150


def test_collector_cli_module_imports_and_keeps_its_arguments() -> None:
    from scripts import collect_session_minutes as cli

    args = cli._parse_args(["--market", "kr", "--dry-run"])
    assert args.market == "kr" and args.dry_run is True and args.symbols is None
