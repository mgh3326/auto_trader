# tests/mcp_server/test_kis_live_reconcile_expiry.py
"""ROB-476/ROB-487 — reconcile 커널: NXT-aware expiry + 실 row 형태 증거.

fixture 는 실 TTTC8001R 행 형태 (2026-06-10 라이브 read-only 프로브로 확정):
prcs_stat_name / rvse_cncl_dvsn_* 키는 존재하지 않는다. classify_fill_evidence /
classify_day_order_expiry / nxt_session_closed 는 실물을 사용한다 (self-fulfilling
mock 금지).
"""

import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import kis_live_ledger as mod

KST = datetime.timezone(datetime.timedelta(hours=9))

_CREATED_0609 = datetime.datetime(2026, 6, 9, 15, 31, 25, tzinfo=KST)


def _ledger_row(created_at=_CREATED_0609):
    return SimpleNamespace(
        id=19,
        order_no="0029287200",
        symbol="047810",
        side="buy",
        instrument_type="equity_kr",
        fee=0,
        currency="KRW",
        created_at=created_at,
        trade_date=created_at,
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


def _broker_row(**overrides):
    row = {
        "odno": "0029287200",
        "orgn_odno": "0000000000",
        "pdno": "047810",
        "sll_buy_dvsn_cd_name": "매수",
        "ord_qty": "2",
        "ord_unpr": "126000",
        "tot_ccld_qty": "0",
        "rjct_qty": "0",
        "rmn_qty": "2",
        "cncl_yn": "N",
        "excg_id_dvsn_cd": "SOR",
        "ord_tmd": "153125",
    }
    row.update(overrides)
    return row


def test_kernel_no_longer_uses_xkrx_session_classifier():
    # ROB-487: XKRX 15:30 분류기는 reconcile 경로에서 제거 — NXT 술어만 사용.
    assert not hasattr(mod, "kr_market_data_state")
    assert not hasattr(mod, "DATA_STATE_MARKET_CLOSED")
    assert hasattr(mod, "nxt_session_closed")


@pytest.mark.asyncio
async def test_known_order_date_uses_exact_date_daily_order_window():
    # 실 KIS TTTC8001R은 prior-day 주문에 대해 order_date..today multi-day
    # 조회가 KIER2570 으로 거부될 수 있다. ledger 주문일을 아는 reconcile은
    # start_date == end_date == 주문일로 조회해야 한다.
    calls = []

    class FakeKIS:
        async def inquire_daily_order_domestic(self, **kwargs):
            calls.append(kwargs)
            return []

    with (
        patch.object(mod, "_create_live_kis_client", return_value=FakeKIS()),
        patch.object(mod, "_today_yyyymmdd", return_value="20260610"),
    ):
        rows = await mod._fetch_live_daily_rows(
            symbol="035420",
            order_no="0038569700",
            order_trade_date=datetime.date(2026, 6, 8),
        )

    assert rows == []
    assert calls == [
        {
            "start_date": "20260608",
            "end_date": "20260608",
            "stock_code": "035420",
            "order_number": "0038569700",
            "is_mock": False,
        }
    ]


@pytest.mark.asyncio
async def test_unfilled_sor_during_nxt_session_stays_pending():
    # 6/9 19:02 KST 조기 expiry 재발 방지: NXT 마감(20:00) 전에는 expire 금지.
    rows = [_broker_row()]  # tot_ccld_qty=0, rjct_qty=0, rmn_qty=2 → 생존
    now = datetime.datetime(2026, 6, 9, 19, 2, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["verdict"] == "pending"
    assert out["action"] == "noop_pending"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_rjct_before_nxt_close_stays_pending():
    # rjct_qty 의 장중 채움 여부 미확인 → 20:00 전에는 fail-closed pending.
    rows = [_broker_row(rjct_qty="2", rmn_qty="0")]
    now = datetime.datetime(2026, 6, 9, 19, 59, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["action"] == "noop_pending"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_rjct_after_nxt_close_marks_expired():
    # (c) 20:00 이후 + rjct_qty == ord_qty 브로커 증거 → expired.
    rows = [_broker_row(rjct_qty="2", rmn_qty="0")]
    now = datetime.datetime(2026, 6, 9, 20, 5, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["verdict"] == "expired"
    assert out["action"] == "marked_expired"
    upd.assert_awaited_once()
    assert upd.call_args.kwargs["status"] == "expired"


@pytest.mark.asyncio
async def test_full_rjct_after_nxt_close_dry_run_does_not_write():
    rows = [_broker_row(rjct_qty="2", rmn_qty="0")]
    now = datetime.datetime(2026, 6, 9, 20, 5, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=True)
    assert out["verdict"] == "expired"
    assert out["action"] == "would_mark_expired"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_rjct_evidence_after_close_stays_pending():
    # 순수 time-guard 폐기 회귀: 20:00 후라도 broker 증거 없으면 expire 금지.
    rows = [_broker_row(rjct_qty="0", rmn_qty="2")]
    now = datetime.datetime(2026, 6, 9, 20, 5, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["action"] == "noop_pending"
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_next_day_reconcile_books_prior_day_fill_via_exact_order_date():
    # (a) 6/9 주문을 6/10 아침에 reconcile: 주문일 exact-date 조회
    # (order_trade_date) 로 전일 체결(tot_ccld_qty=2)을 보고 book 한다 —
    # KAI 047810 실측 형태.
    fill_row = _broker_row(tot_ccld_qty="2", rmn_qty="0", avg_prvs="126000")
    now = datetime.datetime(2026, 6, 10, 9, 3, tzinfo=KST)
    with (
        patch.object(
            mod, "_fetch_live_daily_rows", AsyncMock(return_value=[fill_row])
        ) as fetch,
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_save_order_fill", AsyncMock(return_value=107)) as m_fill,
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            AsyncMock(return_value={"journal_id": 64}),
        ),
        patch.object(mod, "_link_journal_to_fill", AsyncMock()),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert fetch.await_args.kwargs["order_trade_date"] == datetime.date(2026, 6, 9)
    assert out["verdict"] == "filled"
    assert out["action"] == "booked_filled"
    assert float(m_fill.await_args.kwargs["price"]) == 126000.0
    assert float(m_fill.await_args.kwargs["quantity"]) == 2.0
    assert upd.call_args.kwargs["status"] == "filled"
    assert upd.call_args.kwargs["filled_qty"] == Decimal("2")


@pytest.mark.asyncio
async def test_duplicated_pagination_rows_do_not_double_book():
    # (d) 실측 페이지네이션 중복(모든 행 2회) — fill_evidence._dedupe_rows 가
    # 보호함을 커널 경유로 회귀 고정 (2가 4로 이중계상되면 안 됨).
    fill_row = _broker_row(tot_ccld_qty="2", rmn_qty="0", avg_prvs="126000")
    rows = [fill_row, dict(fill_row)]
    now = datetime.datetime(2026, 6, 10, 9, 3, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_save_order_fill", AsyncMock(return_value=107)) as m_fill,
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            AsyncMock(return_value={"journal_id": 64}),
        ),
        patch.object(mod, "_link_journal_to_fill", AsyncMock()),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()),
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["verdict"] == "filled"
    assert float(m_fill.await_args.kwargs["quantity"]) == 2.0  # 4가 아침


@pytest.mark.asyncio
async def test_cancel_confirm_row_marks_cancelled_via_orgn_odno():
    # (e) 취소확인 행(신규 odno + orgn_odno == 원주문 + '매수취소') → cancelled.
    # 브로커 취소 증거는 시간 가드 불요 — NXT 마감 전이라도 즉시.
    rows = [
        _broker_row(rmn_qty="0"),
        _broker_row(
            odno="0029999999",
            orgn_odno="0029287200",
            sll_buy_dvsn_cd_name="매수취소",
            rmn_qty="0",
        ),
    ]
    now = datetime.datetime(2026, 6, 9, 16, 0, tzinfo=KST)
    with (
        patch.object(mod, "_fetch_live_daily_rows", AsyncMock(return_value=rows)),
        patch.object(mod, "now_kst", return_value=now),
        patch.object(mod, "_update_ledger_outcome", AsyncMock()) as upd,
    ):
        out = await mod._reconcile_one_ledger_row(_ledger_row(), dry_run=False)
    assert out["verdict"] == "cancelled"
    assert out["action"] == "marked_cancelled"
    upd.assert_awaited_once()
    assert upd.call_args.kwargs["status"] == "cancelled"
