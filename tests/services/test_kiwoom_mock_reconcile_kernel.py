"""hk 4722 / hk 4734 (3797 T-P3, T-P4) — Kiwoom mock reconcile kernel (unit).

The ledger service is replaced by a recorder, the execution-ledger repository by
a run recorder and the broker by a fake reader, so the kernel's contract is
proven without a database or network:

* dry run issues zero writes;
* terminal-only applies only expired/cancelled/rejected and uncertain→accepted;
* terminal-only is impossible without the exact-true gate;
* full mode needs both exact-true gates and refuses before any read otherwise;
* full mode books the broker-evidence delta and journals it with
  ``account='kiwoom_mock'``/``account_type='mock'``;
* a re-run of the same evidence books nothing new and journals nothing new;
* a journal failure after a booked fill is surfaced, never hidden;
* unknown rows never transition; paged listings are followed, and a page cap is
  incomplete evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import kiwoom_mock_reconcile as kernel
from app.services.kiwoom_mock_order_ledger_service import FillBooking
from app.services.kiwoom_mock_reconcile import (
    FULL_GATE_ENVS,
    JOURNAL_ACCOUNT,
    JOURNAL_ACCOUNT_TYPE,
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
    journal_id: int | None = None,
    journaled_qty: Decimal | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=ledger_id,
        client_order_id=f"kwoomm-{ledger_id}",
        status=status,
        broker_order_id=broker_order_id,
        quantity=Decimal(10),
        filled_qty=None if filled_qty is None else Decimal(filled_qty),
        avg_fill_price=None,
        symbol=symbol,
        side=side,
        trading_day=trading_day,
        instrument_type="equity_kr",
        currency="KRW",
        correlation_id=f"kiwoom-mock:{ledger_id}",
        thesis="breakout",
        strategy="kiwoom-test",
        reason=None,
        exit_reason=None,
        notes=None,
        indicators_snapshot=None,
        journal_id=journal_id,
        journaled_qty=Decimal(0) if journaled_qty is None else journaled_qty,
        trade_id=None,
        trade_date=NOW,
        last_reconcile_detail=None,
    )


class FakeSession:
    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def commit(self) -> None:
        return None


class FakeRepo:
    """Records reconcile runs instead of writing execution_ledger run rows."""

    runs: list[Any] = []

    def __init__(self, db: Any) -> None:
        self.db = db

    def record_run(self, run: Any) -> None:
        type(self).runs.append(run)


class FakeService:
    """Records every write; reads come from class-level fixtures."""

    rows: list[Any] = []
    total: int = 0
    cancels: set[str] = set()

    def __init__(self, db: Any) -> None:
        self.db = db
        self.writes: list[tuple[str, int, str, str | None]] = []

    async def list_reconcile_candidates(
        self, *, limit: int, include_journal_recovery: bool = False
    ):
        return list(type(self).rows[:limit]), type(self).total

    async def list_acknowledged_cancels(self, *, original_order_ids: set[str]):
        return type(self).cancels & original_order_ids

    async def mark_terminal(self, *, ledger_id, status, reason, detail=None):
        self.writes.append(("mark_terminal", ledger_id, status, reason))
        return SimpleNamespace(id=ledger_id, status=status)

    async def promote_uncertain_to_accepted(
        self, *, ledger_id, broker_order_id, detail=None
    ):
        self.writes.append(("promote", ledger_id, broker_order_id, None))
        return SimpleNamespace(id=ledger_id, status="accepted")

    async def record_fill(
        self, *, ledger_id, filled_quantity, average_price, observed_at=None
    ):
        row = next(item for item in type(self).rows if item.id == ledger_id)
        previous = int(row.filled_qty or 0)
        delta = int(filled_quantity) - previous
        status = "filled" if int(filled_quantity) >= int(row.quantity) else "partial"
        action = f"booked_{status}" if delta > 0 else "noop_already_booked"
        self.writes.append(("record_fill", ledger_id, action, None))
        row.filled_qty = Decimal(int(filled_quantity))
        return FillBooking(
            ledger_id=row.id,
            client_order_id=row.client_order_id,
            broker_order_id=str(row.broker_order_id),
            action=action,
            delta_qty=Decimal(delta),
            cumulative_qty=Decimal(int(filled_quantity)),
            avg_price=Decimal(int(average_price)),
            cumulative_avg=Decimal(int(average_price)),
            journaled_qty=row.journaled_qty,
            journaled_avg=Decimal(0),
            status=status,
            execution_ledger_row_id=None if delta <= 0 else 900 + ledger_id,
            fill_seq=None if delta <= 0 else 4242,
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
            trade_id=getattr(row, "trade_id", None),
            journal_id=row.journal_id,
        )

    async def attach_journal(
        self, *, ledger_id, trade_id, journal_id, journaled_qty=None, journaled_avg=None
    ):
        row = next(item for item in type(self).rows if item.id == ledger_id)
        if trade_id is not None and getattr(row, "trade_id", None) is None:
            row.trade_id = trade_id
        if journal_id is not None and row.journal_id is None:
            row.journal_id = journal_id
        if journaled_qty is not None and journaled_qty > row.journaled_qty:
            row.journaled_qty = journaled_qty
        self.writes.append(("attach_journal", ledger_id, str(journal_id), None))


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


def _patch_journal(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    fail: bool = False,
    journal_id: int = 55,
) -> None:
    """Replace the journal writers; record the exact kwargs the kernel passes."""
    from app.mcp_server.tooling import order_journal

    async def save_order_fill(**kwargs: Any) -> int:
        calls.append(("save_order_fill", kwargs))
        return 501

    async def create_journal(**kwargs: Any) -> dict[str, Any]:
        calls.append(("create_journal", kwargs))
        if fail:
            raise RuntimeError("journal db down")
        return {"journal_id": journal_id}

    async def link(
        symbol: str, trade_id: int, *, account_type: str, account: str
    ) -> None:
        calls.append(
            (
                "link",
                {
                    "symbol": symbol,
                    "trade_id": trade_id,
                    "account_type": account_type,
                    "account": account,
                },
            )
        )

    async def close_journals(**kwargs: Any) -> dict[str, Any]:
        calls.append(("close_journals", kwargs))
        return {
            "journals_closed": 1,
            "journals_kept": 0,
            "closed_ids": [77],
            "total_pnl_pct": 2.5,
            "realized_pnl_basis": "journal_entry",
        }

    monkeypatch.setattr(order_journal, "_save_order_fill", save_order_fill)
    monkeypatch.setattr(order_journal, "_create_trade_journal_for_buy", create_journal)
    monkeypatch.setattr(order_journal, "_link_journal_to_fill", link)
    monkeypatch.setattr(order_journal, "_close_journals_on_sell", close_journals)


def _filled_reader() -> FakeReader:
    return FakeReader(
        detail_pages={
            TODAY: [
                _detail_payload(
                    _detail_row(
                        ord_no="0001", cntr_qty="10", ord_remnq="0", cntr_uv="70000"
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


async def test_full_mode_books_the_delta_and_journals_under_kiwoom_mock(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [_row(1, broker_order_id="0001")]
    FakeService.total = 1
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_journal(monkeypatch, calls)

    result = await _run(_filled_reader(), dry_run=False, terminal_only=False)

    assert result["success"] is True
    assert result["scope"] == "full"
    outcome = result["reconciled"][0]
    assert outcome["action"] == "booked_filled"
    assert outcome["delta_qty"] == "10"
    assert outcome["execution_ledger_row_id"] == 901
    assert ("record_fill", 1, "booked_filled", None) in _writes(fake_ledger)

    save = next(payload for name, payload in calls if name == "save_order_fill")
    assert save["account"] == JOURNAL_ACCOUNT == "kiwoom_mock"
    assert save["order_id"] == "0001"
    assert save["quantity"] == 10.0
    create = next(payload for name, payload in calls if name == "create_journal")
    assert create["account"] == JOURNAL_ACCOUNT == "kiwoom_mock"
    assert create["account_type"] == JOURNAL_ACCOUNT_TYPE == "mock"
    link = next(payload for name, payload in calls if name == "link")
    assert link["account"] == JOURNAL_ACCOUNT
    assert link["account_type"] == JOURNAL_ACCOUNT_TYPE
    assert ("attach_journal", 1, "55", None) in _writes(fake_ledger)

    assert len(FakeRepo.runs) == 1
    run = FakeRepo.runs[0]
    assert run.broker == "kiwoom"
    assert run.dry_run is False
    assert run.committed_insert == 1
    assert run.unchanged == 0


async def test_full_mode_rerun_books_and_journals_nothing_new(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [_row(1, broker_order_id="0001")]
    FakeService.total = 1
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_journal(monkeypatch, calls)

    await _run(_filled_reader(), dry_run=False, terminal_only=False)
    calls.clear()
    second = await _run(_filled_reader(), dry_run=False, terminal_only=False)

    outcome = second["reconciled"][0]
    assert outcome["action"] == "noop_already_booked"
    assert outcome["delta_qty"] == "0"
    assert [name for name, _ in calls] == []
    assert len(FakeRepo.runs) == 2
    assert FakeRepo.runs[1].committed_insert == 0
    assert FakeRepo.runs[1].unchanged == 1
    assert second["unresolved_count"] == 0


async def test_full_mode_journal_failure_is_surfaced_after_the_booking(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [_row(1, broker_order_id="0001")]
    FakeService.total = 1
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_journal(monkeypatch, calls, fail=True)

    result = await _run(_filled_reader(), dry_run=False, terminal_only=False)

    outcome = result["reconciled"][0]
    assert outcome["action"] == "booked_filled"
    assert outcome["journal_error"] == "RuntimeError"
    assert outcome["requires_manual_review"] is True
    assert result["unresolved_count"] >= 1


async def test_full_mode_recovers_the_journal_of_an_already_filled_row(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    # The fill committed earlier; the journal step never finished.
    FakeService.rows = [
        _row(1, status="filled", filled_qty=10, journaled_qty=Decimal(0))
    ]
    FakeService.total = 1
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_journal(monkeypatch, calls)

    result = await _run(_filled_reader(), dry_run=False, terminal_only=False)

    outcome = result["reconciled"][0]
    assert outcome["action"] == "noop_already_booked"
    # The whole cumulative fill is journaled now, not a zero delta.
    save = next(payload for name, payload in calls if name == "save_order_fill")
    assert save["quantity"] == 10.0
    assert save["price"] == 70000.0
    assert ("attach_journal", 1, "55", None) in _writes(fake_ledger)
    assert result["unresolved_count"] == 0


async def test_full_mode_sell_closes_journals_under_kiwoom_mock(
    fake_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in FULL_GATE_ENVS:
        monkeypatch.setenv(name, "true")
    FakeService.rows = [_row(1, broker_order_id="0001", side="sell", journal_id=9)]
    FakeService.total = 1
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_journal(monkeypatch, calls)

    result = await _run(_filled_reader(), dry_run=False, terminal_only=False)

    outcome = result["reconciled"][0]
    assert outcome["action"] == "booked_filled"
    assert outcome["journal"] == {
        "trade_id": 501,
        "journals_closed": 1,
        "closed_journal_ids": [77],
        "realized_pnl_pct": 2.5,
        "realized_pnl_basis": "journal_entry",
        "journal_pnl_pct": 2.5,
    }
    close = next(payload for name, payload in calls if name == "close_journals")
    assert close["account"] == JOURNAL_ACCOUNT
    assert close["account_type"] == JOURNAL_ACCOUNT_TYPE
    assert close["sell_quantity"] == 10.0


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
