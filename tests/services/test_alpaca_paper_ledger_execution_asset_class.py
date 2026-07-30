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


# ---------------------------------------------------------------------------
# ROB-1152 review finding #2: record_plan/record_validation_attempt/
# record_sell_validation had NO first-class execution_asset_class parameter
# before this fix -- they could only receive it via the now-removed
# ApprovalProvenance.execution_asset_class field. These tests lock in that
# the contract was preserved (widened), not narrowed: each method now takes
# execution_asset_class explicitly and persists it, exactly like
# claim_submit/reserve_sell_and_claim/record_preview.
# ---------------------------------------------------------------------------


async def test_record_plan_persists_execution_asset_class(
    db_session: AsyncSession,
):
    ledger = AlpacaPaperLedgerService(db_session)
    coid = _uniq("-plan")
    row = await ledger.record_plan(
        client_order_id=coid,
        execution_symbol="AAPL",
        execution_venue="alpaca_paper",
        execution_asset_class="us_equity",
        instrument_type=InstrumentType.equity_us,
        side="buy",
    )
    assert row.execution_asset_class == "us_equity"


async def test_record_validation_attempt_persists_execution_asset_class(
    db_session: AsyncSession,
):
    ledger = AlpacaPaperLedgerService(db_session)
    coid = _uniq("-validation")
    row = await ledger.record_validation_attempt(
        client_order_id=coid,
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        execution_asset_class="crypto",
        instrument_type=InstrumentType.crypto,
        side="buy",
        validation_outcome="passed",
    )
    assert row.execution_asset_class == "crypto"


async def test_record_sell_validation_persists_execution_asset_class(
    db_session: AsyncSession,
):
    ledger = AlpacaPaperLedgerService(db_session)
    coid = _uniq("-sellval")
    row = await ledger.record_sell_validation(
        client_order_id=coid,
        execution_symbol="AAPL",
        execution_venue="alpaca_paper",
        execution_asset_class="us_equity",
        instrument_type=InstrumentType.equity_us,
    )
    assert row.execution_asset_class == "us_equity"


# ---------------------------------------------------------------------------
# ROB-1152 review finding #1: the backfill must go through the service layer
# (ORM update(...).values(...)), never a migration op.execute("UPDATE ...")
# raw SQL string. These tests exercise the actual backfill method against
# the real DB.
# ---------------------------------------------------------------------------


async def test_backfill_dry_run_reports_counts_without_writing(
    db_session: AsyncSession,
):
    ledger = AlpacaPaperLedgerService(db_session)
    coid_equity = _uniq("-backfill-equity")
    coid_crypto = _uniq("-backfill-crypto")

    # Seed two NULL rows directly via claim_submit with execution_asset_class
    # omitted (simulating the pre-fix persisted state / an old row).
    db_session.add(
        AlpacaPaperOrderLedger(
            client_order_id=coid_equity,
            lifecycle_correlation_id=coid_equity,
            record_kind="execution",
            broker="alpaca",
            account_mode="alpaca_paper",
            lifecycle_state="submitted",
            execution_symbol="AAPL",
            execution_venue="alpaca_paper",
            execution_asset_class=None,
            instrument_type=InstrumentType.equity_us,
            side="buy",
            order_type="limit",
            currency="USD",
            requested_qty=Decimal("1"),
            confirm_flag=True,
        )
    )
    db_session.add(
        AlpacaPaperOrderLedger(
            client_order_id=coid_crypto,
            lifecycle_correlation_id=coid_crypto,
            record_kind="execution",
            broker="alpaca",
            account_mode="alpaca_paper",
            lifecycle_state="submitted",
            execution_symbol="BTC/USD",
            execution_venue="alpaca_paper",
            execution_asset_class=None,
            instrument_type=InstrumentType.crypto,
            side="buy",
            order_type="limit",
            currency="USD",
            requested_qty=Decimal("0.01"),
            confirm_flag=True,
        )
    )
    await db_session.commit()

    result = await ledger.backfill_execution_asset_class_from_instrument_type(
        dry_run=True
    )
    assert result["dry_run"] is True
    assert result["by_asset_class"]["us_equity"] >= 1
    assert result["by_asset_class"]["crypto"] >= 1

    # Dry-run must not have written anything.
    for coid in (coid_equity, coid_crypto):
        row = await ledger.get_execution_by_client_order_id(coid)
        assert row is not None
        assert row.execution_asset_class is None


async def test_backfill_apply_updates_only_null_rows_via_orm(
    db_session: AsyncSession,
):
    ledger = AlpacaPaperLedgerService(db_session)
    coid_null = _uniq("-backfill-apply-null")
    coid_populated = _uniq("-backfill-apply-populated")

    db_session.add(
        AlpacaPaperOrderLedger(
            client_order_id=coid_null,
            lifecycle_correlation_id=coid_null,
            record_kind="execution",
            broker="alpaca",
            account_mode="alpaca_paper",
            lifecycle_state="submitted",
            execution_symbol="AAPL",
            execution_venue="alpaca_paper",
            execution_asset_class=None,
            instrument_type=InstrumentType.equity_us,
            side="buy",
            order_type="limit",
            currency="USD",
            requested_qty=Decimal("1"),
            confirm_flag=True,
        )
    )
    # A row that already has a value must be left untouched (not overwritten
    # with a re-derived value, even if it happened to match).
    db_session.add(
        AlpacaPaperOrderLedger(
            client_order_id=coid_populated,
            lifecycle_correlation_id=coid_populated,
            record_kind="execution",
            broker="alpaca",
            account_mode="alpaca_paper",
            lifecycle_state="submitted",
            execution_symbol="BTC/USD",
            execution_venue="alpaca_paper",
            execution_asset_class="crypto",
            instrument_type=InstrumentType.crypto,
            side="buy",
            order_type="limit",
            currency="USD",
            requested_qty=Decimal("0.01"),
            confirm_flag=True,
        )
    )
    await db_session.commit()

    result = await ledger.backfill_execution_asset_class_from_instrument_type(
        dry_run=False
    )
    assert result["dry_run"] is False

    db_session.expire_all()
    null_row = await ledger.get_execution_by_client_order_id(coid_null)
    assert null_row is not None
    assert null_row.execution_asset_class == "us_equity"

    populated_row = await ledger.get_execution_by_client_order_id(coid_populated)
    assert populated_row is not None
    assert populated_row.execution_asset_class == "crypto"
