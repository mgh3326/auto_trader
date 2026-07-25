"""ROB-954 — Alpaca paper ledger source branch in build_retrospective_pending.

trade_retrospective_pending never surfaced alpaca_paper_order_ledger terminal
rows (no scan branch existed), so fills booked by the ROB-953 reconcile
service and the ROB-994 zero-fill terminalization sat invisible to the
retrospective due-list forever. This exercises the new branch: terminal
lifecycle_state rows surface (filled/position_reconciled/closed/
final_reconciled/anomaly by default, canceled only with include_cancelled),
non-terminal states and non-{execution,reconcile} record_kinds stay excluded,
coverage still applies, and the scan is properly isolated from every other
source (especially `paper` / PaperTrade, a structurally disjoint writer — see
ROB-954 PR notes for the grep evidence that no code path writes both).

ROB-954 round-2 (adversarial-review fix pass) added three more behaviors
this file covers: the window anchors on `updated_at` (terminal-transition
time), not `created_at` (claim time); record_kind='reconcile' rows (produced
in-place by record_final_reconcile()) surface alongside 'execution'; and
roundtrip buy/sell legs sharing one lifecycle_correlation_id collapse to a
single due entry.

The terminal window is anchored on the dedicated ``terminalized_at`` transition
timestamp. Legacy terminal rows created before that nullable column existed use
the stable ``created_at`` fallback; metadata-only writes must move neither path.

This file's name contains "alpaca_paper" so conftest's
`_serialize_alpaca_paper_db_suites` automatically cross-worker-locks it
against every other alpaca_paper suite sharing this table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST, now_kst
from app.models.paper_trading import PaperAccount, PaperTrade
from app.models.review import AlpacaPaperOrderLedger
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService
from app.services.brokers.alpaca.schemas import Order
from app.services.paper_trading_service import PaperTradingService
from app.services.trade_journal.trade_retrospective_service import (
    build_retrospective_pending,
    save_retrospective,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

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
        # Prefix-scoped account cleanup closes the leak from the disjoint
        # PaperTrade coverage test without touching unrelated shared-test rows.
        await db_session.execute(
            delete(PaperAccount).where(PaperAccount.name.like(f"{_PREFIX}%"))
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
    """record_kind IN {plan, preview, validation_attempt} rows share the
    (client_order_id, record_kind) unique-slot family with the real execution
    row — scanning them too would double-surface a single order as multiple
    due-list entries. lifecycle_state is deliberately set to a terminal value
    here to isolate the record_kind filter from the lifecycle_state filter
    (not a realistic broker combo). 'reconcile' is intentionally NOT in this
    list — see test_reconcile_record_kind_surfaces_with_final_reconciled_state
    below, since AlpacaPaperLedgerService.record_final_reconcile() genuinely
    produces record_kind='reconcile' in production (an in-place UPDATE of the
    execution row, not a second row)."""
    coid = _uniq()
    for record_kind in ("plan", "preview", "validation_attempt"):
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


async def test_reconcile_record_kind_surfaces_with_final_reconciled_state(
    db_session: AsyncSession,
) -> None:
    """record_kind='reconcile' paired with lifecycle_state='final_reconciled'
    is the realistic combination AlpacaPaperLedgerService.record_final_reconcile()
    produces (it flips record_kind on the same row at the moment it books
    final_reconciled) — this must surface, not be silently dropped by an
    execution-only record_kind filter."""
    coid = _uniq()
    db_session.add(
        _row(
            client_order_id=coid,
            lifecycle_state="final_reconciled",
            record_kind="reconcile",
        )
    )
    await db_session.commit()

    result = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    entry = await _find(result, coid)
    assert entry is not None, result["pending"]
    assert entry["status"] == "final_reconciled"


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
# AC4 — a real ROB-953-reconciled fill, produced by the actual
# AlpacaPaperReconcileService against a fake broker (not a hand-rolled
# record_status() call), surfaces in a narrow today-only window — mirroring
# the ISRG rob73-... production evidence and jointly proving the round-2
# `updated_at` window fix (HIGH-1).
# ---------------------------------------------------------------------------


class _FakeAlpacaBroker:
    """Minimal broker double for AlpacaPaperReconcileService.reconcile().

    Returns a single deterministic filled Order plus its matching fill
    activity, exercising the real classify_fill_evidence/resolve_transition/
    record_status chain that production reconcile uses — no fake ledger, no
    shortcut through record_status directly.
    """

    def __init__(self, order: Order) -> None:
        self._order = order

    async def get_order_by_client_order_id(self, _: str) -> Order:
        return self._order

    async def list_fills(self, **_: Any) -> list[Any]:
        return [
            SimpleNamespace(
                id="f1",
                order_id=self._order.id,
                qty=self._order.filled_qty,
                price=self._order.filled_avg_price,
                cum_qty=self._order.filled_qty,
                transaction_time=None,
            )
        ]


async def test_reconciled_fill_via_real_reconcile_service_surfaces_as_pending(
    db_session: AsyncSession,
) -> None:
    # Realistic client_order_id shape (rob73-<hex16>), matching the production
    # evidence format used in tests/services/test_alpaca_paper_reconcile_service.py
    # (Row.client_order_id default "rob73-08ebbf8c64e2dd93"). Kept under this
    # file's _PREFIX so the autouse cleanup fixture still reclaims it.
    coid = f"{_PREFIX}rob73-{uuid.uuid4().hex[:16]}"
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

    submitted = await ledger.record_submit(
        coid, {"id": f"broker-{coid}", "status": "new"}
    )

    # The claim/submit really happened three days ago. Only the terminal write
    # produced by the real reconcile below happens today; this makes reverting
    # the scanner to created_at fail instead of yielding the round-2 false green.
    stale = now_kst().astimezone(UTC) - timedelta(days=3)
    submitted.created_at = stale
    submitted.updated_at = stale
    await db_session.commit()
    assert submitted.terminalized_at is None

    order = Order(
        id=f"broker-{coid}",
        client_order_id=coid,
        symbol="ISRG",
        qty=Decimal("1"),
        filled_qty=Decimal("1"),
        filled_avg_price=Decimal("512.34"),
        side="buy",
        type="limit",
        time_in_force="day",
        status="filled",
    )
    reconcile_result = await AlpacaPaperReconcileService(
        ledger, _FakeAlpacaBroker(order)
    ).reconcile(client_order_id=coid, dry_run=False)
    assert reconcile_result["reconciled"][0]["action"] == "booked_filled"

    # Narrow, realistic (today-only) window — NOT the 2000-2100 span used
    # elsewhere in this file. A wide window can mask the created_at-vs-
    # terminalized_at bug; this only passes when the actual transition is the
    # window anchor.
    today = now_kst().strftime("%Y-%m-%d")
    result = await _pending_with_retry(
        db_session,
        kst_date_from=today,
        kst_date_to=today,
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


# ---------------------------------------------------------------------------
# HIGH-1 (round-3) — window anchors on terminalized_at (terminal-transition time),
# not created_at (claim time). A row claimed days ago that only becomes
# terminal today must surface in today's narrow window; the old created_at
# anchor silently dropped exactly this shape (the REGN long-stall repro).
# ---------------------------------------------------------------------------


async def test_stale_created_at_with_recent_terminal_transition_surfaces_in_narrow_window(
    db_session: AsyncSession,
) -> None:
    coid = _uniq()
    stale = now_kst().astimezone(UTC) - timedelta(days=3)
    row = _row(client_order_id=coid, lifecycle_state="submitted")
    row.created_at = stale
    row.updated_at = stale
    db_session.add(row)
    await db_session.commit()

    # Sanity: the setup is genuinely stale before the transition below.
    assert row.created_at.date() < now_kst().date()

    # Real terminal transition today stamps terminalized_at exactly once;
    # created_at is untouched by the UPDATE and stays stale forever.
    ledger = AlpacaPaperLedgerService(db_session)
    await ledger.record_status(
        coid,
        {
            "id": f"broker-{coid}",
            "status": "filled",
            "filled_qty": "1",
            "filled_avg_price": "500",
        },
    )

    today = now_kst().strftime("%Y-%m-%d")
    narrow = await _pending_with_retry(
        db_session,
        kst_date_from=today,
        kst_date_to=today,
        account_mode="alpaca_paper",
    )
    entry = await _find(narrow, coid)
    assert entry is not None, narrow["pending"]
    assert entry["status"] == "filled"
    transitioned = await ledger.get_execution_by_client_order_id(coid)
    assert transitioned is not None
    assert transitioned.terminalized_at is not None


async def test_terminal_window_does_not_move_after_metadata_only_writes(
    db_session: AsyncSession,
) -> None:
    """Round-2 real-DB counterexample: reconcile/cancel metadata updates
    ``updated_at`` but may never re-date an already-terminal execution."""
    coid = _uniq()
    terminalized = now_kst().astimezone(UTC) - timedelta(days=3)
    row = _row(client_order_id=coid, lifecycle_state="filled")
    row.created_at = terminalized - timedelta(days=2)
    row.updated_at = terminalized
    row.terminalized_at = terminalized
    db_session.add(row)
    await db_session.commit()

    ledger = AlpacaPaperLedgerService(db_session)
    await ledger.record_reconcile(coid, reconcile_status="metadata-only")
    # order_status is not canceled, so this is another metadata-only write.
    updated = await ledger.record_cancel(coid, cancel_status="not_requested")
    assert updated.lifecycle_state == "filled"
    assert updated.terminalized_at == terminalized
    assert updated.updated_at > terminalized

    old_day = terminalized.astimezone(KST).strftime("%Y-%m-%d")
    old_window = await _pending_with_retry(
        db_session,
        kst_date_from=old_day,
        kst_date_to=old_day,
        account_mode="alpaca_paper",
    )
    assert await _find(old_window, coid) is not None

    today = now_kst().strftime("%Y-%m-%d")
    today_window = await _pending_with_retry(
        db_session,
        kst_date_from=today,
        kst_date_to=today,
        account_mode="alpaca_paper",
    )
    assert await _find(today_window, coid) is None


async def test_legacy_null_terminal_timestamp_surfaces_without_window_churn(
    db_session: AsyncSession,
) -> None:
    """Pre-migration terminal rows remain visible through created_at fallback.

    The fallback deliberately avoids updated_at: a metadata-only reconcile must
    not move a legacy NULL row from its original window into today's window.
    """
    coid = _uniq()
    created = now_kst().astimezone(UTC) - timedelta(days=3)
    row = _row(client_order_id=coid, lifecycle_state="filled")
    row.created_at = created
    row.updated_at = created
    row.terminalized_at = None
    db_session.add(row)
    await db_session.commit()

    legacy_day = created.astimezone(KST).strftime("%Y-%m-%d")
    before = await _pending_with_retry(
        db_session,
        kst_date_from=legacy_day,
        kst_date_to=legacy_day,
        account_mode="alpaca_paper",
    )
    assert await _find(before, coid) is not None

    ledger = AlpacaPaperLedgerService(db_session)
    updated = await ledger.record_reconcile(coid, reconcile_status="legacy-metadata")
    assert updated.terminalized_at is None

    still_legacy = await _pending_with_retry(
        db_session,
        kst_date_from=legacy_day,
        kst_date_to=legacy_day,
        account_mode="alpaca_paper",
    )
    assert await _find(still_legacy, coid) is not None

    today = now_kst().strftime("%Y-%m-%d")
    moved = await _pending_with_retry(
        db_session,
        kst_date_from=today,
        kst_date_to=today,
        account_mode="alpaca_paper",
    )
    assert await _find(moved, coid) is None


@pytest.mark.parametrize(
    "writer",
    [
        "record_submit",
        "record_submit_failure",
        "record_status",
        "record_cancel",
        "record_position_snapshot",
        "record_close",
        "record_final_reconcile",
    ],
)
async def test_every_execution_terminal_write_stamps_once(
    db_session: AsyncSession, writer: str
) -> None:
    """Every execution terminal writer stamps the first transition and retries
    preserve that exact instant, including terminal-to-terminal paths."""
    coid = _uniq(f"-{writer}")
    row = _row(client_order_id=coid, lifecycle_state="submitted")
    if writer == "record_cancel":
        row.order_status = "canceled"
    db_session.add(row)
    await db_session.commit()
    ledger = AlpacaPaperLedgerService(db_session)

    async def invoke() -> AlpacaPaperOrderLedger:
        if writer == "record_submit":
            return await ledger.record_submit(
                coid,
                {
                    "id": f"broker-{coid}",
                    "status": "filled",
                    "filled_qty": "1",
                    "filled_avg_price": "500",
                },
            )
        if writer == "record_submit_failure":
            return await ledger.record_submit_failure(
                coid, error_summary="deterministic rejection"
            )
        if writer == "record_status":
            result = await ledger.record_status(
                coid,
                {
                    "id": f"broker-{coid}",
                    "status": "filled",
                    "filled_qty": "1",
                    "filled_avg_price": "500",
                },
            )
            assert isinstance(result, AlpacaPaperOrderLedger)
            return result
        if writer == "record_cancel":
            return await ledger.record_cancel(coid, cancel_status="canceled")
        if writer == "record_position_snapshot":
            return await ledger.record_position_snapshot(coid, position=None)
        if writer == "record_close":
            return await ledger.record_close(coid, qty_delta=Decimal("-1"))
        assert writer == "record_final_reconcile"
        return await ledger.record_final_reconcile(coid)

    first = await invoke()
    first_terminalized_at = first.terminalized_at
    assert first_terminalized_at is not None

    repeated = await invoke()
    assert repeated.terminalized_at == first_terminalized_at


async def test_terminal_insert_writers_stamp_at_insert(
    db_session: AsyncSession,
) -> None:
    """Preview/validation anomaly inserts are terminal bookkeeping writes even
    though record_kind filtering deliberately keeps them out of due-list scans."""
    ledger = AlpacaPaperLedgerService(db_session)
    preview = await ledger.record_preview(
        client_order_id=_uniq("-preview-anomaly"),
        execution_symbol="ISRG",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        lifecycle_state="anomaly",
    )
    validation = await ledger.record_validation_attempt(
        client_order_id=_uniq("-validation-anomaly"),
        execution_symbol="ISRG",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        validation_outcome="failed",
    )
    assert preview.terminalized_at is not None
    assert validation.terminalized_at is not None


async def test_zero_fill_terminal_reconcile_stamps_transition(
    db_session: AsyncSession,
) -> None:
    """ROB-994 expired/rejected/canceled zero-fill booking uses record_status,
    so the real reconcile integration must also populate terminalized_at."""
    coid = _uniq("-zero-fill-expired")
    ledger = AlpacaPaperLedgerService(db_session)
    claim = await ledger.claim_submit(
        client_order_id=coid,
        execution_symbol="ISRG",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        requested_qty=Decimal("1"),
    )
    assert claim.won is True
    await ledger.record_submit(coid, {"id": f"broker-{coid}", "status": "new"})
    expired = Order(
        id=f"broker-{coid}",
        client_order_id=coid,
        symbol="ISRG",
        qty=Decimal("1"),
        filled_qty=Decimal("0"),
        filled_avg_price=None,
        side="buy",
        type="limit",
        time_in_force="day",
        status="expired",
    )
    result = await AlpacaPaperReconcileService(
        ledger, _FakeAlpacaBroker(expired)
    ).reconcile(client_order_id=coid, dry_run=False)
    assert result["reconciled"][0]["action"] == "booked_anomaly"
    persisted = await ledger.get_execution_by_client_order_id(coid)
    assert persisted is not None
    assert persisted.lifecycle_state == "anomaly"
    assert persisted.terminalized_at is not None


# ---------------------------------------------------------------------------
# HIGH-3 (round-3) — buy/sell roundtrip legs sharing one
# lifecycle_correlation_id collapse to a single due entry, since
# review.trade_retrospectives enforces UNIQUE(correlation_id, account_mode)
# and one saved retrospective covers both legs at once.
# ---------------------------------------------------------------------------


async def test_shared_correlation_id_roundtrip_collapses_to_one_pending_entry(
    db_session: AsyncSession,
) -> None:
    correlation_id = _uniq("-roundtrip")
    buy_coid = _uniq("-buy")
    sell_coid = _uniq("-sell")
    transitioned = now_kst().astimezone(UTC) - timedelta(hours=1)
    buy = _row(
        client_order_id=buy_coid,
        lifecycle_state="filled",
        side="buy",
        lifecycle_correlation_id=correlation_id,
    )
    buy.terminalized_at = transitioned
    sell = _row(
        client_order_id=sell_coid,
        lifecycle_state="closed",
        side="sell",
        lifecycle_correlation_id=correlation_id,
    )
    sell.terminalized_at = transitioned
    db_session.add_all([buy, sell])
    await db_session.commit()

    result = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    matches = [
        p for p in result["pending"] if p["suggested_correlation_id"] == correlation_id
    ]
    assert len(matches) == 1, result["pending"]
    assert matches[0]["order_ref"] == sell_coid
    assert matches[0]["side"] == "sell"
    assert matches[0]["status"] == "closed"

    # A later metadata write on the buy leg changes updated_at only. The
    # representative must remain the deterministic sell/close leg.
    await AlpacaPaperLedgerService(db_session).record_reconcile(
        buy_coid, reconcile_status="metadata-only"
    )
    after_metadata = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    after_matches = [
        p
        for p in after_metadata["pending"]
        if p["suggested_correlation_id"] == correlation_id
    ]
    assert len(after_matches) == 1, after_metadata["pending"]
    assert after_matches[0]["order_ref"] == sell_coid
    assert after_matches[0]["side"] == "sell"
    assert after_matches[0]["status"] == "closed"

    await save_retrospective(
        db_session,
        symbol="ISRG",
        instrument_type="equity_us",
        account_mode="alpaca_paper",
        outcome="filled",
        correlation_id=correlation_id,
    )
    await db_session.commit()

    covered = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    matches_after = [
        p for p in covered["pending"] if p["suggested_correlation_id"] == correlation_id
    ]
    assert matches_after == []


async def test_mismatched_symbols_in_one_correlation_surface_anomaly(
    db_session: AsyncSession,
) -> None:
    """A malformed correlation group is one resolvable due entry plus explicit
    anomaly evidence accounting for every affected execution row."""
    correlation_id = _uniq("-mismatch")
    aapl_coid = _uniq("-aapl")
    msft_coid = _uniq("-msft")
    transitioned = now_kst().astimezone(UTC) - timedelta(minutes=5)
    aapl = _row(
        client_order_id=aapl_coid,
        lifecycle_state="filled",
        symbol="AAPL",
        side="buy",
        lifecycle_correlation_id=correlation_id,
    )
    msft = _row(
        client_order_id=msft_coid,
        lifecycle_state="closed",
        symbol="MSFT",
        side="sell",
        lifecycle_correlation_id=correlation_id,
    )
    aapl.terminalized_at = transitioned
    msft.terminalized_at = transitioned
    db_session.add_all([aapl, msft])
    await db_session.commit()

    result = await _pending_with_retry(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="alpaca_paper",
    )
    matches = [
        p for p in result["pending"] if p["suggested_correlation_id"] == correlation_id
    ]
    assert len(matches) == 1, result["pending"]
    anomaly = matches[0]["correlation_anomaly"]
    assert anomaly["code"] == "inconsistent_correlation_group"
    assert anomaly["group_size"] == 2
    assert anomaly["symbols"] == ["AAPL", "MSFT"]
    assert anomaly["client_order_ids"] == sorted([aapl_coid, msft_coid])
    assert len(anomaly["ledger_row_ids"]) == 2
