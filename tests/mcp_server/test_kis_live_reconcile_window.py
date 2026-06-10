# tests/mcp_server/test_kis_live_reconcile_window.py
"""ROB-487 — 증거 윈도우 확장 + NONE-verdict fail-closed + 빈 후보 UX.

TTTC8001R 은 '주문일' 기준 윈도우다 — 2026-06-10 라이브 read-only 프로브에서
20260610 윈도우에 6/9 주문이 0건이었다. 익일 reconcile 이 전일 체결을 보려면
INQR_STRT_DT 를 ledger 행의 주문일(created_at KST date)로 넓혀야 한다.
"""

import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import kis_live_ledger as mod
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
)

KST = datetime.timezone(datetime.timedelta(hours=9))
UTC = datetime.UTC


def _ledger_row(created_at, trade_date=None):
    return SimpleNamespace(
        id=19,
        order_no="0029287200",
        symbol="047810",
        side="buy",
        instrument_type="equity_kr",
        fee=0,
        currency="KRW",
        created_at=created_at,
        trade_date=trade_date,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        exit_reason=None,
        reason=None,
    )


# --- _order_date_kst ----------------------------------------------------------


def test_order_date_kst_converts_aware_utc_to_kst_date():
    # 6/9 15:31:25 KST 주문의 DB 저장형(UTC): 06:31:25Z
    row = _ledger_row(datetime.datetime(2026, 6, 9, 6, 31, 25, tzinfo=UTC))
    assert mod._order_date_kst(row) == datetime.date(2026, 6, 9)
    # KST 자정 경계: 6/9 16:30Z == 6/10 01:30 KST
    row = _ledger_row(datetime.datetime(2026, 6, 9, 16, 30, tzinfo=UTC))
    assert mod._order_date_kst(row) == datetime.date(2026, 6, 10)


def test_order_date_kst_naive_assumed_kst():
    # naive 는 KST 관례 (app/core/timezone.to_kst_naive 와 동일 가정)
    row = _ledger_row(datetime.datetime(2026, 6, 9, 15, 31, 25))
    assert mod._order_date_kst(row) == datetime.date(2026, 6, 9)


def test_order_date_kst_falls_back_to_trade_date():
    trade = datetime.datetime(2026, 6, 9, 15, 31, tzinfo=KST)
    row = _ledger_row(None, trade_date=trade)
    assert mod._order_date_kst(row) == datetime.date(2026, 6, 9)


def test_order_date_kst_none_when_underivable():
    assert mod._order_date_kst(_ledger_row(None)) is None


# --- _fetch_live_daily_rows window ---------------------------------------------


@pytest.mark.asyncio
async def test_fetch_live_daily_rows_widens_window_to_start_date():
    fake_client = AsyncMock()
    fake_client.inquire_daily_order_domestic = AsyncMock(return_value=[])
    with (
        patch.object(mod, "_create_live_kis_client", return_value=fake_client),
        patch.object(mod, "_today_yyyymmdd", return_value="20260610"),
    ):
        await mod._fetch_live_daily_rows(
            symbol="047810", order_no="0029287200", start_date="20260609"
        )
    kwargs = fake_client.inquire_daily_order_domestic.await_args.kwargs
    assert kwargs["start_date"] == "20260609"  # 주문일
    assert kwargs["end_date"] == "20260610"  # 오늘
    assert kwargs["is_mock"] is False


@pytest.mark.asyncio
async def test_fetch_live_daily_rows_defaults_to_today_window():
    fake_client = AsyncMock()
    fake_client.inquire_daily_order_domestic = AsyncMock(return_value=[])
    with (
        patch.object(mod, "_create_live_kis_client", return_value=fake_client),
        patch.object(mod, "_today_yyyymmdd", return_value="20260610"),
    ):
        await mod._fetch_live_daily_rows(symbol="047810", order_no="0029287200")
    kwargs = fake_client.inquire_daily_order_domestic.await_args.kwargs
    assert kwargs["start_date"] == "20260610"
    assert kwargs["end_date"] == "20260610"


@pytest.mark.asyncio
async def test_reconcile_passes_order_date_window_to_fetch():
    row = _ledger_row(datetime.datetime(2026, 6, 9, 6, 31, 25, tzinfo=UTC))
    filled = FillEvidence(
        FillVerdict.FILLED, Decimal("2"), Decimal("126000"), None, "filled", ""
    )
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=[])) as f,
        patch.object(mod, "classify_fill_evidence", return_value=filled),
    ):
        out = await mod._reconcile_one_ledger_row(row, dry_run=True)
    assert f.await_args.kwargs["start_date"] == "20260609"
    assert out["action"] == "would_book_filled"


# --- FillVerdict.NONE fail-closed ----------------------------------------------


@pytest.mark.asyncio
async def test_none_verdict_with_covered_window_marks_cancelled():
    # 윈도우가 주문일을 커버(start_date == 주문일)했고 행 부재 → cancelled 유지.
    row = _ledger_row(datetime.datetime(2026, 6, 9, 6, 31, 25, tzinfo=UTC))
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=[])),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(row, dry_run=False)
    assert out["action"] == "marked_cancelled"
    upd.assert_awaited_once()
    assert upd.call_args.kwargs["status"] == "cancelled"


@pytest.mark.asyncio
async def test_none_verdict_with_covered_window_dry_run_does_not_write():
    row = _ledger_row(datetime.datetime(2026, 6, 9, 6, 31, 25, tzinfo=UTC))
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=[])),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(row, dry_run=True)
    assert out["action"] == "would_mark_cancelled"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_none_verdict_without_order_date_refuses_terminal_mark():
    # (f) 주문일 도출 불가 → 윈도우 커버 증명 불가 → terminal 마킹 금지 (noop).
    row = _ledger_row(None)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=[])),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(row, dry_run=False)
    assert out["action"] == "noop_window_uncovered"
    assert "window" in out["reason"]
    upd.assert_not_awaited()


# --- 빈 후보 UX 메시지 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_impl_empty_candidates_message_is_distinguishable():
    # ROB-487 UX: "Reconciled 0" 이 누락으로 오인되지 않도록 후보 0건을 구분.
    with patch.object(mod, "_list_open_ledger_rows", AsyncMock(return_value=[])):
        out = await mod.kis_live_reconcile_orders_impl(dry_run=True)
    assert out["success"] is True
    assert out["counts"] == {}
    assert "No open candidates (all ledger rows terminal)" in out["message"]


@pytest.mark.asyncio
async def test_impl_nonempty_keeps_reconciled_message():
    with (
        patch.object(mod, "_list_open_ledger_rows", AsyncMock(return_value=[object()])),
        patch.object(
            mod,
            "_reconcile_one_ledger_row",
            AsyncMock(return_value={"verdict": "pending", "order_id": "A"}),
        ),
    ):
        out = await mod.kis_live_reconcile_orders_impl(dry_run=True)
    assert out["message"].startswith("Reconciled 1 live order(s)")
