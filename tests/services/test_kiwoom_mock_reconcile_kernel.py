"""hk 4722 / hk 4734 / hk 4802 — Kiwoom mock reconcile kernel (unit).

The ledger service is replaced by a recorder, the execution-ledger repository by
a run recorder, the broker by a fake reader and the journal tables by an
in-memory ``TradeJournal`` store on the fake session, so the kernel's contract
is proven without a database or network:

* dry run issues zero writes;
* terminal-only applies only expired/cancelled/rejected and uncertain→accepted;
* terminal-only is impossible without the exact-true gate;
* full mode needs both exact-true gates and refuses before any read otherwise;
* full mode books each broker-evidence delta **and journals it in the same
  transaction** (hk 4802): a buy delta creates or grows the order's lot at the
  delta's implied price, a sell delta FIFO-closes lots (split on a partial
  sell) with realized P&L, and ``journaled_qty``/``journaled_avg`` progress
  commits with it so a retried or overlapping run never journals twice;
* a sell delta that outruns the lots journals only the matched part, leaves a
  manual-review reason and is completed by a later run;
* a fill-carrying terminal verdict books and journals its delta first and then
  closes once the row's booked quantity equals the verdict's;
* terminal_only and US keep ``deferred_fill_booking`` and write no journal;
* overlapping full runs still serialize on the run lock, and a re-run of the
  same evidence books nothing new;
* unknown rows never transition; paged listings are followed, and a page cap is
  incomplete evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType
from app.services import kiwoom_mock_reconcile as kernel
from app.services.kiwoom_mock_order_ledger_service import (
    FillBooking,
    KiwoomMockTransitionRefused,
    journaled_progress,
)
from app.services.kiwoom_mock_reconcile import (
    FULL_GATE_ENVS,
    TERMINAL_GATE_ENV,
    kiwoom_mock_reconcile_orders_impl,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

TODAY = "20261116"
NOW = datetime(2026, 11, 16, 3, tzinfo=UTC)  # 12:00 KST


def _continuation(cont_yn: str = "N", next_key: str = "") -> dict[str, str]:
    return {"cont_yn": cont_yn, "next_key": next_key}


def _status_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "return_code": 0,
        "continuation": _continuation(),
        "acnt_ord_cntr_prst_array": list(rows),
    }


def _detail_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "return_code": 0,
        "continuation": _continuation(),
        "acnt_ord_cntr_prps_dtl": list(rows),
    }


def _detail_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ord_no": "0001",
        "stk_cd": "005930",
        "ord_qty": "10",
        "cntr_qty": "0",
        "ord_remnq": "10",
        "ord_uv": "70000",
        "cntr_uv": "0",
        "mdfy_cncl": "",
        "dmst_stex_tp": "KRX",
    }
    row.update(overrides)
    return row


def _row(
    ledger_id: int,
    *,
    broker_order_id: str = "0001",
    status: str = "accepted",
    trading_day: str = TODAY,
    symbol: str = "005930",
    filled_qty: int | None = None,
    side: str = "buy",
    avg_fill_price: int | None = None,
    journal_id: int | None = None,
    trade_id: int | None = None,
    journaled_qty: Decimal | None = None,
    journaled_avg: Decimal | None = None,
) -> SimpleNamespace:
    detail: dict[str, Any] | None = None
    if journaled_qty is not None:
        detail = {"journaled_qty": str(journaled_qty)}
        if journaled_avg is not None:
            detail["journaled_avg"] = str(journaled_avg)
    return SimpleNamespace(
        id=ledger_id,
        client_order_id=f"kwoomm-{ledger_id}",
        status=status,
        broker_order_id=broker_order_id,
        quantity=Decimal(10),
        filled_qty=None if filled_qty is None else Decimal(filled_qty),
        avg_fill_price=None if avg_fill_price is None else Decimal(avg_fill_price),
        symbol=symbol,
        side=side,
        trading_day=trading_day,
        trade_date=NOW,
        last_reconcile_detail=detail,
        instrument_type="equity_kr",
        currency="KRW",
        correlation_id=None,
        thesis="test thesis",
        strategy="test strategy",
        reason=None,
        exit_reason=None,
        notes=None,
        indicators_snapshot=None,
        trade_id=trade_id,
        journal_id=journal_id,
    )


class _FakeScalars:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return list(self._items)


class _FakeResult:
    """Serves the journal SELECT and the trade upsert ``RETURNING`` clause."""

    def scalars(self) -> _FakeScalars:
        active = [
            journal
            for journal in FakeSession.journals
            if journal.status == JournalStatus.active
        ]
        return _FakeScalars(active)

    def scalar_one(self) -> int:
        return 777  # the upserted trade row's id


class FakeSession:
    """In-memory journal store: get/add/flush/execute for the journal helpers."""

    statements: list[Any] = []
    journals: list[TradeJournal] = []
    _next_journal_id = 100

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, statement: Any = None, params: Any = None) -> _FakeResult:
        # The full-mode run lock is a plain SELECT on its own session; the unit
        # harness only needs it to not fail (the real ordering is a DB test).
        type(self).statements.append(statement)
        return _FakeResult()

    async def get(self, model: Any, ident: int) -> TradeJournal | None:
        return next(
            (journal for journal in type(self).journals if journal.id == ident),
            None,
        )

    def add(self, obj: Any) -> None:
        self._pending.append(obj)

    async def flush(self) -> None:
        for obj in self._pending:
            if obj.id is None:
                obj.id = type(self)._next_journal_id
                type(self)._next_journal_id += 1
            type(self).journals.append(obj)
        self._pending = []

    _pending: list[Any]

    def __init__(self) -> None:
        self._pending = []

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _journal(
    journal_id: int,
    *,
    symbol: str = "005930",
    quantity: str = "10",
    entry_price: str = "70000",
    status: JournalStatus = JournalStatus.active,
) -> TradeJournal:
    journal = TradeJournal(
        symbol=symbol,
        instrument_type=InstrumentType.equity_kr,
        side="buy",
        entry_price=Decimal(entry_price),
        quantity=Decimal(quantity),
        amount=Decimal(quantity) * Decimal(entry_price),
        thesis="seed thesis",
        strategy="seed strategy",
        account="kiwoom_mock",
        account_type="mock",
        status=status,
    )
    journal.id = journal_id
    return journal


class FakeRepo:
    """Records reconcile runs instead of writing execution_ledger run rows."""

    runs: list[Any] = []

    def __init__(self, db: Any) -> None:
        self.db = db

    def record_run(self, run: Any) -> None:
        type(self).runs.append(run)


def _fake_booking(
    row: Any,
    *,
    action: str,
    delta: Decimal,
    row_id: int | None = None,
    fill_seq: int | None = None,
    journaled: tuple[Decimal, Decimal] | None = None,
    avg_price: Decimal | None = None,
) -> FillBooking:
    journaled_qty, journaled_avg = (
        journaled if journaled is not None else journaled_progress(row)
    )
    return FillBooking(
        ledger_id=row.id,
        client_order_id=row.client_order_id,
        broker_order_id=str(row.broker_order_id),
        action=action,
        delta_qty=delta,
        cumulative_qty=Decimal(row.filled_qty or 0),
        avg_price=Decimal(row.avg_fill_price or 0) if avg_price is None else avg_price,
        status=row.status,
        execution_ledger_row_id=row_id,
        fill_seq=fill_seq,
        cumulative_avg=Decimal(row.avg_fill_price or 0),
        journaled_qty=journaled_qty,
        journaled_avg=journaled_avg,
        side=row.side,
        symbol=row.symbol,
        instrument_type=row.instrument_type,
        currency=row.currency,
        correlation_id=row.correlation_id,
        thesis=row.thesis,
        strategy=row.strategy,
        reason=row.reason,
        exit_reason=row.exit_reason,
        notes=row.notes,
        indicators_snapshot=row.indicators_snapshot,
        trade_id=row.trade_id,
        journal_id=row.journal_id,
    )


class FakeService:
    """Records every write; reads come from class-level fixtures."""

    rows: list[Any] = []
    total: int = 0
    cancels: set[str] = set()

    def __init__(self, db: Any) -> None:
        self.db = db
        self.writes: list[tuple[str, int, str, str | None]] = []

    async def list_reconcile_candidates(
        self,
        *,
        limit: int,
        account_mode: str = "kiwoom_mock",
        include_journal_recovery: bool = False,
    ):
        self.account_mode = account_mode
        open_rows = [
            row
            for row in type(self).rows
            if row.status in {"accepted", "pending", "partial", "uncertain"}
        ]
        if include_journal_recovery:
            seen = {row.id for row in open_rows}
            open_rows += [
                row
                for row in type(self).rows
                if row.id not in seen
                and (row.filled_qty or 0) > journaled_progress(row)[0]
            ]
        return list(open_rows[:limit]), type(self).total

    async def list_acknowledged_cancels(
        self, *, original_order_ids: set[str], account_mode: str = "kiwoom_mock"
    ):
        return type(self).cancels & original_order_ids

    def _row(self, ledger_id: int) -> Any:
        return next(item for item in type(self).rows if item.id == ledger_id)

    async def mark_terminal(
        self, *, ledger_id, status, reason, detail=None, account_mode="kiwoom_mock"
    ):
        # Mirrors the service: a booked fill must never be closed as a no-fill
        # terminal row.
        row = self._row(ledger_id)
        if row.status not in {"accepted", "pending"}:
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="source_status_not_open",
            )
        self.writes.append(("mark_terminal", ledger_id, status, reason))
        return SimpleNamespace(id=ledger_id, status=status)

    async def close_filled_as_terminal(
        self,
        *,
        ledger_id,
        status,
        reason,
        filled_quantity,
        detail=None,
        account_mode="kiwoom_mock",
    ):
        row = self._row(ledger_id)
        booked = row.filled_qty or Decimal(0)
        if row.status not in {"partial", "filled"} or booked != Decimal(
            filled_quantity
        ):
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="fill_not_booked",
            )
        row.status = status
        self.writes.append(("close_filled", ledger_id, status, reason))
        return SimpleNamespace(id=ledger_id, status=status)

    async def promote_uncertain_to_accepted(
        self,
        *,
        ledger_id,
        broker_order_id,
        detail=None,
        account_mode="kiwoom_mock",
    ):
        self.writes.append(("promote", ledger_id, broker_order_id, None))
        return SimpleNamespace(id=ledger_id, status="accepted")

    async def record_fill(
        self,
        *,
        ledger_id,
        filled_quantity,
        average_price,
        observed_at=None,
        commit=True,
    ):
        row = next(item for item in type(self).rows if item.id == ledger_id)
        journaled = journaled_progress(row)
        previous = int(row.filled_qty or 0)
        previous_avg = row.avg_fill_price
        delta = int(filled_quantity) - previous
        if (
            delta == 0
            and previous_avg is not None
            and Decimal(int(average_price)) != previous_avg
        ):
            raise KiwoomMockTransitionRefused(
                ledger_id=ledger_id,
                status=row.status,
                reason="average_price_changed_without_fill",
            )
        status = "filled" if int(filled_quantity) >= int(row.quantity) else "partial"
        action = f"booked_{status}" if delta > 0 else "noop_already_booked"
        self.writes.append(("record_fill", ledger_id, action, None))
        row.filled_qty = Decimal(int(filled_quantity))
        row.avg_fill_price = Decimal(int(average_price))
        row.status = status
        # The implied delta price, mirroring the real service's
        # (cumulative·avg − previous·prev_avg)/delta derivation.
        delta_price = (
            (
                Decimal(int(filled_quantity)) * Decimal(int(average_price))
                - Decimal(previous) * (previous_avg or Decimal(0))
            )
            / delta
            if delta > 0
            else Decimal(int(average_price))
        )
        return _fake_booking(
            row,
            action=action,
            delta=Decimal(delta),
            row_id=None if delta <= 0 else 900 + ledger_id,
            fill_seq=None if delta <= 0 else 4242,
            journaled=journaled,
            avg_price=delta_price,
        )

    async def lock_for_journal(self, *, ledger_id):
        row = next(item for item in type(self).rows if item.id == ledger_id)
        return _fake_booking(row, action="journal_lock", delta=Decimal(0))

    async def record_journal_progress(
        self,
        *,
        ledger_id,
        trade_id=None,
        journal_id=None,
        journaled_qty=None,
        journaled_avg=None,
        manual_review_reason=None,
        commit=True,
    ):
        row = self._row(ledger_id)
        if trade_id is not None and row.trade_id is None:
            row.trade_id = trade_id
        if journal_id is not None:
            row.journal_id = journal_id
        detail = dict(row.last_reconcile_detail or {})
        current_qty, _ = journaled_progress(row)
        if journaled_qty is not None and journaled_qty > current_qty:
            detail["journaled_qty"] = str(journaled_qty)
            if journaled_avg is not None:
                detail["journaled_avg"] = str(journaled_avg)
        if manual_review_reason is not None:
            detail["journal_manual_review"] = manual_review_reason
        elif journaled_qty is not None:
            detail.pop("journal_manual_review", None)
        row.last_reconcile_detail = detail
        self.writes.append(
            ("journal_progress", ledger_id, str(journaled_qty), manual_review_reason)
        )


class FakeReader:
    """Serves configured pages and records the continuation tokens it was sent."""

    def __init__(
        self,
        *,
        status_pages: list[dict[str, Any]] | None = None,
        detail_pages: dict[str, list[dict[str, Any]]] | None = None,
        fail: bool = False,
    ) -> None:
        self.status_pages = list(status_pages or [_status_payload()])
        self.detail_pages = {
            day: list(pages) for day, pages in (detail_pages or {}).items()
        }
        self.fail = fail
        self.calls: list[tuple[str, str | None, str | None]] = []
        self._status_i = 0
        self._detail_i: dict[str, int] = {}

    async def read_order_status(self, *, cont_yn=None, next_key=None):
        self.calls.append(("status", cont_yn, next_key))
        if self.fail:
            raise RuntimeError("boom")
        page = self.status_pages[min(self._status_i, len(self.status_pages) - 1)]
        self._status_i += 1
        return page

    async def read_order_detail(self, *, order_date: str, cont_yn=None, next_key=None):
        self.calls.append((f"detail:{order_date}", cont_yn, next_key))
        if self.fail:
            raise RuntimeError("boom")
        pages = self.detail_pages.get(order_date, [_detail_payload()])
        index = self._detail_i.get(order_date, 0)
        self._detail_i[order_date] = index + 1
        return pages[min(index, len(pages) - 1)]


@pytest.fixture()
def fake_ledger(monkeypatch: pytest.MonkeyPatch):
    FakeService.rows = []
    FakeService.total = 0
    FakeService.cancels = set()
    FakeRepo.runs = []
    FakeSession.journals = []
    FakeSession._next_journal_id = 100
    FakeSession.statements = []
    written: list[FakeService] = []

    class Recorder(FakeService):
        def __init__(self, db: Any) -> None:
            super().__init__(db)
            written.append(self)

    monkeypatch.setattr(kernel, "KiwoomMockOrderLedgerService", Recorder)
    monkeypatch.setattr(kernel, "ExecutionLedgerRepository", FakeRepo)
    return written


def _writes(recorders: list[FakeService]) -> list[tuple[str, int, str, str | None]]:
    return [write for recorder in recorders for write in recorder.writes]


async def _run(
    reader: FakeReader,
    *,
    dry_run: bool = True,
    terminal_only: bool = False,
    limit: int = 100,
    max_pages: int = kernel.DEFAULT_MAX_LISTING_PAGES,
    sleeps: list[float] | None = None,
) -> dict[str, Any]:
    async def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return await kiwoom_mock_reconcile_orders_impl(
        dry_run=dry_run,
        terminal_only=terminal_only,
        limit=limit,
        max_pages=max_pages,
        session_factory=FakeSession,
        reader=reader,
        sleep=fake_sleep,
        now=NOW,
    )


async def test_dry_run_issues_zero_writes(fake_ledger) -> None:
    FakeService.rows = [
        _row(1, broker_order_id="0001"),
        _row(2, broker_order_id="0002"),
    ]
    FakeService.total = 2
    # The cancel pair: our own acknowledged request + broker confirmation.
    FakeService.cancels = {"0001"}
    reader = FakeReader(
        status_pages=[
            _status_payload(
                {
                    "ord_no": "0001",
                    "stk_cd": "005930",
                    "ord_qty": "10",
                    "cntr_qty": "0",
                    "ord_uv": "70000",
                    "cntr_uv": "0",
                    "mdfy_cncl_tp": "취소",
                },
            )
        ],
        detail_pages={
            TODAY: [
                _detail_payload(
                    _detail_row(ord_no="0001", ord_remnq="0"),
                    _detail_row(
                        ord_no="0002", cntr_qty="10", ord_remnq="0", cntr_uv="70000"
                    ),
                )
            ]
        },
    )

    result = await _run(reader)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert _writes(fake_ledger) == []
    actions = {row["ledger_id"]: row["action"] for row in result["reconciled"]}
    assert actions[1] == "would_mark_cancelled"
    assert actions[2] == "deferred_fill_booking"


async def test_terminal_only_applies_only_terminal_transitions(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TERMINAL_GATE_ENV, "true")
    FakeService.cancels = {"0003"}
    FakeService.rows = [
        _row(1, broker_order_id="0001"),  # cancelled marker, no own ack -> unknown
        _row(2, broker_order_id="0002"),  # filled -> deferred
        _row(3, broker_order_id="0003"),  # cancelled marker + own ack -> cancelled
        _row(4, broker_order_id="0004", status="uncertain"),  # working -> accepted
        _row(5, broker_order_id="0005"),  # absent from listing -> unknown
    ]
    FakeService.total = 5
    reader = FakeReader(
        status_pages=[
            _status_payload(
                {
                    "ord_no": "0001",
                    "stk_cd": "005930",
                    "ord_qty": "10",
                    "cntr_qty": "0",
                    "ord_uv": "70000",
                    "cntr_uv": "0",
                    "mdfy_cncl_tp": "취소",
                },
            )
        ],
        detail_pages={
            TODAY: [
                _detail_payload(
                    _detail_row(ord_no="0001", ord_remnq="0", mdfy_cncl="취소"),
                    _detail_row(
                        ord_no="0002", cntr_qty="10", ord_remnq="0", cntr_uv="70000"
                    ),
                    _detail_row(ord_no="0003", ord_remnq="0", mdfy_cncl="취소"),
                    _detail_row(ord_no="0004"),
                )
            ]
        },
    )
    result = await _run(reader, dry_run=False, terminal_only=True)
    assert result["success"] is True
    assert result["scope"] == "terminal_only"

    actual = _writes(fake_ledger)
    assert (
        "mark_terminal",
        3,
        "cancelled",
        "own_cancel_acknowledged_and_broker_confirmed",
    ) in actual
    assert ("promote", 4, "0004", None) in actual
    written_ids = {write[1] for write in actual}
    assert written_ids == {3, 4}
    actions = {row["ledger_id"]: row["action"] for row in result["reconciled"]}
    assert actions[2] == "deferred_fill_booking"
    assert actions[5] == "noop_no_evidence"
    assert result["unresolved_count"] >= 2


def _filled_reader(average_price: int = 70000) -> FakeReader:
    return FakeReader(
        detail_pages={
            TODAY: [
                _detail_payload(
                    _detail_row(
                        ord_no="0001",
                        cntr_qty="10",
                        ord_remnq="0",
                        cntr_uv=str(average_price),
                    )
                )
            ]
        }
    )


def _expired_partial_reader(
    quantity: int = 4, average_price: int = 70000
) -> FakeReader:
    """4 of 10 filled, nothing left working, broker expiry marker."""
    return FakeReader(
        detail_pages={
            TODAY: [
                _detail_payload(
                    _detail_row(
                        ord_no="0001",
                        cntr_qty=str(quantity),
                        ord_remnq="0",
                        cntr_uv=str(average_price),
                        mdfy_cncl="만료",
                    )
                )
            ]
        }
    )


def _rejected_partial_reader(
    quantity: int = 4, average_price: int = 70000
) -> FakeReader:
    """4 of 10 filled, nothing left working, broker reject marker."""
    return FakeReader(
        detail_pages={
            TODAY: [
                _detail_payload(
                    _detail_row(
                        ord_no="0001",
                        cntr_qty=str(quantity),
                        ord_remnq="0",
                        cntr_uv=str(average_price),
                        mdfy_cncl="거부",
                    )
                )
            ]
        }
    )


def _partial_reader(quantity: int = 4, average_price: int = 70000) -> FakeReader:
    return FakeReader(
        detail_pages={
            TODAY: [
                _detail_payload(
                    _detail_row(
                        ord_no="0001",
                        cntr_qty=str(quantity),
                        ord_remnq=str(10 - quantity),
                        cntr_uv=str(average_price),
                    )
                )
            ]
        }
    )


@pytest.mark.parametrize(
    "enabled",
    [
        {},
        {FULL_GATE_ENVS[0]: "true"},
        {FULL_GATE_ENVS[1]: "true"},
        {FULL_GATE_ENVS[0]: "yes", FULL_GATE_ENVS[1]: "true"},
    ],
)
async def test_full_mode_needs_both_gates_before_any_read(
    fake_ledger, monkeypatch: pytest.MonkeyPatch, enabled: dict[str, str]
) -> None:
    for name in FULL_GATE_ENVS:
        monkeypatch.delenv(name, raising=False)
    for name, value in enabled.items():
        monkeypatch.setenv(name, value)

    class ExplodingSession:
        async def __aenter__(self):
            raise AssertionError("ledger must not be read with the gate off")

    class ExplodingReader(FakeReader):
        async def read_order_status(self, *, cont_yn=None, next_key=None):
            raise AssertionError("broker must not be read with the gate off")

    result = await kiwoom_mock_reconcile_orders_impl(
        dry_run=False,
        terminal_only=False,
        session_factory=ExplodingSession,
        reader=ExplodingReader(),
        now=NOW,
    )

    assert result["success"] is False
    assert result["error"] == "full_gate_disabled"
    assert result["scope"] == "full"
    assert _writes(fake_ledger) == []


async def test_full_mode_books_the_delta_and_journals_it(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filled verdict books its delta and journals it in the same pass."""
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [_row(1, broker_order_id="0001")]
    FakeService.total = 1

    result = await _run(_filled_reader(), dry_run=False, terminal_only=False)

    assert result["success"] is True
    assert result["scope"] == "full"
    outcome = result["reconciled"][0]
    assert outcome["verdict"] == "filled"
    assert outcome["action"] == "booked_filled"
    assert outcome["delta_qty"] == "10"
    assert outcome["execution_ledger_row_id"] == 901
    assert ("record_fill", 1, "booked_filled", None) in _writes(fake_ledger)
    assert not outcome.get("requires_manual_review")
    assert result["unresolved_count"] == 0

    # hk 4802: the booked delta journals in the same pass — one active lot.
    journal = outcome["journal"]
    assert journal["journal_created"] is True
    assert journal["journaled_qty"] == "10"
    assert len(FakeSession.journals) == 1
    lot = FakeSession.journals[0]
    assert lot.status == JournalStatus.active
    assert lot.quantity == Decimal("10")
    assert lot.entry_price == Decimal("70000")
    assert lot.account == "kiwoom_mock"
    assert lot.account_type == "mock"
    assert ("journal_progress", 1, "10", None) in _writes(fake_ledger)

    assert len(FakeRepo.runs) == 1
    run = FakeRepo.runs[0]
    assert run.broker == "kiwoom"
    assert run.dry_run is False
    assert run.committed_insert == 1
    assert run.unchanged == 0


async def test_terminal_only_and_dry_run_write_no_journal(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scopes that cannot book a fill must never write a journal row."""
    monkeypatch.setenv(TERMINAL_GATE_ENV, "true")
    FakeService.rows = [_row(1, broker_order_id="0001")]
    FakeService.total = 1

    await _run(_filled_reader(), dry_run=False, terminal_only=True)
    await _run(_filled_reader(), dry_run=True)

    assert FakeSession.journals == []
    assert not any(write[0] == "journal_progress" for write in _writes(fake_ledger))


async def test_full_mode_rerun_books_and_journals_nothing_new(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [_row(1, broker_order_id="0001")]
    FakeService.total = 1

    await _run(_filled_reader(), dry_run=False, terminal_only=False)
    second = await _run(_filled_reader(), dry_run=False, terminal_only=False)

    # The first run booked and journaled the whole fill; the row is terminal
    # and fully journaled, so the second run finds nothing to do.
    assert second["reconciled"] == []
    assert len(FakeSession.journals) == 1
    # An empty scan returns before the run row is written.
    assert len(FakeRepo.runs) == 1
    assert second["unresolved_count"] == 0


async def test_partial_then_terminal_books_each_delta_and_grows_one_lot(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial books only the delta; the final fill books and grows the rest.

    4 @ 70,000 then cumulative 10 @ 76,000: the second delta is 6 shares at the
    implied 80,000, so one buy journal ends at 10 shares entered at the
    quantity-weighted 76,000 — the journal's cost basis equals the booked
    notional (4·70,000 + 6·80,000).
    """
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [_row(1, broker_order_id="0001")]
    FakeService.total = 1

    first = await _run(_partial_reader(), dry_run=False, terminal_only=False)
    outcome = first["reconciled"][0]
    assert outcome["action"] == "booked_partial"
    assert outcome["delta_qty"] == "4"
    assert outcome["journal"]["journal_created"] is True
    assert len(FakeSession.journals) == 1
    lot = FakeSession.journals[0]
    assert lot.quantity == Decimal("4")
    assert lot.entry_price == Decimal("70000")

    second = await _run(
        _filled_reader(average_price=76000), dry_run=False, terminal_only=False
    )
    outcome = second["reconciled"][0]
    assert outcome["action"] == "booked_filled"
    assert outcome["delta_qty"] == "6"
    assert outcome["booked_cumulative_qty"] == "10"
    assert outcome["journal"]["journal_grown"] is True

    assert len(FakeSession.journals) == 1
    assert lot.quantity == Decimal("10")
    assert lot.entry_price == Decimal("76000")
    stored = FakeService.rows[0]
    assert journaled_progress(stored) == (Decimal("10"), Decimal("76000"))


async def test_sell_delta_fifo_closes_and_splits_the_lot(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sell delta closes lots FIFO; a partial sell splits the boundary lot."""
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [
        _row(1, broker_order_id="0001", side="sell"),
    ]
    FakeService.total = 1
    FakeSession.journals = [
        _journal(7, quantity="10", entry_price="70000"),
    ]

    result = await _run(
        _partial_reader(quantity=4, average_price=80000),
        dry_run=False,
        terminal_only=False,
    )

    outcome = result["reconciled"][0]
    assert outcome["action"] == "booked_partial"
    journal = outcome["journal"]
    assert journal["journals_closed"] == 1
    assert journal["unmatched_quantity"] == 0
    # 4 sold at 80,000 against a 70,000-entry lot: ~14.2857% realized.
    assert journal["realized_pnl_pct"] == pytest.approx(14.2857, abs=1e-3)
    assert journal["journaled_qty"] == "4"
    assert not outcome.get("requires_manual_review")

    # The lot split: the consumed 4 closed at 80,000; 6 stay active at 70,000.
    closed = [j for j in FakeSession.journals if j.status == JournalStatus.closed]
    active = [j for j in FakeSession.journals if j.status == JournalStatus.active]
    assert len(closed) == 1
    assert closed[0].quantity == Decimal("4")
    assert closed[0].exit_price == Decimal("80000")
    assert len(active) == 1
    assert active[0].quantity == Decimal("6")
    assert active[0].entry_price == Decimal("70000")


async def test_short_sell_journals_matched_part_and_recovers_next_run(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """unmatched_quantity never advances progress; a later run completes it."""
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [
        _row(1, broker_order_id="0001", side="sell"),
    ]
    FakeService.total = 1
    # Only 6 of the 10-share sell is covered by lots.
    FakeSession.journals = [_journal(7, quantity="6", entry_price="70000")]

    result = await _run(
        _partial_reader(quantity=10, average_price=80000),
        dry_run=False,
        terminal_only=False,
    )

    outcome = result["reconciled"][0]
    assert outcome["action"] == "booked_filled"
    journal = outcome["journal"]
    assert journal["unmatched_quantity"] == 4
    # The unmatched 4 shares are NOT journaled: progress stops at 6 and the
    # row keeps a manual-review reason for the next run.
    assert Decimal(str(journal["journaled_qty"])) == Decimal("6")
    assert outcome.get("requires_manual_review") is True
    assert result["unresolved_count"] >= 1
    stored = FakeService.rows[0]
    assert journaled_progress(stored)[0] == Decimal("6")
    assert stored.last_reconcile_detail["journal_manual_review"].startswith(
        "unmatched_sell_quantity"
    )

    # Next run: the 'filled' row is scanned for journal recovery, the new lot
    # covers the remaining 4 and the manual-review reason clears.
    FakeSession.journals.append(_journal(8, quantity="4", entry_price="70000"))
    second = await _run(
        _partial_reader(quantity=10, average_price=80000),
        dry_run=False,
        terminal_only=False,
    )
    outcome = second["reconciled"][0]
    assert outcome["verdict"] == "journal_recovery"
    assert outcome["action"] == "journaled"
    assert Decimal(str(outcome["journal"]["journaled_qty"])) == Decimal("10")
    assert not outcome.get("requires_manual_review")
    assert journaled_progress(stored)[0] == Decimal("10")
    assert "journal_manual_review" not in (stored.last_reconcile_detail or {})
    # The short sell was never marked fully journaled early.
    closed_qty = sum(
        j.quantity or 0
        for j in FakeSession.journals
        if j.status == JournalStatus.closed
    )
    assert closed_qty == Decimal("10")


async def test_overlapping_full_runs_book_the_delta_once(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two overlapping full runs serialize on the lock; one delta is booked."""
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [_row(1, broker_order_id="0001")]
    FakeService.total = 1

    run_lock = asyncio.Lock()
    acquisitions: list[int] = []

    @contextlib.asynccontextmanager
    async def serialized(session_factory: Any):
        async with run_lock:
            acquisitions.append(1)
            yield

    monkeypatch.setattr(kernel, "_hold_full_run_lock", serialized)

    first, second = await asyncio.gather(
        _run(_filled_reader(), dry_run=False, terminal_only=False),
        _run(_filled_reader(), dry_run=False, terminal_only=False),
    )

    assert acquisitions == [1, 1]
    deltas = sorted(
        outcome["delta_qty"]
        for result in (first, second)
        for outcome in result["reconciled"]
    )
    # The losing run sees the row already booked+journaled and out of scan.
    assert deltas == ["10"]
    assert len(FakeSession.journals) == 1
    booked = [
        write
        for write in _writes(fake_ledger)
        if write[0] == "record_fill" and write[2] != "noop_already_booked"
    ]
    assert len(booked) == 1


async def test_terminal_verdict_with_a_positive_fill_books_then_closes(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settled partial (4 of 10, expiry marker) is booked, then closed."""
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [_row(1, broker_order_id="0001")]
    FakeService.total = 1

    result = await _run(_expired_partial_reader(), dry_run=False, terminal_only=False)

    outcome = result["reconciled"][0]
    assert outcome["verdict"] == "expired"
    assert outcome["filled_qty"] == 4
    assert outcome["fill_action"] == "booked_partial"
    assert outcome["action"] == "mark_expired"
    assert not outcome.get("requires_manual_review")
    # record_fill moved the row to ``partial`` first, so the close must go
    # through close_filled_as_terminal, not mark_terminal.
    assert (
        "close_filled",
        1,
        "expired",
        "broker_expiry_confirmed",
    ) in _writes(fake_ledger)
    assert not any(write[0] == "mark_terminal" for write in _writes(fake_ledger))


async def test_fill_carrying_rejection_is_booked_then_closed(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected verdict with a positive cumulative fill closes after booking.

    The classifier can emit ``rejected`` with a fill (settled partial, reject
    marker); that fill must be booked and the row closed, not left refused for
    ever (round 8 findings r4577789108/r4577789147/r4577667963).
    """
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [_row(1, broker_order_id="0001")]
    FakeService.total = 1

    result = await _run(_rejected_partial_reader(), dry_run=False, terminal_only=False)

    outcome = result["reconciled"][0]
    assert outcome["verdict"] == "rejected"
    assert outcome["filled_qty"] == 4
    assert outcome["fill_action"] == "booked_partial"
    assert outcome["action"] == "mark_rejected"
    assert (
        "close_filled",
        1,
        "rejected",
        "broker_reject_confirmed",
    ) in _writes(fake_ledger)
    assert not outcome.get("requires_manual_review")
    assert result["unresolved_count"] == 0


async def test_terminal_only_defers_a_settled_partial_instead_of_closing_it(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """terminal_only cannot book the fill, so the row must stay open.

    Closing it would take the row out of the open scan with 4 broker-filled
    shares never written to execution_ledger.
    """
    monkeypatch.setenv(TERMINAL_GATE_ENV, "true")
    FakeService.rows = [_row(1, broker_order_id="0001")]
    FakeService.total = 1

    result = await _run(_expired_partial_reader(), dry_run=False, terminal_only=True)

    assert _writes(fake_ledger) == []
    outcome = result["reconciled"][0]
    assert outcome["verdict"] == "expired"
    assert outcome["filled_qty"] == 4
    assert outcome["action"] == "deferred_fill_booking"
    assert outcome["requires_session_reconcile"] is True
    assert result["unresolved_count"] >= 1


async def test_terminal_gate_off_refuses_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TERMINAL_GATE_ENV, raising=False)

    class ExplodingSession:
        async def __aenter__(self):
            raise AssertionError("ledger must not be read with the gate off")

    class ExplodingReader(FakeReader):
        async def read_order_status(self, *, cont_yn=None, next_key=None):
            raise AssertionError("broker must not be read with the gate off")

    result = await kiwoom_mock_reconcile_orders_impl(
        dry_run=False,
        terminal_only=True,
        session_factory=ExplodingSession,
        reader=ExplodingReader(),
        now=NOW,
    )
    assert result["success"] is False
    assert result["error"] == "terminal_gate_disabled"
    assert result["scope"] == "terminal_only"


async def test_terminal_only_with_dry_run_is_refused(fake_ledger) -> None:
    with pytest.raises(ValueError):
        await _run(FakeReader(), dry_run=True, terminal_only=True)


async def test_broker_read_failure_is_unknown_with_gate_on(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TERMINAL_GATE_ENV, "true")
    FakeService.rows = [_row(1)]
    FakeService.total = 1
    result = await _run(FakeReader(fail=True), dry_run=False, terminal_only=True)
    assert result["success"] is True
    assert _writes(fake_ledger) == []
    row = result["reconciled"][0]
    assert row["verdict"] == "unknown"
    assert row["requires_manual_review"] is True
    assert row["action"] == "noop_no_evidence"
    assert row["reason"] == "order_status_unreadable"


async def test_reads_are_serial_and_spaced(fake_ledger) -> None:
    FakeService.rows = [
        _row(1, trading_day="20261114"),
        _row(2, trading_day=TODAY),
    ]
    FakeService.total = 2
    sleeps: list[float] = []
    reader = FakeReader(
        detail_pages={
            "20261114": [_detail_payload(_detail_row())],
            TODAY: [_detail_payload(_detail_row())],
        }
    )

    await _run(reader, sleeps=sleeps)

    # kt00009 once (today is among the days) + kt00007 per distinct day, 1.0 s apart.
    assert reader.calls == [
        ("status", None, None),
        ("detail:20261114", None, None),
        (f"detail:{TODAY}", None, None),
    ]
    assert sleeps == [1.0, 1.0]


async def test_paged_listing_is_followed(fake_ledger) -> None:
    FakeService.rows = [
        _row(1, broker_order_id="0001"),
        _row(2, broker_order_id="0002"),
    ]
    FakeService.total = 2
    page_one = _detail_payload(
        _detail_row(ord_no="0001", cntr_qty="10", ord_remnq="0", cntr_uv="70000")
    )
    page_one["continuation"] = _continuation("Y", "NEXT")
    page_two = _detail_payload(_detail_row(ord_no="0002"))
    reader = FakeReader(detail_pages={TODAY: [page_one, page_two]})

    result = await _run(reader)

    assert reader.calls[1:] == [
        (f"detail:{TODAY}", None, None),
        (f"detail:{TODAY}", "Y", "NEXT"),
    ]
    actions = {row["ledger_id"]: row["action"] for row in result["reconciled"]}
    assert actions[1] == "deferred_fill_booking"
    # The second page's row is seen, so row 2 is working, not "no_exact_row".
    assert actions[2] == "noop_pending"


async def test_page_cap_is_incomplete_evidence(fake_ledger) -> None:
    FakeService.rows = [_row(1)]
    FakeService.total = 1
    page = _detail_payload(_detail_row())
    page["continuation"] = _continuation("Y", "AGAIN")
    reader = FakeReader(detail_pages={TODAY: [page]})

    result = await _run(reader, max_pages=2)

    row = result["reconciled"][0]
    assert row["verdict"] == "unknown"
    assert row["reason"] == "order_detail_page_cap_reached"
    assert row["action"] == "noop_no_evidence"


async def test_uncertain_with_terminal_marker_is_not_promoted(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TERMINAL_GATE_ENV, "true")
    FakeService.rows = [_row(1, broker_order_id="0001", status="uncertain")]
    FakeService.total = 1
    reader = FakeReader(
        detail_pages={
            TODAY: [
                _detail_payload(
                    _detail_row(ord_no="0001", ord_remnq="0", mdfy_cncl="취소")
                )
            ]
        }
    )

    result = await _run(reader, dry_run=False, terminal_only=True)

    assert _writes(fake_ledger) == []
    row = result["reconciled"][0]
    assert row["verdict"] == "unknown"
    assert row["reason"] == "uncertain_broker_terminal_evidence"
    assert result["unresolved_count"] >= 1


async def test_partial_row_is_not_closed_as_terminal(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TERMINAL_GATE_ENV, "true")
    FakeService.rows = [_row(1, broker_order_id="0001", status="partial", filled_qty=4)]
    FakeService.total = 1
    reader = FakeReader(
        detail_pages={
            TODAY: [
                _detail_payload(
                    _detail_row(ord_no="0001", ord_remnq="0", mdfy_cncl="취소")
                )
            ]
        }
    )

    result = await _run(reader, dry_run=False, terminal_only=True)

    assert _writes(fake_ledger) == []
    row = result["reconciled"][0]
    assert row["verdict"] == "unknown"
    assert row["reason"] == "recorded_fill_contradicts_broker"


async def test_truncated_scan_reports_work_remaining(fake_ledger) -> None:
    FakeService.rows = [_row(1), _row(2)]
    FakeService.total = 2
    result = await _run(
        FakeReader(detail_pages={TODAY: [_detail_payload(_detail_row())]}), limit=1
    )
    assert result["candidate_scan"] == {
        "open_total": 2,
        "scanned": 1,
        "limit": 1,
        "truncated": True,
    }
    assert result["unresolved_count"] >= 1


async def test_no_candidates_returns_success(fake_ledger) -> None:
    result = await _run(FakeReader())
    assert result["success"] is True
    assert result["reconciled"] == []
    assert result["unresolved_count"] == 0
