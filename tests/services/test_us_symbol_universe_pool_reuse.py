"""ROB-1487: repeated universe lookups must reuse the shared pooled connection.

Sentry showed 67 ``connect`` spans/day at p95 877 ms in the MCP server — fresh
physical connections instead of pool reuse. The per-symbol resolver opens its
own ``AsyncSessionLocal`` when no session is passed, so every symbol in a
fan-out checked out (and sometimes overflowed) the pool. This test proves the
production engine configuration keeps ONE physical connection across N
sequential lookups.

Database-backed: CI only (the local brief forbids starting a database server).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import delete, event, insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.db import build_engine
from app.models.us_symbol_universe import USSymbolUniverse
from app.services import us_symbol_universe_service as universe

pytestmark = [pytest.mark.integration]

_TEST_SYMBOL = "ZZROB1"


@pytest.mark.asyncio
async def test_n_universe_lookups_reuse_one_pooled_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # conftest forces DB_POOL_CLASS=null for the ambient test env; exercise the
    # queue-pool configuration production actually runs.
    monkeypatch.delenv("DB_POOL_CLASS", raising=False)
    engine = build_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    connects = 0

    @event.listens_for(engine.sync_engine, "connect")
    def _count_connect(_dbapi_connection: object, _record: object) -> None:
        nonlocal connects
        connects += 1

    created = False
    try:
        async with sessions() as session:
            result = await session.execute(
                insert(USSymbolUniverse)
                .values(
                    symbol=_TEST_SYMBOL,
                    exchange="NASD",
                    name_kr="",
                    name_en="",
                    is_active=True,
                )
                .on_conflict_do_nothing(index_elements=["symbol"])
                .returning(USSymbolUniverse.symbol)
            )
            created = result.scalar_one_or_none() is not None
            await session.commit()

        monkeypatch.setattr(universe, "AsyncSessionLocal", sessions)

        # The seeding session already opened the pool's one connection.
        assert connects == 1

        for _ in range(6):
            assert await universe.get_us_exchange_by_symbol(_TEST_SYMBOL) == "NASD"

        assert connects == 1, "each lookup must reuse the pooled connection"
    finally:
        if created:
            async with sessions() as session:
                await session.execute(
                    delete(USSymbolUniverse).where(
                        USSymbolUniverse.symbol == _TEST_SYMBOL
                    )
                )
                await session.commit()
        await engine.dispose()
