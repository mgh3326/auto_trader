"""hk 4722 (3797 T-P3) — Kiwoom mock reconcile kernel (unit, no database).

The ledger service is replaced by a recorder and the broker by a fake reader, so
the kernel's contract is proven without a database or network:

* dry run issues zero writes;
* terminal-only applies only expired/cancelled/rejected and uncertain→accepted;
* writes are impossible without both ``terminal_only`` and the exact-true gate;
* fills are never booked (T-P4), unknown rows never transition;
* paged listings are followed, and a page cap is incomplete evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import kiwoom_mock_reconcile as kernel
from app.services.kiwoom_mock_reconcile import (
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
) -> SimpleNamespace:
    return SimpleNamespace(
        id=ledger_id,
        client_order_id=f"kwoomm-{ledger_id}",
        status=status,
        broker_order_id=broker_order_id,
        quantity=Decimal(10),
        filled_qty=None if filled_qty is None else Decimal(filled_qty),
        symbol=symbol,
        side="buy",
        trading_day=trading_day,
    )


class FakeSession:
    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeService:
    """Records every write; reads come from class-level fixtures."""

    rows: list[Any] = []
    total: int = 0
    cancels: set[str] = set()

    def __init__(self, db: Any) -> None:
        self.db = db
        self.writes: list[tuple[str, int, str, str | None]] = []

    async def list_reconcile_candidates(self, *, limit: int):
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
    written: list[FakeService] = []

    class Recorder(FakeService):
        def __init__(self, db: Any) -> None:
            super().__init__(db)
            written.append(self)

    monkeypatch.setattr(kernel, "KiwoomMockOrderLedgerService", Recorder)
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


async def test_full_mode_writes_are_refused(fake_ledger) -> None:
    # dry_run=False without terminal_only must never reach a write path.
    with pytest.raises(ValueError):
        await _run(FakeReader(), dry_run=False, terminal_only=False)
    assert _writes(fake_ledger) == []


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
