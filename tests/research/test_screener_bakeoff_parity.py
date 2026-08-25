"""Research source definition ↔ production definition parity.

Path ① remaining: ``kr.double_buy`` must match the production loader on the
same fixture (real call). Path ②: ``us.high_yield_value`` is contracted as a
research definition whose live parity was not verified.

``tv_rsi45`` live-comparison parity tests were deleted. Two adversarial
rounds showed the reconstruction is not live fanout; a test derived from the
same misread cannot catch it. The comparator is now logged live picks.
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.core.db import engine
from research.screener_bakeoff.sources import (
    MarketContext,
    src_double_buy,
    src_us_high_yield_value,
)
from research.screener_bakeoff.spec import SOURCES_BY_ID
from tests._run_owned_database import validate_run_owned_database_url

# BL-4b: this module's ``_double_buy_parity_rows`` fixture DELETEs/writes
# real rows. ``--noconftest`` skips tests/conftest.py entirely, so the normal
# run-owned-database env setup and ``db_session`` fixture definition never
# happen — but a caller could still supply their own ``db_session`` fixture
# (e.g. a hand-rolled local plugin) pointed at a production DATABASE_URL.
# Validate the process-wide engine URL at import time, before any fixture or
# test body in this module can run, so the write path fails closed
# independent of how ``db_session`` was constructed.
validate_run_owned_database_url(engine.url)

# Isolated from sibling loader suites (91xxxx / 92xxxx).
_DB_SYMBOLS = ["931000", "931001", "931002", "931003"]


# ---------------------------------------------------------------------------
# ② us.high_yield_value — research definition; live parity not verified
# ---------------------------------------------------------------------------


def test_us_high_yield_value_is_research_definition_not_live_preset():
    from app.services.invest_view_model.us_quality_guards import (
        US_MAX_ROE_PERCENT,
        US_MIN_MARKET_CAP_USD,
    )

    spec = SOURCES_BY_ID["us.high_yield_value"]
    assert spec.live_comparable is False
    blob = (spec.label + " " + " ".join(spec.caveats)).lower()
    assert "tvscreener" in blob
    assert "parity" in blob
    assert "미검증" in spec.label or "미검증" in " ".join(spec.caveats)
    assert "라이브" in spec.label
    # The r2 "yahoo=0 ⇒ reconstruction impossible" claim was false.
    assert "impossible" not in blob
    assert "yahoo partition" not in blob
    assert "yahoo=0" not in blob.replace(" ", "")

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


def test_crypto_tv_rsi45_is_not_a_live_comparator():
    spec = SOURCES_BY_ID["crypto.tv_rsi45"]
    assert spec.live_comparable is False
    blob = (spec.label + " " + " ".join(spec.caveats)).lower()
    assert "철회" in spec.label or "withdrawn" in blob
    assert "현행 주력" not in spec.label


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
    cave = " ".join(spec.caveats)
    assert "fail-closed" in cave
    assert "17.5%" in cave


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


def test_write_fixture_fails_closed_under_noconftest_with_synthetic_prod_url() -> None:
    """BL-4b: prove the module-level guard, not just its bare presence.

    ``--noconftest`` skips ``tests/conftest.py`` entirely, so the run-owned
    database env vars and the ``db_session`` fixture definition never exist.
    A caller could still supply their own ``db_session`` (e.g. a hand-rolled
    local plugin) bound to a real ``DATABASE_URL``. This spawns the real
    interpreter against this real test file — not a synthetic probe — with a
    synthetic non-owned ``prod-db.invalid`` URL and asserts the module-level
    guard call rejects it BEFORE ``_double_buy_parity_rows`` (or any other
    fixture in this module) can open a connection or issue a write.

    Mutant check: delete the ``validate_run_owned_database_url(engine.url)``
    call (or its import) at the top of this file — the subprocess still
    exits non-zero (pytest can't resolve the missing ``db_session`` fixture
    either way), but the specific guard message below never appears, so this
    assertion goes RED instead of silently agreeing with the wrong failure.
    """

    repo_root = Path(__file__).resolve().parents[2]
    target = "tests/research/test_screener_bakeoff_parity.py::test_double_buy_research_matches_production_loader"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        # Synthetic, unreachable, non-owned target — never a real credential.
        "DATABASE_URL": (
            "postgresql+asyncpg://postgres:postgres@prod-db.invalid:5432/prod_db"
        ),
        "KIS_APP_KEY": "DUMMY_KIS_APP_KEY",
        "KIS_APP_SECRET": "DUMMY_KIS_APP_SECRET",
        "OPENDART_API_KEY": "DUMMY_OPENDART_API_KEY",
        "UPBIT_ACCESS_KEY": "DUMMY_UPBIT_ACCESS_KEY",
        "UPBIT_SECRET_KEY": "DUMMY_UPBIT_SECRET_KEY",
        "SECRET_KEY": "Test_Secret_Key_12345_Test_Secret_Key_12345",
        "ENVIRONMENT": "test",
    }

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--noconftest", target, "-q"],
        capture_output=True,
        env=env,
        cwd=repo_root,
        text=True,
        timeout=60,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "refusing unsafe or unowned pytest database name" in combined, combined
    # Guard fires at import time — the fixture body's DELETE/add_all/commit
    # must never even attempt to resolve the (nonexistent, in this bare
    # --noconftest context) ``db_session`` fixture.
    assert "fixture 'db_session' not found" not in combined, combined
