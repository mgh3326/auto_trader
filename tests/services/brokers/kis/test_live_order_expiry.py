# tests/services/brokers/kis/test_live_order_expiry.py
"""ROB-487 — 실 TTTC8001R 행 형태 기반 day-order expiry 분류 + NXT 마감 술어.

라이브 read-only 프로브(2026-06-10, 윈도우 20260608/09/10)로 확정된 실 행 키:
odno / orgn_odno / ord_qty / tot_ccld_qty / rjct_qty / rmn_qty / cncl_yn /
sll_buy_dvsn_cd_name / excg_id_dvsn_cd ... — prcs_stat_name 과
rvse_cncl_dvsn_cd / rvse_cncl_dvsn_name 은 존재하지 않는다.
미체결 SOR day order 는 EOD 에 rjct_qty == ord_qty (tot_ccld_qty == 0).
"""

import datetime

from app.services.brokers.kis.live_order_expiry import (
    classify_day_order_expiry,
    nxt_session_closed,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def _live_row(**overrides):
    """실측 TTTC8001R 행 형태 (KAI 047810 6/9 사례를 기본값으로)."""
    row = {
        "odno": "0029287200",
        "orgn_odno": "0000000000",
        "pdno": "047810",
        "prdt_name": "한국항공우주",
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


# --- nxt_session_closed (ROB-487) ----------------------------------------------


def test_nxt_open_before_2000_kst_same_day():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 19, 59, tzinfo=KST),
        )
        is False
    )


def test_nxt_closed_at_exactly_2000_kst():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 20, 0, tzinfo=KST),
        )
        is True
    )


def test_nxt_closed_next_day_morning():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 10, 9, 3, tzinfo=KST),
        )
        is True
    )


def test_nxt_naive_now_assumed_kst():
    # naive now 는 KST 관례 (app/core/timezone.to_kst_naive 와 동일 가정)
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 19, 2),
        )
        is False
    )


def test_nxt_utc_aware_now_converted():
    # 6/9 11:30 UTC == 6/9 20:30 KST → closed
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 11, 30, tzinfo=datetime.UTC),
        )
        is True
    )


# --- classify_day_order_expiry ---------------------------------------------------


def test_unfilled_sor_before_nxt_close_stays_pending():
    # (b) 15:31~19:59 KST 미체결 SOR — 6/9 19:02 조기 expiry 재발 방지.
    rows = [_live_row(rjct_qty="0", rmn_qty="2")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=False)
        == "pending"
    )


def test_full_rjct_qty_after_nxt_close_is_expired():
    # (c) 20:00 이후 + rjct_qty == ord_qty > 0 (EOD 만료의 실측 형태).
    rows = [_live_row(ord_qty="2", rjct_qty="2", rmn_qty="0")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=True)
        == "expired"
    )


def test_full_rjct_qty_before_nxt_close_stays_pending():
    # rjct_qty 가 장중에 채워지는 시점 미확인 → 20:00 전에는 fail-closed.
    rows = [_live_row(ord_qty="2", rjct_qty="2", rmn_qty="0")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=False)
        == "pending"
    )


def test_partial_rjct_after_close_stays_pending():
    rows = [_live_row(ord_qty="10", rjct_qty="6", rmn_qty="0")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=True)
        == "pending"
    )


def test_no_informative_evidence_after_close_stays_pending():
    # 순수 time-guard 폐기 회귀: EOD 배치 전(rjct=0, rmn>0)이면 20:00 후에도 pending.
    rows = [_live_row(ord_qty="2", rjct_qty="0", rmn_qty="2")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=True)
        == "pending"
    )


def test_cncl_yn_truthy_is_cancelled_any_time():
    rows = [_live_row(cncl_yn="Y", rmn_qty="0")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=False)
        == "cancelled"
    )


def test_cancel_confirm_row_matched_via_orgn_odno():
    # (e) 취소확인 행: 새 odno + orgn_odno == 원주문 + '매수취소'.
    rows = [
        _live_row(rjct_qty="0", rmn_qty="0"),
        _live_row(
            odno="0029999999",
            orgn_odno="0029287200",
            sll_buy_dvsn_cd_name="매수취소",
            rmn_qty="0",
        ),
    ]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=False)
        == "cancelled"
    )


def test_modify_confirm_row_is_not_cancel_evidence():
    rows = [
        _live_row(rjct_qty="0", rmn_qty="2"),
        _live_row(
            odno="0029999999",
            orgn_odno="0029287200",
            sll_buy_dvsn_cd_name="매수정정",
        ),
    ]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=False)
        == "pending"
    )


def test_duplicated_pagination_rows_do_not_change_verdicts():
    # (d) 실측: 모든 행이 정확히 2회 반환(32행/16 unique) — any-row 술어라 멱등.
    expired_single = [_live_row(ord_qty="2", rjct_qty="2", rmn_qty="0")]
    for rows in (expired_single, expired_single * 2):
        assert (
            classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=True)
            == "expired"
        )
    cancel_single = [_live_row(cncl_yn="Y")]
    assert (
        classify_day_order_expiry(
            rows=cancel_single * 2, order_no="0029287200", nxt_closed=False
        )
        == "cancelled"
    )


def test_no_matching_row_stays_pending():
    # 해당 주문 행 없음 → 이 분기 책임 아님 (NONE-verdict 경로가 처리).
    rows = [_live_row(odno="9999999999", orgn_odno="0000000000")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0029287200", nxt_closed=True)
        == "pending"
    )


def test_order_no_leading_zero_normalization_matches():
    # fill_evidence 와 동일한 정규화 — 구 분류기의 exact-match 불일치 해소.
    rows = [_live_row(ord_qty="2", rjct_qty="2")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="29287200", nxt_closed=True)
        == "expired"
    )


def test_missing_order_no_stays_pending():
    assert (
        classify_day_order_expiry(rows=[_live_row()], order_no=None, nxt_closed=True)
        == "pending"
    )
