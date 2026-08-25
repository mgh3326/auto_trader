"""The D2 pre-send claim has to survive its author's transaction.

``record_planned`` writes into the caller's transaction and only flushes, which
is right for a caller that owns the whole lifecycle. It is wrong for a writer
whose safety argument is "a restart must see the prior attempt": everything the
caller had not committed disappears when the process dies, so a crash between
the signed POST and the caller's own commit erased the fence.

These tests run against the real database because that is the only place the
distinction exists — an in-memory double cannot tell a flush from a commit.
Each one opens its own session and abandons it without committing, which is
what a dying process leaves behind, and then reads back through a fresh session,
which is what the restarted process sees.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.db import engine
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def instrument_ids(db_session) -> tuple[int, int]:
    """Two committed instrument identities.

    Two, not one, because ``uq_binance_demo_ledger_open_root`` is unique on
    ``(product, instrument_id)`` across the open lifecycle states: a flush-only
    open root and a committed open root for the *same* instrument would have
    the second waiting on the first's transaction lock rather than showing the
    durability difference. The D2 writer never hits that — its three orders are
    three different instruments — and the constraint is a useful extra fence,
    but a test has to stay off it to measure anything.

    Resolved through the service, which commits them in its own transaction; a
    flush-only fixture would be invisible to the claim transaction and the
    insert would fail on the foreign key.
    """

    ledger = BinanceDemoLedgerService(db_session)
    return (
        await ledger.resolve_or_create_instrument(
            venue="binance",
            product="spot",
            venue_symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
        ),
        await ledger.resolve_or_create_instrument(
            venue="binance",
            product="spot",
            venue_symbol="ETHUSDT",
            base_asset="ETH",
            quote_asset="USDT",
        ),
    )


_CID_PREFIX = "d2durable-"


def _cid() -> str:
    return f"{_CID_PREFIX}{uuid.uuid4().hex[:16]}"


@pytest_asyncio.fixture(autouse=True)
async def _drop_committed_rows():
    """Delete what this module commits.

    These claims are committed on purpose — that is the whole property under
    test — so they outlive the usual transaction rollback and would otherwise
    be counted by the neighbouring ``test_ledger_service`` row-counting tests.
    """

    yield
    from sqlalchemy import text

    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(
            text(
                "DELETE FROM binance_demo_order_ledger "
                "WHERE client_order_id LIKE :prefix"
            ),
            {"prefix": f"{_CID_PREFIX}%"},
        )
        await cleanup.commit()


def _claim_kwargs(instrument_id: int, client_order_id: str) -> dict:
    return {
        "instrument_id": instrument_id,
        "product": "spot",
        "venue_host": "demo-api.binance.com",
        "client_order_id": client_order_id,
        "side": "SELL",
        "order_type": "LIMIT",
        "qty": Decimal("0.00015000"),
        "price": Decimal("69266.01000000"),
        "now": dt.datetime.now(dt.UTC),
    }


async def _read_from_a_fresh_session(client_order_id: str) -> str | None:
    """What a restarted process sees: a new session, a new transaction, nothing
    inherited from the writer that died."""

    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as fresh:
        return await BinanceDemoLedgerService(fresh).committed_lifecycle_state(
            client_order_id
        )


@pytest.mark.asyncio
async def test_only_the_committed_claim_survives_the_writers_session_dying(
    instrument_ids: tuple[int, int],
) -> None:
    """One session, two claims — the only difference is how they were written.

    Kept as a single test on purpose: both claims share a session and a
    lifetime, so nothing but the write method can explain the different
    outcomes.
    """

    from app.core.db import AsyncSessionLocal

    committed_instrument, flushed_instrument = instrument_ids
    committed_cid, flushed_cid = _cid(), _cid()
    async with AsyncSessionLocal() as owner:
        ledger = BinanceDemoLedgerService(owner)

        # The old path: written into this transaction, flushed, never committed.
        await ledger.record_planned(**_claim_kwargs(flushed_instrument, flushed_cid))
        # Its author can see it — which is exactly why reading through the owner
        # session would be a useless durability check...
        owned = await ledger.get_by_client_order_id(flushed_cid)
        assert owned is not None and owned.lifecycle_state == "planned"
        # ...and nobody else can.
        assert await ledger.committed_lifecycle_state(flushed_cid) is None

        # The fix: committed in its own transaction, before any broker call.
        await ledger.commit_planned_claim(
            **_claim_kwargs(committed_instrument, committed_cid),
            extra_metadata={"writer": "d2_remediation_single"},
        )
        assert await ledger.committed_lifecycle_state(committed_cid) == "planned"

        # The process dies here: this session never commits.

    # What the restarted process sees.
    assert await _read_from_a_fresh_session(committed_cid) == "planned"
    assert await _read_from_a_fresh_session(flushed_cid) is None
