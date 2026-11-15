"""hk 4754 (3797 T-P8) — US Kiwoom mock reconcile (unit, no database).

The ledger service is a recorder and the broker a fake reader, so the US lane's
contract is proven without a database or network:

* the scan is scoped to ``kiwoom_mock_us`` rows and reads only the US listings;
* the US write gate is its own env var — arming the KR gate never arms US;
* dry run issues zero writes and US verdicts come from the US row fields;
* a past ET trading day has no listing, so it is never closed from absence;
* an incomplete/truncated listing degrades every row to ``unknown``;
* a fill is reported (deferred) even though the US listings carry no price.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.brokers.kiwoom.mock_reconcile import (
    LedgerOrder,
    classify_verdict,
    us_evidence_from_payloads,
)
from app.services.kiwoom_mock_order_ledger_service import (
    ACCOUNT_MODE_KIWOOM_MOCK_US,
    us_trading_day,
)
from app.services.kiwoom_mock_reconcile import (
    TERMINAL_GATE_ENV,
    US_TERMINAL_GATE_ENV,
    kiwoom_mock_reconcile_orders_impl,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

TODAY = "20261116"
#: 2026-11-16 14:00 UTC = 09:00 ET (EST): inside the ET trading day 20261116.
NOW = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)
PAST_DAY = "20261113"


def _continuation(cont_yn: str = "N", next_key: str = "") -> dict[str, str]:
    return {"cont_yn": cont_yn, "next_key": next_key}


def _payload(*rows: dict[str, Any], cont_yn: str = "N") -> dict[str, Any]:
    return {
        "return_code": 0,
        "continuation": _continuation(cont_yn=cont_yn, next_key="k" * (cont_yn == "Y")),
        "result_list": list(rows),
    }


def _us_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ord_no": "000000063",
        "stk_cd": "NVDA",
        "ord_qty": "1",
        "cntr_qty": "0",
        "ord_remnq": "1",
        "ord_stat": "접수",
        "ord_cntr_tp": "0",
    }
    row.update(overrides)
    return row


def _row(
    ledger_id: int,
    *,
    broker_order_id: str = "000000063",
    status: str = "accepted",
    trading_day: str = TODAY,
    symbol: str = "NVDA",
    filled_qty: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=ledger_id,
        client_order_id=f"kwoomus-{ledger_id}",
        status=status,
        broker_order_id=broker_order_id,
        quantity=Decimal(1),
        filled_qty=None if filled_qty is None else Decimal(filled_qty),
        avg_fill_price=None,
        symbol=symbol,
        side="buy",
        trading_day=trading_day,
        instrument_type="equity_us",
        currency="USD",
        correlation_id=f"kiwoom-mock-us:{ledger_id}",
        thesis=None,
        strategy=None,
        reason=None,
        exit_reason=None,
        notes=None,
        indicators_snapshot=None,
        journal_id=None,
        trade_date=None,
    )


class FakeSession:
    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeService:
    """Records every write and the account mode it was asked for."""

    rows: list[Any] = []
    total: int = 0
    cancels: set[str] = set()
    #: Shared call log so tests can read it after the service instances are gone.
    sink: list[tuple[str, Any]] = []

    def __init__(self, db: Any) -> None:
        del db
        self.calls = type(self).sink

    async def list_reconcile_candidates(
        self,
        *,
        limit: int,
        account_mode: str = "kiwoom_mock",
        include_journal_recovery: bool = False,
    ):
        self.calls.append(("list", account_mode))
        return list(type(self).rows[:limit]), type(self).total

    async def list_acknowledged_cancels(
        self, *, original_order_ids: set[str], account_mode: str = "kiwoom_mock"
    ):
        self.calls.append(("cancels", account_mode))
        return type(self).cancels & original_order_ids

    async def mark_terminal(
        self, *, ledger_id, status, reason, detail=None, account_mode="kiwoom_mock"
    ):
        self.calls.append(("mark_terminal", account_mode, ledger_id, status, reason))
        return SimpleNamespace(id=ledger_id, status=status)

    async def promote_uncertain_to_accepted(
        self,
        *,
        ledger_id,
        broker_order_id,
        detail=None,
        account_mode="kiwoom_mock",
    ):
        self.calls.append(("promote", account_mode, ledger_id))
        return SimpleNamespace(id=ledger_id, status="accepted")


class FakeUsReader:
    """Serves the two US listings and records which reads were asked for."""

    def __init__(
        self,
        *,
        today_pages: list[dict[str, Any]] | None = None,
        open_pages: list[dict[str, Any]] | None = None,
        today_unreadable: bool = False,
        today_stuck: bool = False,
    ) -> None:
        self.today_pages = list(today_pages or [_payload()])
        self.open_pages = list(open_pages or [_payload()])
        self.today_unreadable = today_unreadable
        self.today_stuck = today_stuck
        self.calls: list[str] = []

    async def read_today_orders(self, *, cont_yn=None, next_key=None):
        del cont_yn, next_key
        self.calls.append("read_today_orders")
        if self.today_unreadable:
            return None
        if self.today_stuck and len(self.today_pages) == 1:
            # A continuation that never finishes: the page cap is the evidence.
            return self.today_pages[0]
        return self.today_pages.pop(0) if self.today_pages else _payload()

    async def read_open_orders(self, *, cont_yn=None, next_key=None):
        del cont_yn, next_key
        self.calls.append("read_open_orders")
        return self.open_pages.pop(0) if self.open_pages else _payload()

    async def read_order_status(
        self, *, cont_yn=None, next_key=None
    ):  # pragma: no cover
        raise AssertionError("the US scan must never read a KR listing")

    async def read_order_detail(
        self, *, order_date, cont_yn=None, next_key=None
    ):  # pragma: no cover
        raise AssertionError("the US scan must never read a KR listing")


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> type[FakeService]:
    FakeService.rows = []
    FakeService.total = 0
    FakeService.cancels = set()
    FakeService.sink = []
    monkeypatch.setattr(
        "app.services.kiwoom_mock_reconcile.KiwoomMockOrderLedgerService",
        FakeService,
    )
    return FakeService


async def _run(reader: FakeUsReader, **kwargs: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "market": "us",
        "session_factory": FakeSession,
        "reader": reader,
        "now": NOW,
        "min_request_interval_seconds": 0.0,
    }
    params.update(kwargs)
    return await kiwoom_mock_reconcile_orders_impl(**params)


# --- the trading day the scan uses -----------------------------------------


async def test_us_scan_uses_the_et_trading_day(fake_service: type[FakeService]) -> None:
    fake_service.rows = [_row(1)]
    fake_service.total = 1
    reader = FakeUsReader(today_pages=[_payload(_us_row())])

    result = await _run(reader)

    assert us_trading_day(NOW) == TODAY
    assert result["market"] == "us"
    assert result["account_mode"] == ACCOUNT_MODE_KIWOOM_MOCK_US
    assert ("list", ACCOUNT_MODE_KIWOOM_MOCK_US) in fake_service.sink
    assert ("cancels", ACCOUNT_MODE_KIWOOM_MOCK_US) in fake_service.sink


async def test_us_scan_reads_only_the_us_listings(
    fake_service: type[FakeService],
) -> None:
    fake_service.rows = [_row(1)]
    fake_service.total = 1
    reader = FakeUsReader(today_pages=[_payload(_us_row())])

    await _run(reader)

    assert reader.calls == ["read_today_orders", "read_open_orders"]


# --- verdicts ---------------------------------------------------------------


async def test_us_dry_run_reports_verdicts_and_writes_nothing(
    fake_service: type[FakeService],
) -> None:
    fake_service.rows = [
        _row(1, broker_order_id="000000063"),  # pending
        _row(2, broker_order_id="000000064"),  # filled
        _row(3, broker_order_id="000000065"),  # cancel confirmation row
    ]
    fake_service.total = 3
    fake_service.cancels = {"000000065"}
    reader = FakeUsReader(
        today_pages=[
            _payload(
                _us_row(),
                _us_row(ord_no="000000064", cntr_qty="1", ord_remnq="0"),
                _us_row(
                    ord_no="000000065",
                    ord_cntr_tp="12",
                    ord_stat="취소완료",
                    ord_remnq="0",
                ),
            )
        ]
    )

    result = await _run(reader)

    assert result["success"] is True
    actions = {row["ledger_id"]: row["action"] for row in result["reconciled"]}
    assert actions == {
        1: "noop_pending",
        2: "deferred_fill_booking",
        3: "would_mark_cancelled",
    }
    # A fill has no price in the US listings, but it is still a fill.
    filled = next(r for r in result["reconciled"] if r["ledger_id"] == 2)
    assert filled["verdict"] == "filled"
    assert filled["filled_qty"] == 1
    assert "avg_price" not in filled


async def test_us_terminal_only_applies_with_the_us_gate(
    monkeypatch: pytest.MonkeyPatch, fake_service: type[FakeService]
) -> None:
    monkeypatch.delenv(TERMINAL_GATE_ENV, raising=False)
    monkeypatch.setenv(US_TERMINAL_GATE_ENV, "true")
    fake_service.rows = [_row(1, broker_order_id="000000064")]
    fake_service.total = 1
    reader = FakeUsReader(
        today_pages=[
            _payload(
                _us_row(
                    ord_no="000000064", ord_stat="거부", ord_remnq="0", cntr_qty="0"
                )
            )
        ]
    )

    result = await _run(reader, dry_run=False, terminal_only=True)

    assert result["success"] is True
    assert result["reconciled"][0]["action"] == "mark_rejected"
    marks = [c for c in fake_service.sink if c[0] == "mark_terminal"]
    assert marks and marks[0][1] == ACCOUNT_MODE_KIWOOM_MOCK_US


async def test_us_terminal_only_defers_a_settled_partial_instead_of_closing_it(
    monkeypatch: pytest.MonkeyPatch, fake_service: type[FakeService]
) -> None:
    """A US fill is never booked here, so the settled partial must stay open."""
    monkeypatch.setenv(US_TERMINAL_GATE_ENV, "true")
    row = _row(1, broker_order_id="000000064")
    row.quantity = Decimal(10)
    fake_service.rows = [row]
    fake_service.total = 1
    reader = FakeUsReader(
        today_pages=[
            _payload(
                _us_row(
                    ord_no="000000064",
                    ord_qty="10",
                    cntr_qty="4",
                    ord_remnq="0",
                    ord_stat="Expired",
                )
            )
        ]
    )

    result = await _run(reader, dry_run=False, terminal_only=True)

    outcome = result["reconciled"][0]
    assert outcome["verdict"] == "expired"
    assert outcome["filled_qty"] == 4
    assert outcome["action"] == "deferred_fill_booking"
    assert outcome["requires_session_reconcile"] is True
    # Nothing was closed or booked: only the reads touched the ledger service.
    assert [call[0] for call in fake_service.sink] == ["list", "cancels"]
    assert result["unresolved_count"] >= 1


async def test_us_terminal_only_refuses_without_the_us_gate(
    monkeypatch: pytest.MonkeyPatch, fake_service: type[FakeService]
) -> None:
    # Arming the KR gate must not arm the US account.
    monkeypatch.setenv(TERMINAL_GATE_ENV, "true")
    monkeypatch.delenv(US_TERMINAL_GATE_ENV, raising=False)
    fake_service.rows = [_row(1)]
    fake_service.total = 1

    result = await _run(FakeUsReader(), dry_run=False, terminal_only=True)

    assert result["success"] is False
    assert result["error"] == "terminal_gate_disabled"
    assert result["market"] == "us"
    assert result["account_mode"] == ACCOUNT_MODE_KIWOOM_MOCK_US


async def test_us_past_day_row_is_never_closed_from_absence(
    fake_service: type[FakeService],
) -> None:
    fake_service.rows = [_row(1, trading_day=PAST_DAY)]
    fake_service.total = 1

    result = await _run(FakeUsReader())

    row = result["reconciled"][0]
    assert row["verdict"] == "unknown"
    assert row["action"] == "noop_no_evidence"
    assert row["requires_manual_review"] is True


async def test_us_truncated_listing_degrades_every_row(
    fake_service: type[FakeService],
) -> None:
    fake_service.rows = [_row(1)]
    fake_service.total = 1
    reader = FakeUsReader(
        today_pages=[_payload(_us_row(), cont_yn="Y")], today_stuck=True
    )

    result = await _run(reader)

    row = result["reconciled"][0]
    assert row["verdict"] == "unknown"
    # The fake keeps returning "more pages", so the cap is what stops the read.
    assert row["reason"] in {
        "us_today_orders_listing_truncated",
        "us_today_orders_page_cap_reached",
        "us_today_orders_unparseable",
        "us_today_orders_broker_error",
        "us_today_orders_unreadable",
    }


async def test_us_unreadable_listing_degrades_every_row(
    fake_service: type[FakeService],
) -> None:
    fake_service.rows = [_row(1)]
    fake_service.total = 1

    result = await _run(FakeUsReader(today_unreadable=True))

    row = result["reconciled"][0]
    assert row["verdict"] == "unknown"
    assert row["reason"] == "us_today_orders_unreadable"


async def test_unknown_market_is_refused(
    fake_service: type[FakeService],
) -> None:
    with pytest.raises(ValueError):
        await _run(FakeUsReader(), market="jp")


# --- the pure US evidence model --------------------------------------------


def test_us_evidence_needs_every_consulted_listing() -> None:
    complete = us_evidence_from_payloads(
        today_orders=_payload(_us_row()), open_orders=_payload()
    )
    assert complete.complete is True
    assert complete.rows[0].role == "detail"

    missing = us_evidence_from_payloads(today_orders=None, open_orders=_payload())
    assert missing.complete is False
    assert missing.reason == "us_today_orders_unreadable"

    truncated = us_evidence_from_payloads(
        today_orders=_payload(), open_orders=_payload(cont_yn="Y")
    )
    assert truncated.complete is False
    assert truncated.reason == "us_open_orders_listing_truncated"


def test_us_evidence_maps_the_observed_row_fields() -> None:
    evidence = us_evidence_from_payloads(
        today_orders=_payload(
            _us_row(
                ord_no="000000619",
                stk_cd="nvda",
                ord_qty="3",
                cntr_qty="1",
                ord_remnq="2",
                ord_stat="접수",
            )
        ),
        open_orders=_payload(),
    )

    row = evidence.rows[0]
    assert row.order_id == "000000619"
    assert row.symbol == "NVDA"
    assert row.ordered_quantity == 3
    assert row.filled_quantity == 1
    assert row.remaining_quantity == 2
    assert row.average_price is None  # the US listings carry no fill price
    assert row.marker is None


def test_us_evidence_refuses_an_unparseable_row() -> None:
    evidence = us_evidence_from_payloads(
        today_orders=_payload({"stk_cd": "NVDA", "ord_qty": "1"}),
        open_orders=_payload(),
    )
    assert evidence.complete is False
    assert evidence.reason == "broker_rows_unparseable"


def _classify(
    *,
    status: str = "accepted",
    ordered: int = 1,
    filled: int | None = 0,
    remaining: int | None = 0,
    marker_text: str = "",
    cancel_row: bool = False,
    own_cancel: bool = False,
    require_fill_price: bool = False,
):
    overrides: dict[str, Any] = {
        "ord_qty": str(ordered),
        "ord_stat": marker_text or ("취소완료" if cancel_row else "접수"),
        "ord_cntr_tp": "12" if cancel_row else "0",
    }
    if filled is not None:
        overrides["cntr_qty"] = str(filled)
    if remaining is not None:
        overrides["ord_remnq"] = str(remaining)
    return classify_verdict(
        ledger=LedgerOrder(
            client_order_id="kwoomus-1",
            status=status,
            broker_order_id="000000063",
            quantity=ordered,
            symbol="NVDA",
        ),
        evidence=us_evidence_from_payloads(
            today_orders=_payload(_us_row(**overrides)),
            open_orders=_payload(),
        ),
        own_cancel_acknowledged=own_cancel,
        require_fill_price=require_fill_price,
    )


def test_us_cancelled_needs_our_own_acknowledged_cancel() -> None:
    without = _classify(cancel_row=True)
    assert without.verdict == "unknown"
    assert without.reason == "cancel_without_own_acknowledged_request"

    confirmed = _classify(cancel_row=True, own_cancel=True)
    assert confirmed.verdict == "cancelled"


def test_us_reject_and_expiry_need_a_positive_marker() -> None:
    assert _classify(marker_text="거부").verdict == "rejected"
    assert _classify(marker_text="만료").verdict == "expired"
    # A no-fill terminal row without a recognised marker is never guessed.
    unmapped = _classify(marker_text="")
    assert unmapped.verdict == "unknown"
    assert unmapped.reason == "terminal_without_reason_marker"


def test_us_ambiguous_marker_is_not_evidence() -> None:
    verdict = _classify(marker_text="취소/만료")
    assert verdict.verdict == "unknown"
    assert verdict.reason == "ambiguous_marker_text"


def test_us_pending_and_partial() -> None:
    pending = _classify(filled=0, remaining=1)
    assert pending.verdict == "pending"

    partial = _classify(ordered=3, filled=1, remaining=2)
    assert partial.verdict == "partial"
    assert partial.filled_quantity == 1


def test_us_fill_price_is_required_only_when_the_caller_books() -> None:
    booked = _classify(filled=1, remaining=0, require_fill_price=True)
    assert booked.verdict == "unknown"
    assert booked.reason == "fill_price_unparseable"

    reported = _classify(filled=1, remaining=0, require_fill_price=False)
    assert reported.verdict == "filled"
    assert reported.average_price is None
