"""hk 6036 — Upbit intraday shadow storage migration and its writer.

The DB part is CI only: it uses the pytest-owned throwaway database and runs the
whole round trip inside one transaction that is rolled back. The static tests need
no database.

Covered: upgrade/downgrade round trip, the writer's idempotency (a re-run keeps
the first receipt), pending resolution, the virtual/disposition/all-or-reason
checks, and downgrade refusal while rows exist.
"""

from __future__ import annotations

import dataclasses
import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.upbit_intraday_shadow import contracts as K
from app.services.upbit_intraday_shadow import writer
from research.upbit_intraday import rules as R

REPO = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO / "alembic" / "versions" / "20261125_hk6036_upbit_shadow.py"
CANDIDATES = "research.upbit_shadow_candidates"
TRADES = "research.upbit_shadow_virtual_trades"
OPEN_AT = datetime(2026, 11, 25, 1, 30, tzinfo=UTC)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _captured(step: str) -> list[str]:
    migration = _load(f"hk6036_{step}")
    captured: list[str] = []
    migration.op = SimpleNamespace(execute=lambda sql: captured.append(str(sql)))
    getattr(migration, step)()
    return captured


# ---------------------------------------------------------------- static ----


@pytest.mark.unit
def test_revision_chain_and_length() -> None:
    migration = _load("hk6036_chain")
    assert migration.down_revision == "20261125_hk6322_us_venue_val"
    assert len(migration.revision) <= 32


@pytest.mark.unit
def test_vocabularies_match_the_code() -> None:
    migration = _load("hk6036_vocab")
    assert migration.FAMILIES == R.FAMILIES
    assert migration.BUCKETS == R.BUCKET_NAMES
    assert migration.HORIZONS == K.HORIZONS
    assert migration.DISPOSITIONS == K.DISPOSITIONS
    assert migration.EXCLUSION_REASONS == K.EXCLUSION_REASONS
    assert migration.ENTRY_SOURCES == K.ENTRY_SOURCES
    assert migration.EXIT_BASES == K.EXIT_BASES
    assert migration.MISSING_REASONS == K.MISSING_REASONS


@pytest.mark.unit
def test_conditional_insert_only_grants() -> None:
    sql = "\n".join(_captured("upgrade"))
    assert "rolname = 'at_app'" in sql
    assert f"GRANT SELECT, INSERT ON {CANDIDATES} TO at_app" in sql
    assert f"GRANT SELECT, INSERT ON {TRADES} TO at_app" in sql
    grants = [line for line in sql.splitlines() if "GRANT " in line]
    assert len(grants) == 3
    assert not any(w in line for line in grants for w in ("UPDATE", "DELETE", "ALL"))


@pytest.mark.unit
def test_only_the_two_new_tables_are_touched() -> None:
    sql = "\n".join(_captured("upgrade") + _captured("downgrade"))
    assert "execution_ledger" not in sql
    assert "review." not in sql
    assert "public." not in sql


@pytest.mark.unit
def test_downgrade_locks_before_the_emptiness_check_and_the_drops() -> None:
    captured = _captured("downgrade")
    assert captured[0] == f"LOCK TABLE {CANDIDATES}, {TRADES} IN ACCESS EXCLUSIVE MODE"
    assert "rows exist" in captured[1]
    assert captured[2:] == [f"DROP TABLE {TRADES}", f"DROP TABLE {CANDIDATES}"]


# -------------------------------------------------------------------- DB ----


def _run_step(sync_conn, step: str) -> None:
    migration = _load(f"hk6036_db_{step}")
    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        getattr(migration, step)()


def candidate(**overrides) -> K.Candidate:
    base = K.Candidate(
        rules_version=K.RULES_VERSION,
        rules_sha256=K.FROZEN_RULES_SHA256,
        market="KRW-SAHARA",
        family="C_downside",
        config_id="C_downside:N16_X3",
        n_bars=16,
        x_multiple=Decimal("3"),
        direction=-1,
        selected_config=False,
        signal_bar_open_at=OPEN_AT,
        signal_time=OPEN_AT + timedelta(minutes=15),
        first_received_at=OPEN_AT + timedelta(minutes=16),
        disposition="excluded",
        exclusion_reasons=("market_event_caution",),
        open_price=Decimal("7.85"),
        high_price=Decimal("8.02"),
        low_price=Decimal("7.47"),
        close_price=Decimal("7.60"),
        quote_volume=Decimal("1523800000.12345678"),
        ref_level=Decimal("7.69"),
        breakout_distance=Decimal("-0.011703511053"),
        median_quote_volume=Decimal("75700000"),
        volume_ratio=Decimal("20.123733719772"),
        quote_volume_24h=Decimal("60000000000"),
        liquidity_bucket="L3",
        fee_rate_per_side=K.FEE_RATE_PER_SIDE,
        roundtrip_cost_rate=Decimal("0.002"),
        signal_tick_krw=Decimal("0.01"),
        market_event_state="caution:TRADING_VOLUME_SOARING",
        market_event_caution_types=("TRADING_VOLUME_SOARING",),
        market_event_observed_at=OPEN_AT + timedelta(minutes=16),
    )
    return dataclasses.replace(base, **overrides)


def trade(candidate_id: int, horizon: int, **overrides) -> K.VirtualTrade:
    base = K.VirtualTrade(
        candidate_id=candidate_id,
        market="KRW-SAHARA",
        config_id="C_downside:N16_X3",
        disposition="excluded",
        horizon_bars=horizon,
        entry_bar_open_at=OPEN_AT + timedelta(minutes=15),
        exit_bar_open_at=OPEN_AT + timedelta(minutes=15 * horizon),
        entry_source="next_bar_open",
        virtual_entry_price=Decimal("7.62"),
        exit_basis="exit_bar_close",
        virtual_exit_price=Decimal("7.23"),
        entry_tick_krw=Decimal("0.01"),
        exit_tick_krw=Decimal("0.01"),
        gross_return=Decimal("0.051181102362"),
        roundtrip_cost_rate=Decimal("0.002"),
        net_return=Decimal("0.049181102362"),
        mae=Decimal("0.010498687664"),
        mfe=Decimal("0.065616797900"),
        missing_reason=None,
        outcome_received_at=OPEN_AT + timedelta(hours=2),
    )
    return dataclasses.replace(base, **overrides)


NO_NUMBERS = dict.fromkeys(
    (
        "entry_source",
        "virtual_entry_price",
        "exit_basis",
        "virtual_exit_price",
        "entry_tick_krw",
        "exit_tick_krw",
        "gross_return",
        "net_return",
        "mae",
        "mfe",
    )
)


@pytest_asyncio.fixture
async def conn():
    from app.core.db import engine
    from tests._run_owned_database import validate_run_owned_database_url

    validate_run_owned_database_url(engine.url)
    async with engine.connect() as connection:
        try:
            await connection.execute(text("CREATE SCHEMA IF NOT EXISTS research"))
            await connection.execute(text(f"DROP TABLE IF EXISTS {TRADES} CASCADE"))
            await connection.execute(text(f"DROP TABLE IF EXISTS {CANDIDATES} CASCADE"))
            yield connection
        finally:
            await connection.rollback()


async def _rejected(session: AsyncSession, action) -> bool:
    try:
        async with session.begin_nested():
            await action()
    except (IntegrityError, DBAPIError):
        return True
    return False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_round_trip_writer_checks_and_downgrade_refusal(conn) -> None:
    await conn.run_sync(_run_step, "upgrade")
    # The writer never commits; the outer transaction is rolled back.
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")

    first = candidate()
    clean = candidate(
        market="KRW-XRP",
        disposition="signal",
        exclusion_reasons=(),
        market_event_state="none",
        market_event_caution_types=(),
    )
    assert await writer.insert_candidates(session, [first, clean], run_id="a") == 2
    later = dataclasses.replace(
        first, first_received_at=first.first_received_at + timedelta(minutes=15)
    )
    assert await writer.insert_candidates(session, [later], run_id="b") == 0
    kept = (
        await session.execute(
            text(
                f"SELECT first_received_at, run_id FROM {CANDIDATES} "
                "WHERE market = 'KRW-SAHARA'"
            )
        )
    ).one()
    assert tuple(kept) == (first.first_received_at, "a")

    pending = {p.market: p for p in await writer.load_pending(session)}
    assert set(pending) == {"KRW-SAHARA", "KRW-XRP"}
    assert pending["KRW-SAHARA"].resolved_horizons == frozenset()
    assert [p.market for p in await writer.load_pending(session, ["KRW-XRP"])] == [
        "KRW-XRP"
    ]
    held = pending["KRW-SAHARA"].candidate_id

    assert (
        await writer.insert_virtual_trades(session, [trade(held, 4)], run_id="a") == 1
    )
    assert (
        await writer.insert_virtual_trades(
            session, [trade(held, 4, net_return=Decimal("9"))], run_id="b"
        )
        == 0
    )
    missing = trade(held, 16, missing_reason="next_bar_no_trades", **NO_NUMBERS)
    assert await writer.insert_virtual_trades(session, [missing], run_id="a") == 1
    (still,) = [p for p in await writer.load_pending(session) if p.candidate_id == held]
    assert still.resolved_horizons == frozenset({4, 16})
    await writer.insert_virtual_trades(session, [trade(held, 96)], run_id="a")
    assert [p.market for p in await writer.load_pending(session)] == ["KRW-XRP"]

    other = pending["KRW-XRP"].candidate_id
    bad_trades = [
        trade(other, 4),  # disposition differs from the candidate's
        trade(other, 5, disposition="signal"),
        trade(other, 4, disposition="signal", missing_reason="next_bar_no_trades"),
        trade(other, 4, disposition="signal", virtual_entry_price=None),
        trade(other, 4, disposition="signal", entry_source="price_touch"),
        trade(other, 4, disposition="signal", missing_reason="touched", **NO_NUMBERS),
    ]
    for bad in bad_trades:
        assert await _rejected(
            session,
            lambda bad=bad: writer.insert_virtual_trades(session, [bad], run_id="x"),
        ), bad
    assert await _rejected(
        session,
        lambda: session.execute(
            text(
                f"INSERT INTO {TRADES} (candidate_id, disposition, is_virtual, "
                "horizon_bars, entry_bar_open_at, exit_bar_open_at, "
                "roundtrip_cost_rate, missing_reason, outcome_received_at, run_id) "
                "VALUES (:id, 'signal', false, 4, now(), now(), 0, "
                "'market_unavailable', now(), 'x')"
            ),
            {"id": other},
        ),
    )

    bad_candidates = [
        candidate(market="KRW-A", exclusion_reasons=()),  # excluded without reason
        candidate(market="KRW-B", disposition="signal"),  # signal with a reason
        candidate(market="KRW-C", exclusion_reasons=("too_hot",)),
        candidate(market="KRW-D", first_received_at=OPEN_AT),  # before the close
        candidate(market="KRW-E", signal_time=OPEN_AT + timedelta(minutes=30)),
        candidate(market="KRW-F", market_event_state="caution:"),
        candidate(market="KRW-G", family="D_other"),
    ]
    for bad in bad_candidates:
        assert await _rejected(
            session,
            lambda bad=bad: writer.insert_candidates(session, [bad], run_id="x"),
        ), bad

    with pytest.raises(DBAPIError, match="rows exist"):
        async with session.begin_nested():
            await (await session.connection()).run_sync(_run_step, "downgrade")

    await session.execute(text(f"DELETE FROM {TRADES}"))
    with pytest.raises(DBAPIError, match="rows exist"):
        async with session.begin_nested():
            await (await session.connection()).run_sync(_run_step, "downgrade")
    await session.execute(text(f"DELETE FROM {CANDIDATES}"))
    await session.commit()
    await session.close()
    await conn.run_sync(_run_step, "downgrade")
    for table in (CANDIDATES, TRADES):
        assert (
            await conn.execute(text(f"SELECT to_regclass('{table}')"))
        ).scalar_one() is None
    await conn.run_sync(_run_step, "upgrade")  # and back up again
