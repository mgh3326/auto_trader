"""ROB-844 — serialize + clean up the Demo scalping executor tests.

Every ``confirm=True`` executor run now goes through ``reserve_root_planned``,
which *commits* the planned root so the exposure claim is durable and visible
across processes. Two consequences for these tests:

1. Those committed open-root rows are counted table-wide by the global open-root
   cap, so a reservation concurrency test that asserts "global cap N admits
   exactly one" would race any executor committing an open root on another
   ``--dist=loadfile`` xdist worker (the shared-test_db hazard, ROB-842).
2. The rest of the lifecycle (previewed → reconciled) is written on the shared
   ``db_session`` and is NOT committed, so it rolls back on session close and
   leaves a committed ``planned`` root behind. Re-running an executor test with
   a fixed symbol would then fail closed on its own leftover reservation.

Holding the shared ``binance_demo_reservation_lock`` for every test serializes
the open-root-committing family against itself and against
``tests/services/brokers/binance/demo/test_root_reservation*.py`` (which apply
the same lock). Under that lock we also delete the executor-committed residue
(``rob307-*`` / ``rob844exec-*`` client-order ids) before and after each test, so
isolation is restored without racing other workers. The lock key is distinct
from the production reservation advisory lock, so it never blocks the code under
test.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import delete, or_

from app.core.db import AsyncSessionLocal
from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.scalp_trade_analytics import ScalpTradeAnalytics

_RESIDUE_CID_PREFIXES = ("rob307-%", "rob844exec-%", "rob845r-%", "rob845c-%")


async def _purge_executor_residue() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(ScalpTradeAnalytics).where(
                or_(
                    *[
                        ScalpTradeAnalytics.open_client_order_id.like(prefix)
                        for prefix in _RESIDUE_CID_PREFIXES
                    ]
                )
            )
        )
        await db.execute(
            delete(BinanceDemoOrderLedger).where(
                or_(
                    *[
                        BinanceDemoOrderLedger.client_order_id.like(prefix)
                        for prefix in _RESIDUE_CID_PREFIXES
                    ]
                )
            )
        )
        await db.commit()


@pytest_asyncio.fixture(autouse=True)
async def _serialize_binance_demo_committers(binance_demo_reservation_lock):
    await _purge_executor_residue()
    yield
    await _purge_executor_residue()
