"""ROB-1152 regression: execution_asset_class must survive the ledger insert.

``ApprovalProvenance`` used to include an ``execution_asset_class`` field that
every real call site left at its default (``None``). ``claim_submit``,
``reserve_sell_and_claim``, and ``record_preview`` all built their insert
``values`` dict with the explicit ``execution_asset_class`` keyword argument
set FIRST and then spread ``self._build_provenance_values(prov)`` LAST — so
the default provenance's ``None`` silently overwrote the caller's explicit
value on every single insert. Every alpaca_paper_order_ledger row ever
written carried ``execution_asset_class = NULL`` regardless of what callers
passed in.

These tests exercise the real DB insert path directly (no mocks) so a
regression is caught by the actual persisted column, not by inspecting the
values dict a mock captured.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import AlpacaPaperOrderLedger
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_PREFIX = "rob1152-"


def _uniq(suffix: str = "") -> str:
    return f"{_PREFIX}{uuid.uuid4().hex[:12]}{suffix}"


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession):
    yield
    await db_session.execute(
        delete(AlpacaPaperOrderLedger).where(
            AlpacaPaperOrderLedger.client_order_id.like(f"{_PREFIX}%")
        )
    )
    await db_session.commit()


async def test_claim_submit_persists_execution_asset_class_us_equity(
    db_session: AsyncSession,
):
    ledger = AlpacaPaperLedgerService(db_session)
    coid = _uniq("-buy-equity")
    claim = await ledger.claim_submit(
        client_order_id=coid,
        execution_symbol="AAPL",
        execution_venue="alpaca_paper",
        execution_asset_class="us_equity",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        requested_qty=Decimal("1"),
        requested_price=Decimal("150.00"),
    )
    assert claim.won is True
    assert claim.row is not None
    assert claim.row.execution_asset_class == "us_equity"


async def test_claim_submit_persists_execution_asset_class_crypto(
    db_session: AsyncSession,
):
    ledger = AlpacaPaperLedgerService(db_session)
    coid = _uniq("-buy-crypto")
    claim = await ledger.claim_submit(
        client_order_id=coid,
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        execution_asset_class="crypto",
        instrument_type=InstrumentType.crypto,
        side="buy",
        requested_qty=Decimal("0.01"),
        requested_price=Decimal("60000.00"),
    )
    assert claim.won is True
    assert claim.row is not None
    assert claim.row.execution_asset_class == "crypto"


async def test_reserve_sell_and_claim_persists_execution_asset_class(
    db_session: AsyncSession,
):
    ledger = AlpacaPaperLedgerService(db_session)
    coid = _uniq("-sell-equity")
    claim = await ledger.reserve_sell_and_claim(
        client_order_id=coid,
        execution_symbol="AAPL",
        execution_venue="alpaca_paper",
        execution_asset_class="us_equity",
        instrument_type=InstrumentType.equity_us,
        requested_qty=Decimal("1"),
        position_qty=Decimal("5"),
        position_available=Decimal("5"),
    )
    assert claim.won is True
    assert claim.row is not None
    assert claim.row.execution_asset_class == "us_equity"


async def test_record_preview_persists_execution_asset_class(
    db_session: AsyncSession,
):
    ledger = AlpacaPaperLedgerService(db_session)
    coid = _uniq("-preview")
    row = await ledger.record_preview(
        client_order_id=coid,
        execution_symbol="ETH/USD",
        execution_venue="alpaca_paper",
        execution_asset_class="crypto",
        instrument_type=InstrumentType.crypto,
        side="buy",
    )
    assert row.execution_asset_class == "crypto"


async def test_record_submit_carries_execution_asset_class_from_claim(
    db_session: AsyncSession,
):
    """record_submit copies execution_asset_class from the claimed source row."""
    ledger = AlpacaPaperLedgerService(db_session)
    coid = _uniq("-submit")
    await ledger.claim_submit(
        client_order_id=coid,
        execution_symbol="AAPL",
        execution_venue="alpaca_paper",
        execution_asset_class="us_equity",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        requested_qty=Decimal("1"),
        requested_price=Decimal("150.00"),
    )
    updated = await ledger.record_submit(
        coid,
        {"id": "broker-order-1", "status": "accepted"},
    )
    assert updated.execution_asset_class == "us_equity"
