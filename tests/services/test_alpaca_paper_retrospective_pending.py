"""ROB-954 — Alpaca paper ledger source branch in build_retrospective_pending.

trade_retrospective_pending never surfaced alpaca_paper_order_ledger terminal
rows (no scan branch existed), so fills booked by the ROB-953 reconcile
service and the ROB-994 zero-fill terminalization sat invisible to the
retrospective due-list forever. This exercises the new branch: terminal
lifecycle_state rows surface (filled/position_reconciled/closed/
final_reconciled/anomaly by default, canceled only with include_cancelled),
non-terminal states and non-execution record_kinds stay excluded, coverage
still applies, and the scan is properly isolated from every other source
(especially `paper` / PaperTrade, a structurally disjoint writer — see
ROB-954 PR notes for the grep evidence that no code path writes both).

This file's name contains "alpaca_paper" so conftest's
`_serialize_alpaca_paper_db_suites` automatically cross-worker-locks it
against every other alpaca_paper suite sharing this table.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.paper_trading import PaperTrade
from app.models.review import AlpacaPaperOrderLedger
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
from app.services.paper_trading_service import PaperTradingService
from app.services.trade_journal.trade_retrospective_service import (
    build_retrospective_pending,
    save_retrospective,
)

pytestmark = [pytest.mark.asyncio]

_PREFIX = "rob954-"


def _uniq(suffix: str = "") -> str:
    return f"{_PREFIX}{uuid.uuid4().hex[:12]}{suffix}"


async def _pending_with_retry(db: Any, **kwargs: Any) -> Any:
    """Retry on deadlock — the scan also touches the live order ledgers, which
    can deadlock with parallel live-ledger suites under xdist (shared test DB).
    """
    from sqlalchemy.exc import DBAPIError

    last: Exception | None = None
    for _ in range(6):
        try:
            return await build_retrospective_pending(db, **kwargs)
        except DBAPIError as exc:
            if "deadlock" not in str(exc).lower():
                raise
            last = exc
            await db.rollback()
    assert last is not None
    raise last


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession):
    async def _wipe() -> None:
        await db_session.execute(
            delete(AlpacaPaperOrderLedger).where(
                AlpacaPaperOrderLedger.client_order_id.like(f"{_PREFIX}%")
            )
        )
        await db_session.execute(
            delete(PaperTrade).where(PaperTrade.correlation_id.like(f"{_PREFIX}%"))
        )
        await db_session.commit()

    await _wipe()
    yield
    await _wipe()


def _row(
    *,
    client_order_id: str,
    lifecycle_state: str,
    record_kind: str = "execution",
    side: str = "buy",
    symbol: str = "ISRG",
    instrument_type: InstrumentType = InstrumentType.equity_us,
    lifecycle_correlation_id: str | None = None,
) -> AlpacaPaperOrderLedger:
    return AlpacaPaperOrderLedger(
        client_order_id=client_order_id,
        lifecycle_correlation_id=lifecycle_correlation_id or client_order_id,
        record_kind=record_kind,
        broker="alpaca",
        account_mode="alpaca_paper",
        lifecycle_state=lifecycle_state,
        execution_symbol=symbol,
        execution_venue="alpaca_paper",
        instrument_type=instrument_type,
        side=side,
        order_type="limit",
        currency="USD",
        requested_qty=Decimal("1"),
    )


async def _find(result: dict[str, Any], coid: str) -> dict[str, Any] | None:
    return next((p for p in result["pending"] if p["order_ref"] == coid), None)


# ---------------------------------------------------------------------------
# AC1/AC2 — terminal lifecycle states surface, non-terminal stay excluded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lifecycle_state",
    ["filled", "position_reconciled", "closed", "final_reconciled", "anomaly"],
)
async def test_default_terminal_states_surface_as_pending(
    db_session: AsyncSession, lifecycle_state: str
) -> None:
    coid = _uniq()
    db_session.add(_row(client_order_id=coid, lifecycle_state=lifecycle_state))
    await db_session.commit()

    result = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    entry = await _find(result, coid)
    assert entry is not None, result["pending"]
    assert entry["ledger"] == "alpaca_paper"
    assert entry["status"] == lifecycle_state
    assert entry["account_mode"] == "alpaca_paper"


async def test_canceled_hidden_unless_included(db_session: AsyncSession) -> None:
    coid = _uniq()
    db_session.add(_row(client_order_id=coid, lifecycle_state="canceled"))
    await db_session.commit()

    default = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    assert await _find(default, coid) is None

    included = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
        include_cancelled=True,
    )
    entry = await _find(included, coid)
    assert entry is not None
    assert entry["status"] == "canceled"


@pytest.mark.parametrize(
    "lifecycle_state", ["planned", "previewed", "validated", "submitted"]
)
async def test_non_terminal_states_excluded(
    db_session: AsyncSession, lifecycle_state: str
) -> None:
    coid = _uniq()
    db_session.add(_row(client_order_id=coid, lifecycle_state=lifecycle_state))
    await db_session.commit()

    result = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    assert await _find(result, coid) is None


async def test_non_execution_record_kind_excluded_even_with_terminal_state(
    db_session: AsyncSession,
) -> None:
    """record_kind != 'execution' rows share the (client_order_id, record_kind)
    unique-slot family with the real execution row — scanning them too would
    double-surface a single order as multiple due-list entries. lifecycle_state
    is deliberately set to a terminal value here to isolate the record_kind
    filter from the lifecycle_state filter (not a realistic broker combo)."""
    coid = _uniq()
    for record_kind in ("plan", "preview", "validation_attempt", "reconcile"):
        db_session.add(
            _row(
                client_order_id=f"{coid}-{record_kind}",
                lifecycle_state="filled",
                record_kind=record_kind,
            )
        )
    await db_session.commit()

    result = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    refs = {p["order_ref"] for p in result["pending"]}
    assert not any(ref.startswith(coid) for ref in refs if ref)


# ---------------------------------------------------------------------------
# AC3 — isolation from other account_mode sources (especially `paper`)
# ---------------------------------------------------------------------------


async def test_account_mode_filter_isolates_from_paper(
    db_session: AsyncSession,
) -> None:
    coid = _uniq()
    db_session.add(_row(client_order_id=coid, lifecycle_state="filled"))
    await db_session.commit()

    only_alpaca = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    assert await _find(only_alpaca, coid) is not None
    assert all(p["account_mode"] == "alpaca_paper" for p in only_alpaca["pending"])

    only_paper = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="paper",
    )
    assert await _find(only_paper, coid) is None
    assert all(p["ledger"] == "paper_trades" for p in only_paper["pending"])


async def test_account_mode_none_scans_alpaca_and_paper_without_duplication(
    db_session: AsyncSession,
) -> None:
    """AC5 — PaperTrade and AlpacaPaperOrderLedger are structurally disjoint
    writers (no function writes to both; see PR notes for the grep evidence),
    so a combined account_mode=None scan must surface both as distinct
    entries, never collapsed into one and never double-counted."""
    coid = _uniq()
    db_session.add(_row(client_order_id=coid, lifecycle_state="filled", symbol="MSFT"))
    await db_session.commit()

    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("acct"), initial_capital_krw=Decimal("100000000")
    )
    paper_correlation_id = _uniq("paper")
    db_session.add(
        PaperTrade(
            account_id=acct.id,
            symbol="MSFT",
            instrument_type=InstrumentType.crypto,
            side="buy",
            order_type="market",
            quantity=Decimal("0.01"),
            price=Decimal("50000000"),
            total_amount=Decimal("500000"),
            fee=Decimal("0"),
            currency="KRW",
            correlation_id=paper_correlation_id,
            executed_at=now_kst(),
        )
    )
    await db_session.commit()

    result = await _pending_with_retry(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    alpaca_matches = [p for p in result["pending"] if p["order_ref"] == coid]
    paper_matches = [
        p
        for p in result["pending"]
        if p["suggested_correlation_id"] == paper_correlation_id
    ]
    assert len(alpaca_matches) == 1, result["pending"]
    assert len(paper_matches) == 1, result["pending"]
    assert alpaca_matches[0]["ledger"] == "alpaca_paper"
    assert paper_matches[0]["ledger"] == "paper_trades"
    # Distinct suggested_correlation_id namespaces prove neither entry
    # collapsed onto the other via the coverage-dedup key.
    assert (
        alpaca_matches[0]["suggested_correlation_id"]
        != paper_matches[0]["suggested_correlation_id"]
    )


# ---------------------------------------------------------------------------
# AC6 — coverage exclusion
# ---------------------------------------------------------------------------


async def test_covered_by_correlation_id(db_session: AsyncSession) -> None:
    coid = _uniq()
    db_session.add(_row(client_order_id=coid, lifecycle_state="filled"))
    await db_session.commit()

    result = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    assert await _find(result, coid) is not None

    await save_retrospective(
        db_session,
        symbol="ISRG",
        instrument_type="equity_us",
        account_mode="alpaca_paper",
        outcome="filled",
        correlation_id=coid,
    )
    await db_session.commit()

    covered = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    assert await _find(covered, coid) is None


# ---------------------------------------------------------------------------
# AC4 — a real ROB-953-reconciled fill (claim -> submit -> status booked
# filled) surfaces, mirroring the ISRG rob73-... production evidence.
# ---------------------------------------------------------------------------


async def test_reconciled_fill_via_real_ledger_service_surfaces_as_pending(
    db_session: AsyncSession,
) -> None:
    coid = _uniq()
    ledger = AlpacaPaperLedgerService(db_session)
    claim = await ledger.claim_submit(
        client_order_id=coid,
        execution_symbol="ISRG",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        order_type="limit",
        time_in_force="day",
        requested_qty=Decimal("1"),
        requested_price=Decimal("500"),
    )
    assert claim.won is True

    await ledger.record_submit(coid, {"id": f"broker-{coid}", "status": "new"})

    # Mirrors AlpacaPaperReconcileService._book_transition's write path
    # (ROB-953): a status check whose evidence proves a complete fill books
    # lifecycle_state=filled via the same record_status call.
    await ledger.record_status(
        coid,
        {
            "id": f"broker-{coid}",
            "status": "filled",
            "filled_qty": "1",
            "filled_avg_price": "512.34",
        },
    )

    result = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    entry = await _find(result, coid)
    assert entry is not None, result["pending"]
    assert entry["ledger"] == "alpaca_paper"
    assert entry["status"] == "filled"
    assert entry["symbol"] == "ISRG"
    assert entry["market"] == "us"
    assert entry["instrument_type"] == "equity_us"
    # lifecycle_correlation_id defaults to client_order_id (claim_submit).
    assert entry["suggested_correlation_id"] == coid
