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
from types import SimpleNamespace

import pandas as pd
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

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
# happen. This validates the process-wide ``app.core.db.engine`` URL at
# import time, before any fixture or test body in this module can run — this
# covers a caller who runs this file bare (no ``db_session`` provider at
# all). It does NOT cover a caller who supplies their OWN ``db_session``
# fixture bound to a different, independently-constructed engine (e.g. a
# hand-rolled local plugin) — that fixture's own bind is a distinct object
# this import-time check never inspects. See the fixture-scoped check inside
# ``_double_buy_parity_rows`` below for what that adds and does not add
# (BL-4b rework round 2: "closing that gap" overclaimed — see that comment).
validate_run_owned_database_url(engine.url)


def _validate_session_bind_url(session) -> None:
    """Resolve and validate whatever ``session.get_bind()`` (no args) reports.

    SHOULD-1 (BL-4b rework round 2): ``get_bind()`` does not always return an
    Engine. A session joined onto an external transaction (the SAVEPOINT
    pattern in ``tests/_investment_reports_helpers.py``) returns a bare
    ``Connection``, which has no ``.url`` of its own — only ``.engine.url``.
    Without this fallback, that shape would fail closed with an opaque
    ``AttributeError: 'Connection' object has no attribute 'url'`` instead
    of the intended, diagnosable ``RuntimeError`` from the guard.
    """
    bind = session.get_bind()
    validate_run_owned_database_url(getattr(bind, "url", None) or bind.engine.url)


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
    # BL-4b rework (round 2 wording fix): the module-level check above only
    # sees the process-wide ``app.core.db.engine`` — a caller supplying
    # their OWN ``db_session`` fixture bound to a different,
    # independently-constructed engine (e.g. a hand-rolled local plugin
    # under ``--noconftest``) never touches that object. This validates the
    # DEFAULT bind ``db_session.get_bind()`` reports, before any
    # DELETE/add_all/commit below.
    #
    # Scope, precisely: this covers the realistic careless case of a
    # substitute fixture handing back a plain ``AsyncSession``/``Connection``
    # bound to one different, wrong engine — confirmed empirically to reject
    # that with zero connection attempts (docs/rework reports for this PR).
    # It does NOT cover, and cannot: per-mapper ``Session(binds={Model:
    # other_engine})`` routing (a mapper-specific bind never surfaces
    # through the no-argument ``get_bind()`` this line calls); a same-shaped
    # URL whose ``connect_args`` silently redirects the actual TCP/socket
    # target the driver dials; or a ``Session`` subclass that overrides
    # ``get_bind()`` to lie about what it returns. Guarantee strength here
    # is "accidental prevention + static detection" (the #1949/BL-4 house
    # style), not structural impossibility — nothing at the Python level can
    # make it that.
    _validate_session_bind_url(db_session)

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


def _expected_rejection_message(
    url: str = "postgresql+asyncpg://postgres:postgres@prod-db.invalid:5432/prod_db",
) -> str:
    """The guard's own current rejection text for a non-owned URL.

    NIT-1 (BL-4b rework): the guard's message string is owned by
    ``tests/_run_owned_database.py``, a module this file does not own. A
    hardcoded copy of that string here would go stale (false-red) the moment
    that module's wording changes without the guard itself weakening. Instead
    of duplicating the literal, invoke the real function once and capture
    what it actually raises right now, so the two tests below always compare
    against the guard's live behavior rather than a frozen guess.
    """
    try:
        validate_run_owned_database_url(url)
    except RuntimeError as exc:
        return str(exc)
    raise AssertionError(
        "expected validate_run_owned_database_url to reject this synthetic URL"
    )


_NONCONFTEST_ENV_BASE = {
    "PATH": os.environ.get("PATH", ""),
    "HOME": os.environ.get("HOME", ""),
    "KIS_APP_KEY": "DUMMY_KIS_APP_KEY",
    "KIS_APP_SECRET": "DUMMY_KIS_APP_SECRET",
    "OPENDART_API_KEY": "DUMMY_OPENDART_API_KEY",
    "UPBIT_ACCESS_KEY": "DUMMY_UPBIT_ACCESS_KEY",
    "UPBIT_SECRET_KEY": "DUMMY_UPBIT_SECRET_KEY",
    "SECRET_KEY": "Test_Secret_Key_12345_Test_Secret_Key_12345",
    "ENVIRONMENT": "test",
}


def test_write_fixture_fails_closed_under_noconftest_with_synthetic_prod_url() -> None:
    """BL-4b: prove the module-level guard, not just its bare presence.

    ``--noconftest`` skips ``tests/conftest.py`` entirely, so the run-owned
    database env vars and the ``db_session`` fixture definition never exist.
    This spawns the real interpreter against this real test file — not a
    synthetic probe — with a synthetic non-owned ``prod-db.invalid`` URL and
    asserts the module-level guard call rejects it BEFORE
    ``_double_buy_parity_rows`` (or any other fixture in this module) can
    open a connection or issue a write.

    Scope (BL-4b rework SHOULD-1): this only proves the module-level check
    on the process-wide ``app.core.db.engine``. It does NOT prove anything
    about a caller-substituted ``db_session`` bound to a different engine —
    see ``test_fixture_level_guard_blocks_a_locally_reachable_non_owned_engine``
    below for what the fixture-level check (added in the same rework) does
    and does not additionally cover (round 2: it is not a complete close —
    per-mapper ``binds={...}`` routing and ``connect_args`` DSN overrides
    still bypass it; see that fixture's comment for the precise boundary).

    Mutant check: delete the module-level
    ``validate_run_owned_database_url(engine.url)`` call (or its import) at
    the top of this file — the subprocess still exits non-zero (pytest can't
    resolve the missing ``db_session`` fixture either way), but the specific
    guard message below never appears, so this assertion goes RED instead of
    silently agreeing with the wrong failure.

    🔴 This test is the ONLY regression guard for the module-level check.
    ``tests/infra/test_database_guard_completeness.py`` (the #1949 meta
    test) does not cover this module: its AST scan classifies a module as a
    "survivor" requiring the guard only via patterns tied to direct
    engine/session construction, and this module's DB access is exercised
    through the plain ``db_session`` fixture parameter — deleting the guard
    call here does not flip that classification. Verified empirically: with
    the guard call removed, ``tests/infra/test_database_guard_completeness.py``
    still reports ``1 passed``. If this test is skipped, xfailed, or
    deleted, a regression that removes the guard call passes CI silently.
    """

    repo_root = Path(__file__).resolve().parents[2]
    target = "tests/research/test_screener_bakeoff_parity.py::test_double_buy_research_matches_production_loader"
    env = {
        **_NONCONFTEST_ENV_BASE,
        # Synthetic, unreachable, non-owned target — never a real credential.
        "DATABASE_URL": (
            "postgresql+asyncpg://postgres:postgres@prod-db.invalid:5432/prod_db"
        ),
    }

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--noconftest", target, "-q"],
        capture_output=True,
        env=env,
        cwd=repo_root,
        text=True,
        timeout=120,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert _expected_rejection_message() in combined, combined
    # Guard fires at import time — the fixture body's DELETE/add_all/commit
    # must never even attempt to resolve the (nonexistent, in this bare
    # --noconftest context) ``db_session`` fixture.
    assert "fixture 'db_session' not found" not in combined, combined


def test_fixture_level_guard_blocks_a_locally_reachable_non_owned_engine(
    tmp_path,
) -> None:
    """BL-4b rework SHOULD-1: prove the fixture-level check, not just the
    module-level one.

    The module-level check in this file only inspects the process-wide
    ``app.core.db.engine`` — it says nothing about a caller who supplies
    their OWN ``db_session`` fixture bound to a DIFFERENT,
    independently-constructed engine (e.g. a hand-rolled local plugin under
    ``--noconftest``). A verifier demonstrated this bypass for real: a
    ``db_session`` fixture provider bound to an actually-reachable local
    PostgreSQL server (a different, non-run-owned database on the same
    server) sailed past the module-level check and issued a real
    ``DELETE FROM investor_flow_snapshots ...`` against it, failing only
    with ``UndefinedTableError`` because that database happened to have no
    app schema.

    This reproduces that exact shape: a synthetic run-owned-SHAPED
    ``DATABASE_URL`` (so the module-level check on ``engine.url`` passes —
    this test is about the fixture-level gap, not re-proving the
    module-level check) paired with a ``db_session`` plugin bound to its OWN
    ``create_async_engine(...)`` pointed at a database that is NOT that
    run-owned target.

    🔴 Correction (round 2 NIT-1): with the fixture-level guard PRESENT (the
    normal, green path this test exercises), NO PostgreSQL server needs to
    be reachable at all — ``create_async_engine()`` is lazy and the guard
    rejects the URL before any query, so zero connection attempts are made.
    A locally-reachable PostgreSQL at ``localhost:5432``/``postgres``:
    ``postgres`` only matters for the MANUAL mutant reproduction described
    below (and recorded in this PR's rework reports), where the guard is
    temporarily removed by hand to show the ``DELETE`` actually lands
    somewhere real — CI never runs that removed-guard state.

    Mutant check: delete the fixture-level guard call inside
    ``_double_buy_parity_rows`` (as of round 2: the
    ``_validate_session_bind_url(db_session)`` call) — the subprocess then
    actually reaches the evil engine and fails with ``UndefinedTableError``
    from a real ``DELETE`` instead of the expected guard message, so the
    assertions below go RED
    instead of silently agreeing with the wrong failure. (This DOES require
    a real reachable local PostgreSQL, since removing the guard is what
    lets the query attempt actually happen.)

    🔴 This test is the ONLY regression guard for the fixture-level check.
    Neither ``tests/infra/test_database_guard_completeness.py`` nor
    ``test_write_fixture_fails_closed_under_noconftest_with_synthetic_prod_url``
    exercises a caller-substituted ``db_session`` — both leave this bypass
    invisible on their own.

    Scope (round 2 BLOCKER-1 correction): the fixture-level check only
    closes the realistic careless case reproduced here — a substitute
    ``db_session`` bound to one different, wrong engine via the plain
    ``AsyncSession(bind=...)``/``get_bind()`` path. It does NOT close, and
    this test does not claim to close: per-mapper ``Session(binds={Model:
    other_engine})`` routing, a same-shaped URL whose ``connect_args``
    redirects the actual socket target, or a ``Session`` subclass
    overriding ``get_bind()``. See the comment on the guard call inside
    ``_double_buy_parity_rows`` for the full boundary.
    """

    plugin_dir = tmp_path
    plugin_path = plugin_dir / "bl4b_evil_db_session_plugin.py"
    plugin_path.write_text(
        "from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine\n"
        "import pytest_asyncio\n"
        "\n"
        "_EVIL_ENGINE = create_async_engine(\n"
        "    'postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres',\n"
        "    pool_pre_ping=True,\n"
        ")\n"
        "\n"
        "\n"
        "@pytest_asyncio.fixture\n"
        "async def db_session():\n"
        "    async with AsyncSession(_EVIL_ENGINE, expire_on_commit=False) as session:\n"
        "        yield session\n",
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parents[2]
    target = "tests/research/test_screener_bakeoff_parity.py::test_double_buy_research_matches_production_loader"
    synthetic_owned_url = (
        "postgresql+asyncpg://postgres:postgres"
        "@localhost:5432/test_db_pytest_aaaaaaaaaaaa_main"
    )
    env = {
        **_NONCONFTEST_ENV_BASE,
        "PYTHONPATH": str(plugin_dir),
        # A synthetic but correctly-SHAPED run-owned URL/env, so the
        # module-level check (which only ever sees ``engine.url``) passes —
        # this reproduction is about the fixture-level gap, not about
        # re-triggering the module-level rejection from the other test.
        # ``engine`` is lazy (ROB-964): this URL is never actually dialed.
        "AUTO_TRADER_PYTEST_RUN_UID": "aaaaaaaaaaaa",
        "AUTO_TRADER_XDIST_DATABASE_NAME": "test_db_pytest_aaaaaaaaaaaa_main",
        "AUTO_TRADER_XDIST_BASE_DATABASE_URL": synthetic_owned_url,
        "DATABASE_URL": synthetic_owned_url,
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--noconftest",
            "-p",
            "bl4b_evil_db_session_plugin",
            target,
            "-q",
        ],
        capture_output=True,
        env=env,
        cwd=repo_root,
        text=True,
        timeout=120,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert _expected_rejection_message() in combined, combined
    # The exact bypass this reproduces and rejects: without the fixture-level
    # check, the evil session (a substitute AsyncSession bound to a
    # different engine) reaches the real, reachable local PostgreSQL server
    # and issues a real DELETE there before failing on the missing table.
    assert "UndefinedTableError" not in combined, combined
    assert "DELETE FROM investor_flow_snapshots" not in combined, combined


@pytest.mark.asyncio
async def test_bind_resolution_handles_a_connection_bound_session_without_attributeerror(
    db_session,
):
    """BL-4b rework round 2 SHOULD-1: ``get_bind()`` is not always an Engine.

    A session joined onto an external transaction — the exact SAVEPOINT-join
    pattern already used by ``tests/_investment_reports_helpers.py::session``
    — returns a bare ``Connection`` from ``get_bind()``, which has no
    ``.url``. Before this fix, ``_double_buy_parity_rows`` would have died
    with an opaque ``AttributeError: 'Connection' object has no attribute
    'url'`` if this module's fixture chain ever changed shape that way,
    instead of the intended, diagnosable ``RuntimeError``.

    Two things are pinned here, both against the REAL run-owned engine (no
    subprocess, no ``--noconftest`` needed — this is a same-process contract
    test of ``_validate_session_bind_url`` itself):

    1. Normal pass: a genuinely Connection-bound session (confirmed via
       ``not hasattr(bind, "url")`` so this test cannot silently degrade
       into exercising the Engine branch instead) against the real
       run-owned test database passes without raising anything.
    2. Fail-closed maintained: a Connection-SHAPED fake bind (same
       attribute shape: no ``.url``, only ``.engine.url``) pointing at a
       non-owned database still raises ``RuntimeError`` — the ``.engine.url``
       fallback does not weaken rejection, it only fixes the diagnostic.

    Mutant check: revert ``_validate_session_bind_url`` to the round-1 form
    (``validate_run_owned_database_url(session.get_bind().url)``, no
    ``getattr`` fallback) — assertion 1 below goes RED with
    ``AttributeError: 'Connection' object has no attribute 'url'`` instead
    of passing silently.
    """

    async with engine.connect() as connection:
        factory = async_sessionmaker(bind=connection, expire_on_commit=False)
        async with factory() as connection_bound_session:
            bind = connection_bound_session.get_bind()
            assert not hasattr(bind, "url"), (
                "expected a bare Connection here (no .url) — this test's "
                "premise depends on that shape; a plain Engine bind would "
                "make assertion 1 pass for the wrong reason"
            )
            _validate_session_bind_url(connection_bound_session)  # must not raise

    class _ConnectionShapedFakeBind:
        """Mimics the attribute shape ``get_bind()`` returns for a
        Connection: no ``.url``, only ``.engine.url``."""

        def __init__(self, url: str) -> None:
            self.engine = SimpleNamespace(url=url)

    class _FakeConnectionBoundSession:
        def __init__(self, bind) -> None:
            self._bind = bind

        def get_bind(self):
            return self._bind

    evil_bind = _ConnectionShapedFakeBind(
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"
    )
    with pytest.raises(RuntimeError):
        _validate_session_bind_url(_FakeConnectionBoundSession(evil_bind))
