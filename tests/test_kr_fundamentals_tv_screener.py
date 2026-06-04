"""ROB-428 PR-B — read-path loader for tvscreener-backed KR fundamentals presets.

These tests drive ``load_kr_fundamentals_preset_from_tv_snapshot`` against the new
``invest_kr_fundamentals_snapshots`` table (PR-A). The loader replaces the DART
``load_fundamentals_preset_from_snapshots`` on the KR display read-path for the 7
``FUNDAMENTALS_PRESET_SPECS`` presets, filling price/change/volume/category/
market_cap + all metrics so result rows stop being empty ``-`` and the count gap
closes (cheap_value, undervalued_growth, ...).

Synthetic KR-shaped codes (``99xxxx``) keep us clear of any production-shaped rows
the full-suite cleanup fixtures may touch.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.models.invest_kr_fundamentals_snapshot import InvestKrFundamentalsSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.invest_view_model.fundamentals_screener import (
    CHEAP_VALUE_SPEC,
    FUTURE_DIVIDEND_KING_SPEC,
    GROWTH_EXPECTATION_TOSS_SPEC,
    HIGH_YIELD_VALUE_SPEC,
    PROFITABLE_COMPANY_SPEC,
    STABLE_GROWTH_SPEC,
    STEADY_DIVIDEND_SPEC,
    UNDERVALUED_BREAKOUT_SPEC,
    UNDERVALUED_GROWTH_SPEC,
)
from app.services.invest_view_model.kr_fundamentals_tv_screener import (
    EARNINGS_STREAK_SKIP_WARNING,
    load_kr_fundamentals_preset_from_tv_snapshot,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_SD = dt.date(2026, 6, 4)
_PREFIX = "9913"


def _now():
    # 2026-06-04 is a Thursday → a KR trading day; today_trading_date("kr") == _SD.
    return dt.datetime(2026, 6, 4, 6, 0, tzinfo=dt.UTC)


async def _cleanup(db_session) -> None:
    await db_session.execute(
        sa.delete(InvestKrFundamentalsSnapshot).where(
            InvestKrFundamentalsSnapshot.symbol.like(f"{_PREFIX}%")
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.like(f"{_PREFIX}%"))
    )
    await db_session.commit()


def _snap(symbol: str, **kw) -> InvestKrFundamentalsSnapshot:
    base = {
        "symbol": symbol,
        "snapshot_date": _SD,
        "name": symbol,  # snapshot name is the ticker for KR (must NOT be used)
        "source": "tvscreener_kr",
        "raw_payload": {},
    }
    base.update(kw)
    return InvestKrFundamentalsSnapshot(**base)


def _universe(symbol: str, name: str) -> KRSymbolUniverse:
    return KRSymbolUniverse(symbol=symbol, name=name, exchange="KOSPI", is_active=True)


async def _seed(db_session, snaps, universe) -> None:
    db_session.add_all(snaps)
    db_session.add_all(universe)
    await db_session.commit()


async def test_returns_none_when_session_none_or_non_kr():
    assert (
        await load_kr_fundamentals_preset_from_tv_snapshot(
            None, market="kr", spec=CHEAP_VALUE_SPEC
        )
        is None
    )


async def test_returns_none_when_market_not_kr(db_session):
    assert (
        await load_kr_fundamentals_preset_from_tv_snapshot(
            db_session, market="us", spec=CHEAP_VALUE_SPEC
        )
        is None
    )


async def test_profitable_company_includes_and_excludes_on_thresholds(db_session):
    await _cleanup(db_session)
    sym_pass = f"{_PREFIX}01"
    sym_low_roe = f"{_PREFIX}02"
    sym_low_margin = f"{_PREFIX}03"
    await _seed(
        db_session,
        [
            _snap(
                sym_pass,
                price=Decimal("10000"),
                change_rate=Decimal("1.5"),
                volume=Decimal("1234567"),
                market_cap=Decimal("9000000000000"),
                roe_ttm=Decimal("20"),
                gross_margin_ttm=Decimal("0.31"),
                sector="Technology",
                industry="Semiconductors",
            ),
            _snap(
                sym_low_roe,
                market_cap=Decimal("8000000000000"),
                roe_ttm=Decimal("10"),  # < 15 → excluded
                gross_margin_ttm=Decimal("0.40"),
            ),
            _snap(
                sym_low_margin,
                market_cap=Decimal("7000000000000"),
                roe_ttm=Decimal("25"),
                gross_margin_ttm=Decimal("0.10"),  # < 0.20 → excluded
            ),
        ],
        [
            _universe(sym_pass, "통과종목"),
            _universe(sym_low_roe, "낮은ROE"),
            _universe(sym_low_margin, "낮은마진"),
        ],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session,
        market="kr",
        spec=PROFITABLE_COMPANY_SPEC,
        limit=20,
        now=_now,
        # The shared test DB carries residual kr_symbol_universe rows; pin the
        # coverage denominator so this partition's health is deterministic and
        # the date-based freshness ("fresh" on the current trading date) is
        # exercised faithfully (mirrors high_yield_value's cap_degraded path).
        universe_count=3,
    )
    assert result is not None
    symbols = [r["symbol"] for r in result.rows]
    assert symbols == [sym_pass]
    row = result.rows[0]
    # filled row: name from universe (NOT snapshot ticker), category, price, volume, metrics
    assert row["name"] == "통과종목"
    assert row["name"] != sym_pass
    assert row["category"] == "Semiconductors"  # industry preferred over sector
    assert row["close"] == 10000.0
    assert row["change_rate"] == 1.5
    assert row["volume"] == 1234567.0
    assert row["market_cap"] == 9000000000000.0
    assert row["roe"] == 20.0
    assert row["gross_margin_ttm"] == 0.31
    assert row["_screener_snapshot_state"] == "fresh"
    assert row["snapshot_date"] == _SD


async def test_fail_closed_on_null_required_column(db_session):
    await _cleanup(db_session)
    sym_null = f"{_PREFIX}10"
    # roe_ttm is required by profitable_company; NULL must be EXCLUDED, never a pass.
    await _seed(
        db_session,
        [
            _snap(
                sym_null,
                market_cap=Decimal("5000000000000"),
                roe_ttm=None,
                gross_margin_ttm=Decimal("0.50"),
            )
        ],
        [_universe(sym_null, "널종목")],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session, market="kr", spec=PROFITABLE_COMPANY_SPEC, limit=20, now=_now
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == []
    assert any(e["symbol"] == sym_null for e in result.excluded)


async def test_cheap_value_per_pbr_must_be_positive(db_session):
    await _cleanup(db_session)
    sym_pass = f"{_PREFIX}20"
    sym_neg_per = f"{_PREFIX}21"
    await _seed(
        db_session,
        [
            _snap(
                sym_pass,
                market_cap=Decimal("6000000000000"),
                per=Decimal("12"),
                pbr=Decimal("1.0"),
                eps_yoy=Decimal("0.05"),  # cheap_value: min_earnings_growth_3y_avg=0
            ),
            _snap(
                sym_neg_per,
                market_cap=Decimal("5000000000000"),
                per=Decimal("-3"),  # per<=0 → excluded
                pbr=Decimal("0.9"),
                eps_yoy=Decimal("0.10"),
            ),
        ],
        [_universe(sym_pass, "싼가치주"), _universe(sym_neg_per, "적자기업")],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session, market="kr", spec=CHEAP_VALUE_SPEC, limit=20, now=_now
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == [sym_pass]


async def test_earnings_increase_streak_skipped_and_warned(db_session):
    """steady_dividend includes a symbol that lacks any earnings-streak signal —
    it must NOT be fail-closed on min_earnings_increase_streak_years (no tv column),
    and the result must surface the honest skip warning."""
    await _cleanup(db_session)
    sym = f"{_PREFIX}30"
    await _seed(
        db_session,
        [
            _snap(
                sym,
                market_cap=Decimal("4000000000000"),
                dividend_yield=Decimal("0.04"),  # >= 0.03
                payout_ratio_ttm=Decimal("40"),  # >= 30
                continuous_dividend_payout=Decimal("5"),  # >= 3
                # NOTE: no earnings-increase-streak column exists; must be skipped.
            )
        ],
        [_universe(sym, "꾸준배당")],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session, market="kr", spec=STEADY_DIVIDEND_SPEC, limit=20, now=_now
    )
    assert result is not None
    # Passes despite the un-applied streak condition.
    assert [r["symbol"] for r in result.rows] == [sym]
    assert EARNINGS_STREAK_SKIP_WARNING in result.warnings


async def test_steady_dividend_fail_closed_on_null_payout(db_session):
    await _cleanup(db_session)
    sym = f"{_PREFIX}31"
    await _seed(
        db_session,
        [
            _snap(
                sym,
                market_cap=Decimal("3000000000000"),
                dividend_yield=Decimal("0.05"),
                payout_ratio_ttm=None,  # required → fail-closed
                continuous_dividend_payout=Decimal("10"),
            )
        ],
        [_universe(sym, "배당미달")],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session, market="kr", spec=STEADY_DIVIDEND_SPEC, limit=20, now=_now
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == []


async def test_future_dividend_king_growth_streak_and_payout(db_session):
    await _cleanup(db_session)
    sym_pass = f"{_PREFIX}40"
    sym_low_streak = f"{_PREFIX}41"
    await _seed(
        db_session,
        [
            _snap(
                sym_pass,
                market_cap=Decimal("4000000000000"),
                dividend_yield=Decimal("0.02"),  # >= 0.01
                payout_ratio_ttm=Decimal("35"),  # >= 30
                continuous_dividend_growth=Decimal("5"),  # >= 3
            ),
            _snap(
                sym_low_streak,
                market_cap=Decimal("3500000000000"),
                dividend_yield=Decimal("0.02"),
                payout_ratio_ttm=Decimal("50"),
                continuous_dividend_growth=Decimal("1"),  # < 3 → excluded
            ),
        ],
        [_universe(sym_pass, "배당왕"), _universe(sym_low_streak, "짧은성장")],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session,
        market="kr",
        spec=FUTURE_DIVIDEND_KING_SPEC,
        limit=20,
        now=_now,
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == [sym_pass]
    # Still surfaces the streak-skip warning (it also has min_earnings_increase_streak).
    assert EARNINGS_STREAK_SKIP_WARNING in result.warnings


async def test_undervalued_growth_uses_yoy_proxy(db_session):
    await _cleanup(db_session)
    sym_pass = f"{_PREFIX}50"
    sym_low_eps = f"{_PREFIX}51"
    await _seed(
        db_session,
        [
            _snap(
                sym_pass,
                market_cap=Decimal("5000000000000"),
                per=Decimal("15"),  # <= 20
                revenue_yoy=Decimal("0.15"),  # >= 0.10
                eps_yoy=Decimal("0.30"),  # >= 0.20 (proxy for 3y-avg)
            ),
            _snap(
                sym_low_eps,
                market_cap=Decimal("4500000000000"),
                per=Decimal("12"),
                revenue_yoy=Decimal("0.20"),
                eps_yoy=Decimal("0.05"),  # < 0.20 → excluded
            ),
        ],
        [_universe(sym_pass, "저평가성장"), _universe(sym_low_eps, "저성장")],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session,
        market="kr",
        spec=UNDERVALUED_GROWTH_SPEC,
        limit=20,
        now=_now,
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == [sym_pass]
    row = result.rows[0]
    assert row["earnings_growth_3y_avg"] == 0.30  # eps_yoy mapped onto the metric key
    assert row["revenue_growth_3y_avg"] == 0.15


async def test_growth_expectation_toss_uses_qoq(db_session):
    await _cleanup(db_session)
    sym_pass = f"{_PREFIX}60"
    sym_low_qoq = f"{_PREFIX}61"
    await _seed(
        db_session,
        [
            _snap(
                sym_pass,
                market_cap=Decimal("5000000000000"),
                eps_yoy=Decimal("0.05"),  # >= 0.03
                eps_qoq=Decimal("0.15"),  # >= 0.10
            ),
            _snap(
                sym_low_qoq,
                market_cap=Decimal("4000000000000"),
                eps_yoy=Decimal("0.10"),
                eps_qoq=Decimal("0.05"),  # < 0.10 → excluded
            ),
        ],
        [_universe(sym_pass, "성장기대"), _universe(sym_low_qoq, "QoQ미달")],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session,
        market="kr",
        spec=GROWTH_EXPECTATION_TOSS_SPEC,
        limit=20,
        now=_now,
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == [sym_pass]
    assert result.rows[0]["earnings_growth_qoq"] == 0.15


async def test_sort_by_spec_sort_key_desc(db_session):
    await _cleanup(db_session)
    # profitable_company sort_by="roe"; higher ROE first.
    sym_hi = f"{_PREFIX}70"
    sym_lo = f"{_PREFIX}71"
    await _seed(
        db_session,
        [
            _snap(
                sym_lo,
                market_cap=Decimal("9000000000000"),  # bigger cap, but lower ROE
                roe_ttm=Decimal("16"),
                gross_margin_ttm=Decimal("0.30"),
            ),
            _snap(
                sym_hi,
                market_cap=Decimal("3000000000000"),
                roe_ttm=Decimal("40"),
                gross_margin_ttm=Decimal("0.30"),
            ),
        ],
        [_universe(sym_hi, "고ROE"), _universe(sym_lo, "저ROE")],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session, market="kr", spec=PROFITABLE_COMPANY_SPEC, limit=20, now=_now
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == [sym_hi, sym_lo]


async def test_limit_caps_output(db_session):
    await _cleanup(db_session)
    snaps = []
    universe = []
    for i in range(5):
        sym = f"{_PREFIX}8{i}"
        snaps.append(
            _snap(
                sym,
                market_cap=Decimal(str((5 - i) * 1_000_000_000_000)),
                roe_ttm=Decimal(str(20 + i)),
                gross_margin_ttm=Decimal("0.30"),
            )
        )
        universe.append(_universe(sym, f"종목{i}"))
    await _seed(db_session, snaps, universe)
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session, market="kr", spec=PROFITABLE_COMPANY_SPEC, limit=2, now=_now
    )
    assert result is not None
    assert len(result.rows) == 2


async def test_returns_none_when_no_partition(monkeypatch, db_session):
    # No partition for the table -> resolve returns None -> loader returns None
    # (caller renders dataState=missing). The shared test DB always carries rows
    # from sibling suites on _SD, so exercise the empty-table branch directly via
    # the real resolver against a patched no-rows path.
    from app.services.invest_view_model import kr_fundamentals_tv_screener as mod

    async def _no_partition(session, *, universe_count=None):
        return None

    monkeypatch.setattr(mod, "_resolve_kr_partition", _no_partition)
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session, market="kr", spec=CHEAP_VALUE_SPEC, limit=20, now=_now
    )
    assert result is None


async def test_thin_partition_capped_to_stale(db_session):
    """A partition below the coverage floor must not be labeled fresh, even when
    its date matches today (mirrors high_yield_value cap_degraded)."""
    await _cleanup(db_session)
    sym = f"{_PREFIX}95"
    await _seed(
        db_session,
        [
            _snap(
                sym,
                market_cap=Decimal("5000000000000"),
                roe_ttm=Decimal("20"),
                gross_margin_ttm=Decimal("0.30"),
            )
        ],
        [_universe(sym, "얇은파티션")],
    )
    # universe_count=100 → floor=50 >> 1 seeded row → unhealthy → capped to stale.
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session,
        market="kr",
        spec=PROFITABLE_COMPANY_SPEC,
        limit=20,
        now=_now,
        universe_count=100,
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == [sym]
    assert result.rows[0]["_screener_snapshot_state"] == "stale"
    assert result.fundamentals_state == "fresh"  # rows exist → not missing


async def test_stable_growth_includes_via_roe_and_yoy_skips_streak(db_session):
    await _cleanup(db_session)
    sym = f"{_PREFIX}90"
    await _seed(
        db_session,
        [
            _snap(
                sym,
                market_cap=Decimal("5000000000000"),
                roe_ttm=Decimal("18"),  # >= 15
                eps_yoy=Decimal("0.15"),  # proxy for min_earnings_growth_3y_avg=0.10
                # no earnings-streak column → skipped + warned
            )
        ],
        [_universe(sym, "안정성장")],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session, market="kr", spec=STABLE_GROWTH_SPEC, limit=20, now=_now
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == [sym]
    assert EARNINGS_STREAK_SKIP_WARNING in result.warnings


# ---------------------------------------------------------------------------
# ROB-429 B1 — full-partition evaluation (cand_cap removed) + total_matched
# ---------------------------------------------------------------------------


_BULK_PREFIX = "991"  # 3 chars; + 3-digit index = 6-char KR-shaped synthetic codes
# Isolated partition date (distinct from _SD = 2026-06-04) so full-partition
# evaluation sees ONLY this suite's rows, not sibling-suite residue on _SD. The
# resolver picks the newest date, so this future date is selected deterministically.
_BULK_SD = dt.date(2026, 6, 5)


async def _cleanup_bulk(db_session) -> None:
    await db_session.execute(
        sa.delete(InvestKrFundamentalsSnapshot).where(
            InvestKrFundamentalsSnapshot.symbol.like(f"{_BULK_PREFIX}%")
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.like(f"{_BULK_PREFIX}%")
        )
    )
    await db_session.commit()


def _bulk_snap(symbol: str, **kw) -> InvestKrFundamentalsSnapshot:
    base = {
        "symbol": symbol,
        "snapshot_date": _BULK_SD,
        "name": symbol,
        "source": "tvscreener_kr",
        "raw_payload": {},
    }
    base.update(kw)
    return InvestKrFundamentalsSnapshot(**base)


async def test_full_partition_includes_low_market_cap_small_cap(db_session):
    """ROB-429 B1: a small (low-market-cap) symbol that passes the preset predicate
    must appear in results. Under the OLD cand_cap = max(limit*8, 200) market-cap-
    ordered cap, a small cap ranked beyond the top 200 by market_cap was never
    evaluated. We seed 200 large-cap passers + 1 tiny passer (rank 201) and assert
    the tiny one IS included now."""
    await _cleanup_bulk(db_session)
    snaps = []
    universe = []
    # 200 large-cap passers (market_cap descends, all pass undervalued_growth).
    # Symbols 991000..991199 keep the largest caps; the small cap is 991999.
    for i in range(200):
        sym = f"{_BULK_PREFIX}{i:03d}"
        snaps.append(
            _bulk_snap(
                sym,
                # huge caps so the small cap sorts to the very bottom by market_cap
                market_cap=Decimal(str((10_000 - i) * 1_000_000_000_000)),
                per=Decimal("15"),  # <= 20
                revenue_yoy=Decimal("0.15"),  # >= 0.10
                eps_yoy=Decimal("0.30"),  # >= 0.20 (proxy)
            )
        )
        universe.append(_universe(sym, f"대형주{i}"))
    # The tiny small cap (rank 201 by market_cap) — excluded by the OLD cap.
    sym_small = f"{_BULK_PREFIX}999"
    snaps.append(
        _bulk_snap(
            sym_small,
            market_cap=Decimal("1000000000"),  # 1B — far below the top 200
            per=Decimal("10"),
            revenue_yoy=Decimal("0.20"),
            eps_yoy=Decimal("0.40"),
        )
    )
    universe.append(_universe(sym_small, "소형성장주"))
    try:
        await _seed(db_session, snaps, universe)

        # Use a high universe_count so the 201-row partition is judged healthy.
        result = await load_kr_fundamentals_preset_from_tv_snapshot(
            db_session,
            market="kr",
            spec=UNDERVALUED_GROWTH_SPEC,
            limit=300,  # large display limit so all matches show
            now=_now,
            universe_count=201,
        )
        assert result is not None
        matched_syms = {r["symbol"] for r in result.rows}
        # The small cap the OLD market-cap cand_cap would have excluded is present.
        assert sym_small in matched_syms
        # All 201 seeded passers matched the predicate (isolated partition date).
        assert result.total_matched == 201
        # The display limit (300) is high enough to show them all.
        assert len(result.rows) == 201
    finally:
        await _cleanup_bulk(db_session)


async def test_total_matched_counts_all_before_display_limit(db_session):
    """total_matched = full-partition predicate matches BEFORE the display limit;
    len(rows) is capped to the display limit. Uses the isolated bulk partition date
    so total_matched is exact (no sibling-suite residue on _SD)."""
    await _cleanup_bulk(db_session)
    snaps = []
    universe = []
    for i in range(5):
        sym = f"{_BULK_PREFIX}{i:03d}"
        snaps.append(
            _bulk_snap(
                sym,
                market_cap=Decimal(str((5 - i) * 1_000_000_000_000)),
                roe_ttm=Decimal(str(20 + i)),  # all pass profitable_company
                gross_margin_ttm=Decimal("0.30"),
            )
        )
        universe.append(_universe(sym, f"종목{i}"))
    try:
        await _seed(db_session, snaps, universe)

        result = await load_kr_fundamentals_preset_from_tv_snapshot(
            db_session,
            market="kr",
            spec=PROFITABLE_COMPANY_SPEC,
            limit=2,  # display only 2
            now=_now,
            universe_count=5,
        )
        assert result is not None
        assert result.total_matched == 5  # all 5 matched the predicate
        assert len(result.rows) == 2  # display limit applied after counting
        # sort_by="roe" desc → the two highest-ROE symbols are shown.
        assert [r["symbol"] for r in result.rows] == [
            f"{_BULK_PREFIX}004",
            f"{_BULK_PREFIX}003",
        ]
    finally:
        await _cleanup_bulk(db_session)


# ---------------------------------------------------------------------------
# ROB-428 PR-C: high_yield_value + undervalued_breakout (rerouted valuation presets)
# ---------------------------------------------------------------------------


async def test_high_yield_value_roe_and_per_bounds(db_session):
    """high_yield_value: ROE >= 15 AND 0 < PER <= 10 (replicates the OLD loader)."""
    await _cleanup(db_session)
    sym_pass = f"{_PREFIX}A0"
    sym_low_roe = f"{_PREFIX}A1"
    sym_high_per = f"{_PREFIX}A2"
    sym_neg_per = f"{_PREFIX}A3"
    sym_null_roe = f"{_PREFIX}A4"
    await _seed(
        db_session,
        [
            _snap(
                sym_pass,
                price=Decimal("8000"),
                change_rate=Decimal("0.5"),
                volume=Decimal("500000"),
                market_cap=Decimal("9000000000000"),
                roe_ttm=Decimal("18"),  # >= 15
                per=Decimal("8"),  # 0 < per <= 10
                sector="Financials",
                industry="Banks",
            ),
            _snap(
                sym_low_roe,
                market_cap=Decimal("8000000000000"),
                roe_ttm=Decimal("9"),  # < 15 → excluded
                per=Decimal("5"),
            ),
            _snap(
                sym_high_per,
                market_cap=Decimal("7000000000000"),
                roe_ttm=Decimal("25"),
                per=Decimal("12"),  # > 10 → excluded
            ),
            _snap(
                sym_neg_per,
                market_cap=Decimal("6000000000000"),
                roe_ttm=Decimal("20"),
                per=Decimal("-3"),  # per <= 0 → excluded (require_positive)
            ),
            _snap(
                sym_null_roe,
                market_cap=Decimal("5000000000000"),
                roe_ttm=None,  # NULL roe → fail-closed exclude
                per=Decimal("6"),
            ),
        ],
        [
            _universe(sym_pass, "고수익저평가"),
            _universe(sym_low_roe, "낮은ROE"),
            _universe(sym_high_per, "고PER"),
            _universe(sym_neg_per, "적자기업"),
            _universe(sym_null_roe, "ROE없음"),
        ],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session,
        market="kr",
        spec=HIGH_YIELD_VALUE_SPEC,
        limit=20,
        now=_now,
        universe_count=5,
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == [sym_pass]
    row = result.rows[0]
    # high_yield_value metric is ROE (must be emitted) + category filled.
    assert row["roe"] == 18.0
    assert row["per"] == 8.0
    assert row["category"] == "Banks"
    assert row["name"] == "고수익저평가"
    assert row["close"] == 8000.0
    assert row["_screener_snapshot_state"] == "fresh"


async def test_high_yield_value_sorts_by_roe_desc(db_session):
    await _cleanup(db_session)
    sym_hi = f"{_PREFIX}A5"
    sym_lo = f"{_PREFIX}A6"
    await _seed(
        db_session,
        [
            _snap(
                sym_lo,
                market_cap=Decimal("9000000000000"),  # bigger cap, lower ROE
                roe_ttm=Decimal("16"),
                per=Decimal("9"),
            ),
            _snap(
                sym_hi,
                market_cap=Decimal("3000000000000"),
                roe_ttm=Decimal("35"),
                per=Decimal("7"),
            ),
        ],
        [_universe(sym_hi, "고ROE"), _universe(sym_lo, "저ROE")],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session, market="kr", spec=HIGH_YIELD_VALUE_SPEC, limit=20, now=_now
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == [sym_hi, sym_lo]


async def test_undervalued_breakout_per_pbr_and_new_high_recency(db_session):
    """undervalued_breakout (ROB-430 PR-② / ROB-432): 0<PER<=10, 0<PBR<=1, and a NEW
    52-week high made within 20 KRX *trading* sessions (week_high_52_date recency via
    XKRX, holiday-aware), NOT price/52w-high proximity.

    Partition date is _SD = 2026-06-04. high-date 2026-05-25 → 8 trading sessions
    (passes); 2026-04-01 → 43 sessions (excluded); NULL / future high-dates are
    fail-closed excluded.
    """
    await _cleanup(db_session)
    sym_pass = f"{_PREFIX}B0"
    sym_old_high = f"{_PREFIX}B1"
    sym_high_pbr = f"{_PREFIX}B2"
    sym_null_high_date = f"{_PREFIX}B3"
    sym_future_high = f"{_PREFIX}B4"
    await _seed(
        db_session,
        [
            _snap(
                sym_pass,
                price=Decimal("9700"),
                change_rate=Decimal("1.0"),
                volume=Decimal("700000"),
                market_cap=Decimal("9000000000000"),
                per=Decimal("8"),  # 0 < per <= 10
                pbr=Decimal("0.8"),  # 0 < pbr <= 1
                week_high_52=Decimal("10000"),
                week_high_52_date=dt.date(2026, 5, 25),  # 8 trading days <= 20 → recent
                sector="Industrials",
                industry="Machinery",
            ),
            _snap(
                sym_old_high,
                market_cap=Decimal("8000000000000"),
                per=Decimal("6"),
                pbr=Decimal("0.7"),
                price=Decimal("9000"),
                week_high_52=Decimal("10000"),
                week_high_52_date=dt.date(
                    2026, 4, 1
                ),  # 43 trading days > 20 → excluded
            ),
            _snap(
                sym_high_pbr,
                market_cap=Decimal("7000000000000"),
                per=Decimal("5"),
                pbr=Decimal("1.5"),  # > 1 → excluded
                price=Decimal("9900"),
                week_high_52=Decimal("10000"),
                week_high_52_date=dt.date(2026, 6, 2),
            ),
            _snap(
                sym_null_high_date,
                market_cap=Decimal("6000000000000"),
                per=Decimal("4"),
                pbr=Decimal("0.5"),
                price=Decimal("5000"),
                week_high_52=Decimal("5200"),
                week_high_52_date=None,  # NULL high-date → unavailable → excluded
            ),
            _snap(
                sym_future_high,
                market_cap=Decimal("5000000000000"),
                per=Decimal("4"),
                pbr=Decimal("0.5"),
                price=Decimal("5000"),
                week_high_52=Decimal("5200"),
                week_high_52_date=dt.date(2026, 6, 10),  # future → age<0 → excluded
            ),
        ],
        [
            _universe(sym_pass, "저평가탈출"),
            _universe(sym_old_high, "고가오래됨"),
            _universe(sym_high_pbr, "고PBR"),
            _universe(sym_null_high_date, "고가일자없음"),
            _universe(sym_future_high, "미래고가"),
        ],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session,
        market="kr",
        spec=UNDERVALUED_BREAKOUT_SPEC,
        limit=20,
        now=_now,
        universe_count=5,
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == [sym_pass]
    row = result.rows[0]
    # ROB-430 PR-②: the honest signal fields. proximity stays as an emitted column.
    assert row["week_high_52_date"] == "2026-05-25"
    # ROB-432: 8 KRX trading sessions between 2026-05-25 (a holiday) and 2026-06-04.
    assert row["new_high_age_trading_days"] == 8
    assert row["high_52w_proximity"] == pytest.approx(0.97)
    assert row["week_high_52"] == 10000.0
    assert row["per"] == 8.0
    assert row["pbr"] == 0.8
    assert row["category"] == "Machinery"
    assert row["name"] == "저평가탈출"
    # The excluded rows record a reason (fail-closed, never silent pass).
    excluded = {e["symbol"]: e["reason"] for e in result.excluded}
    assert excluded[sym_old_high] == "52w high not recent"
    # ROB-432: distinct reasons so missing-data vs future vs calendar-range differ.
    assert excluded[sym_null_high_date] == "52w-high date unavailable"
    assert excluded[sym_future_high] == "52w-high date in future"
    assert "pbr" in excluded[sym_high_pbr]


async def test_undervalued_breakout_sorts_by_per_ascending(db_session):
    """ROB-432: Toss 저평가 탈출 default order = PER ascending (cheapest first).

    market_cap is reversed vs PER so the assertion can ONLY pass under PER-ascending
    (not the old market_cap-desc): the low-PER name has the SMALLER market cap.
    """
    await _cleanup(db_session)
    sym_low_per = f"{_PREFIX}B5"
    sym_high_per = f"{_PREFIX}B6"
    await _seed(
        db_session,
        [
            _snap(
                sym_high_per,
                market_cap=Decimal("9000000000000"),  # bigger cap, higher PER → second
                per=Decimal("9"),
                pbr=Decimal("0.7"),
                price=Decimal("9990"),
                week_high_52=Decimal("10000"),
                week_high_52_date=dt.date(2026, 5, 28),  # recent
            ),
            _snap(
                sym_low_per,
                market_cap=Decimal("1000000000000"),  # smaller cap, lower PER → first
                per=Decimal("4"),
                pbr=Decimal("0.6"),
                price=Decimal("9600"),
                week_high_52=Decimal("10000"),
                week_high_52_date=dt.date(2026, 5, 30),  # recent
            ),
        ],
        [_universe(sym_low_per, "저PER주"), _universe(sym_high_per, "고PER주")],
    )
    result = await load_kr_fundamentals_preset_from_tv_snapshot(
        db_session, market="kr", spec=UNDERVALUED_BREAKOUT_SPEC, limit=20, now=_now
    )
    assert result is not None
    # PER ascending: 4 before 9 (even though sym_low_per has the smaller market cap).
    assert [r["symbol"] for r in result.rows] == [sym_low_per, sym_high_per]
