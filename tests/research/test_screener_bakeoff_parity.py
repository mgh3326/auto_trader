"""Research source definition ↔ production definition parity.

Path ① sources must produce the same ranked names as the production
function/query on the same fixture. Path ② sources must be contracted as
NOT the live preset — so a future production change cannot silently
re-attach a live-comparison claim.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

import pandas as pd
import pytest
import pytest_asyncio
import sqlalchemy as sa

from research.screener_bakeoff.sources import (
    MarketContext,
    src_double_buy,
    src_tv_rsi45,
    src_us_high_yield_value,
)
from research.screener_bakeoff.spec import SOURCES_BY_ID

# Isolated from sibling loader suites (91xxxx / 92xxxx).
_DB_SYMBOLS = ["931000", "931001", "931002", "931003"]


# ---------------------------------------------------------------------------
# ① tv_rsi45 — live fanout is rsi-asc with no max_rsi / no adv_krw_min
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_fanout_rsi_source_omits_max_rsi_and_adv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import analysis_tool_handlers
    from app.mcp_server.tooling import buy_candidate_fanout as fanout
    from app.mcp_server.tooling.buy_candidate_fanout import TOP_N_PER_SOURCE

    received: dict[str, Any] = {}

    async def fake_screen_stocks_impl(**request: Any) -> dict[str, Any]:
        received.update(request)
        return {"results": [], "total_count": 0}

    monkeypatch.setattr(
        analysis_tool_handlers, "screen_stocks_impl", fake_screen_stocks_impl
    )
    rsi_source = next(s for s in fanout._LIVE_SOURCES if s.sort_by == "rsi")
    await fanout._read_live_source(rsi_source, "kr", TOP_N_PER_SOURCE)
    assert received["sort_by"] == "rsi"
    assert received["sort_order"] == "asc"
    assert "max_rsi" not in received
    assert "adv_krw_min" not in received


def test_tv_rsi45_ranking_matches_production_sort_without_prefilters():
    from app.mcp_server.tooling.screening.common import (
        _apply_basic_filters,
        _sort_and_limit,
    )

    day = dt.date(2026, 7, 15)
    # A is illiquid (turnover 1); B has RSI 80; both must survive candidate gen.
    screener = pd.DataFrame(
        {
            "symbol": ["LIQ_LOW", "RSI_HIGH", "MID", "BEST"],
            "daily_volume": [1.0, 1e7, 1e7, 1e7],
            "latest_close": [1.0, 100.0, 100.0, 100.0],
        }
    )
    rsi_lookup = {
        ("LIQ_LOW", day): 12.0,
        ("RSI_HIGH", day): 80.0,
        ("MID", day): 30.0,
        ("BEST", day): 8.0,
    }
    ctx = MarketContext("kr", screener={day: screener})
    got = src_tv_rsi45(ctx, day, rsi_lookup)
    stocks = [
        {"symbol": sym, "rsi": rsi_lookup[(sym, day)]} for sym in screener["symbol"]
    ]
    filtered = _apply_basic_filters(
        stocks,
        min_market_cap=None,
        max_per=None,
        max_pbr=None,
        min_dividend_yield=None,
        max_rsi=None,
        adv_krw_min=None,
    )
    expected = [r["symbol"] for r in _sort_and_limit(filtered, "rsi", "asc", 100)]
    assert got == expected
    assert got[0] == "BEST"
    assert "LIQ_LOW" in got
    assert "RSI_HIGH" in got
    spec = SOURCES_BY_ID["kr.tv_rsi45"]
    assert spec.live_comparable is True
    cave = " ".join(spec.caveats)
    assert "max_rsi omitted" in cave
    assert "adv_krw_min omitted" in cave


def test_us_tv_rsi45_is_the_same_live_fanout_contract():
    spec = SOURCES_BY_ID["us.tv_rsi45"]
    assert spec.live_comparable is True
    assert "rsi-asc" in spec.label
    cave = " ".join(spec.caveats)
    assert "kr.tv_rsi45" in cave


# ---------------------------------------------------------------------------
# ② us.high_yield_value — NOT the live Yahoo + quality-guard preset
# ---------------------------------------------------------------------------


def test_us_high_yield_value_is_research_definition_not_live_preset():
    from app.services.invest_view_model.us_quality_guards import (
        US_MAX_ROE_PERCENT,
        US_MIN_MARKET_CAP_USD,
    )

    spec = SOURCES_BY_ID["us.high_yield_value"]
    assert spec.live_comparable is False
    blob = (spec.label + " " + " ".join(spec.caveats)).lower()
    assert "yahoo" in blob
    assert "tvscreener" in blob
    assert "not the live" in blob
    assert "라이브" in spec.label

    day = dt.date(2026, 7, 15)
    # MICRO would be dropped by the live $100M / ROE-cap guards.
    df = pd.DataFrame(
        {
            "symbol": ["MICRO", "OKNAME"],
            "roe": [50.0, 20.0],
            "per": [5.0, 8.0],
            "market_cap": [float(US_MIN_MARKET_CAP_USD) / 10.0, 5e9],
        }
    )
    # egregious ROE artifact the live cap would drop
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                {
                    "symbol": ["ROEJUNK"],
                    "roe": [float(US_MAX_ROE_PERCENT) + 1.0],
                    "per": [4.0],
                    "market_cap": [5e9],
                }
            ),
        ],
        ignore_index=True,
    )
    ctx = MarketContext("us", fundamentals={day: df})
    got = src_us_high_yield_value(ctx, day)
    assert "MICRO" in got, "research definition must NOT apply the live cap floor"
    assert "ROEJUNK" in got, "research definition must NOT apply the live ROE cap"
    assert got[0] == "ROEJUNK"  # ranked by roe desc


# ---------------------------------------------------------------------------
# ① kr.double_buy — production loader vs research builder on the same rows
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _double_buy_parity_rows(db_session, monkeypatch):
    from app.models.invest_screener_snapshot import InvestScreenerSnapshot
    from app.models.investor_flow_snapshot import InvestorFlowSnapshot
    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.services.invest_screener_snapshots import partition_health
    from app.services.invest_screener_snapshots.partition_health import (
        HealthyPartition,
    )

    async def _resolve_raw_latest_partition(
        session,
        *,
        model,
        date_col,
        market_col,
        market,
        **_kwargs,
    ):
        newest = (
            await session.execute(
                sa.select(sa.func.max(date_col)).where(market_col == market)
            )
        ).scalar_one_or_none()
        if newest is None:
            return None
        row_count = int(
            (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(model)
                    .where(market_col == market, date_col == newest)
                )
            ).scalar()
            or 0
        )
        return HealthyPartition(
            partition_date=newest,
            row_count=row_count,
            coverage_ratio=1.0,
            is_fallback=False,
            healthy=True,
        )

    monkeypatch.setattr(
        partition_health,
        "resolve_healthy_partition",
        _resolve_raw_latest_partition,
    )

    async def _purge() -> None:
        await db_session.execute(
            sa.delete(InvestorFlowSnapshot).where(
                InvestorFlowSnapshot.symbol.in_(_DB_SYMBOLS)
            )
        )
        await db_session.execute(
            sa.delete(InvestScreenerSnapshot).where(
                InvestScreenerSnapshot.symbol.in_(_DB_SYMBOLS)
            )
        )
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(_DB_SYMBOLS))
        )
        await db_session.commit()

    await _purge()
    yield db_session
    await _purge()


@pytest.mark.asyncio
async def test_double_buy_research_matches_production_loader(_double_buy_parity_rows):
    from app.models.invest_screener_snapshot import InvestScreenerSnapshot
    from app.models.investor_flow_snapshot import InvestorFlowSnapshot
    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.services.invest_view_model.double_buy_screener import (
        load_double_buy_from_snapshots,
    )

    db_session = _double_buy_parity_rows
    today = dt.date(2100, 6, 1)
    prior = dt.date(2100, 5, 31)
    # 931000: DoD increase both legs, +change → KEEP
    # 931001: double_buy=True cached but DoD decrease → DROP (old research rule
    #         would have kept this; that is the blocker being fixed)
    # 931002: DoD increase but negative change_rate → DROP
    # 931003: no prior row → DROP (fail-closed)
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol=sym, name="보통주테스트", exchange="KOSPI", is_active=True
            )
            for sym in _DB_SYMBOLS
        ]
    )
    db_session.add_all(
        [
            InvestorFlowSnapshot(
                market="kr",
                symbol="931000",
                snapshot_date=today,
                foreign_net=2_000_000,
                institution_net=3_000_000,
                double_buy=False,
                double_sell=False,
                source="naver_finance",
            ),
            InvestorFlowSnapshot(
                market="kr",
                symbol="931000",
                snapshot_date=prior,
                foreign_net=1_000_000,
                institution_net=1_000_000,
                double_buy=False,
                double_sell=False,
                source="naver_finance",
            ),
            InvestorFlowSnapshot(
                market="kr",
                symbol="931001",
                snapshot_date=today,
                foreign_net=100,
                institution_net=100,
                double_buy=True,
                double_sell=False,
                source="naver_finance",
            ),
            InvestorFlowSnapshot(
                market="kr",
                symbol="931001",
                snapshot_date=prior,
                foreign_net=500,
                institution_net=500,
                double_buy=True,
                double_sell=False,
                source="naver_finance",
            ),
            InvestorFlowSnapshot(
                market="kr",
                symbol="931002",
                snapshot_date=today,
                foreign_net=9,
                institution_net=9,
                double_buy=True,
                double_sell=False,
                source="naver_finance",
            ),
            InvestorFlowSnapshot(
                market="kr",
                symbol="931002",
                snapshot_date=prior,
                foreign_net=1,
                institution_net=1,
                double_buy=True,
                double_sell=False,
                source="naver_finance",
            ),
            InvestorFlowSnapshot(
                market="kr",
                symbol="931003",
                snapshot_date=today,
                foreign_net=9,
                institution_net=9,
                double_buy=True,
                double_sell=False,
                source="naver_finance",
            ),
        ]
    )
    db_session.add_all(
        [
            InvestScreenerSnapshot(
                market="kr",
                symbol=sym,
                snapshot_date=today,
                latest_close=decimal.Decimal("10000"),
                prev_close=decimal.Decimal("9000" if sym != "931002" else "11000"),
                change_rate=decimal.Decimal("10.0" if sym != "931002" else "-10.0"),
                daily_volume=1000,
                closes_window=[9000, 10000],
                source="kis",
            )
            for sym in _DB_SYMBOLS
        ]
    )
    await db_session.commit()

    production = await load_double_buy_from_snapshots(db_session, market="kr", limit=50)
    assert production is not None
    prod_ours = [r["symbol"] for r in production.rows if r["symbol"] in _DB_SYMBOLS]

    flow_today = pd.DataFrame(
        [
            {
                "symbol": "931000",
                "source": "naver_finance",
                "foreign_net": 2_000_000,
                "institution_net": 3_000_000,
                "double_buy": False,
                "change_rate": 10.0,
            },
            {
                "symbol": "931001",
                "source": "naver_finance",
                "foreign_net": 100,
                "institution_net": 100,
                "double_buy": True,
                "change_rate": 10.0,
            },
            {
                "symbol": "931002",
                "source": "naver_finance",
                "foreign_net": 9,
                "institution_net": 9,
                "double_buy": True,
                "change_rate": -10.0,
            },
            {
                "symbol": "931003",
                "source": "naver_finance",
                "foreign_net": 9,
                "institution_net": 9,
                "double_buy": True,
                "change_rate": 10.0,
            },
        ]
    )
    flow_prior = pd.DataFrame(
        [
            {
                "symbol": "931000",
                "source": "naver_finance",
                "foreign_net": 1_000_000,
                "institution_net": 1_000_000,
            },
            {
                "symbol": "931001",
                "source": "naver_finance",
                "foreign_net": 500,
                "institution_net": 500,
            },
            {
                "symbol": "931002",
                "source": "naver_finance",
                "foreign_net": 1,
                "institution_net": 1,
            },
        ]
    )
    screener = pd.DataFrame(
        [
            {
                "symbol": sym,
                "change_rate": -10.0 if sym == "931002" else 10.0,
                "daily_volume": 1000,
                "latest_close": 10000,
            }
            for sym in _DB_SYMBOLS
        ]
    )
    ctx = MarketContext(
        "kr",
        screener={today: screener},
        flow={today: flow_today, prior: flow_prior},
    )
    research = src_double_buy(ctx, today)
    assert research == prod_ours
    assert research == ["931000"]
    assert "931001" not in research
    assert "931003" not in research
    spec = SOURCES_BY_ID["kr.double_buy"]
    assert spec.live_comparable is True
    assert "fail-closed" in " ".join(spec.caveats)


def test_double_buy_fail_closed_without_prior_partition():
    day = dt.date(2026, 7, 15)
    flow = pd.DataFrame(
        {
            "symbol": ["AAA"],
            "source": ["naver_finance"],
            "foreign_net": [10],
            "institution_net": [10],
            "double_buy": [True],
            "change_rate": [5.0],
        }
    )
    screener = pd.DataFrame(
        {
            "symbol": ["AAA"],
            "change_rate": [5.0],
            "daily_volume": [1],
            "latest_close": [1],
        }
    )
    ctx = MarketContext("kr", screener={day: screener}, flow={day: flow})
    assert src_double_buy(ctx, day) == []
