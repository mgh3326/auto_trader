"""hk 5419 — cancel/modify confirmation rows name their original order.

The broker gives a cancel or modify its own new order number and names the
order it acted on only through ``ori_ord`` (kt00007) / ``orig_ord_no`` (kt00009,
ust21050, ust21510). 11-19 mock measurement: 0159707 accepted, cancel
confirmation row 0159729 -> the original was left ``unknown`` because its own
rows carried no marker. These fixtures pin that the confirmation row is
attributed to the original, and only to it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.services.brokers.kiwoom.mock_reconcile import (
    LedgerOrder,
    classify_verdict,
    evidence_from_payloads,
    us_evidence_from_payloads,
    us_listing_rows,
)

pytestmark = pytest.mark.unit

KR_ORIGINAL = "0159707"
KR_CONFIRM = "0159729"
KR_SYMBOL = "005930"
US_ORIGINAL = "000000063"
US_CONFIRM = "000000064"


def _cont() -> dict[str, str]:
    return {"cont_yn": "N", "next_key": ""}


def _kr_detail(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ord_no": KR_ORIGINAL,
        "stk_cd": KR_SYMBOL,
        "ord_qty": "1",
        "cntr_qty": "0",
        "ord_remnq": "0",
        "ord_uv": "70000",
        "cntr_uv": "0",
        "mdfy_cncl": "",
        "ori_ord": "0000000",
        "dmst_stex_tp": "KRX",
    }
    row.update(overrides)
    return row


def _kr_status(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ord_no": KR_ORIGINAL,
        "stk_cd": KR_SYMBOL,
        "ord_qty": "1",
        "cntr_qty": "0",
        "ord_uv": "70000",
        "cntr_uv": "0",
        "mdfy_cncl_tp": "",
        "orig_ord_no": "0000000",
    }
    row.update(overrides)
    return row


def _kr_detail_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "return_code": 0,
        "continuation": _cont(),
        "acnt_ord_cntr_prps_dtl": list(rows),
    }


def _kr_status_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "return_code": 0,
        "continuation": _cont(),
        "acnt_ord_cntr_prst_array": list(rows),
    }


def _kr_verdict(
    *,
    detail: list[dict[str, Any]],
    status: list[dict[str, Any]] | None = None,
    own_cancel: bool = True,
    ledger_status: str = "accepted",
):
    return classify_verdict(
        ledger=LedgerOrder(
            client_order_id="kwoomm-1",
            status=ledger_status,
            broker_order_id=KR_ORIGINAL,
            quantity=1,
            symbol=KR_SYMBOL,
        ),
        evidence=evidence_from_payloads(
            order_status=_kr_status_payload(*(status or [])),
            order_detail=_kr_detail_payload(*detail),
        ),
        own_cancel_acknowledged=own_cancel,
    )


def _kr_cancel_confirm_detail(**overrides: Any) -> dict[str, Any]:
    return _kr_detail(
        ord_no=KR_CONFIRM, ori_ord=KR_ORIGINAL, mdfy_cncl="취소확인", **overrides
    )


# --- KR kt00007 / kt00009 ---------------------------------------------------


def test_kr_before_the_fix_shape_has_no_marker_on_the_original_alone() -> None:
    """The measured failure: without the confirmation row the original is stuck."""
    verdict = _kr_verdict(detail=[_kr_detail()])
    assert verdict.verdict == "unknown"
    assert verdict.reason == "terminal_without_reason_marker"


def test_kr_cancel_confirmation_row_closes_the_original_via_kt00007() -> None:
    verdict = _kr_verdict(detail=[_kr_detail(), _kr_cancel_confirm_detail()])
    assert verdict.verdict == "cancelled"
    assert verdict.requires_manual_review is False


def test_kr_cancel_confirmation_row_closes_the_original_via_kt00009() -> None:
    verdict = _kr_verdict(
        detail=[_kr_detail()],
        status=[
            _kr_status(),
            _kr_status(
                ord_no=KR_CONFIRM,
                orig_ord_no=KR_ORIGINAL,
                mdfy_cncl_tp="취소주문",
                cnfm_qty="1",
            ),
        ],
    )
    assert verdict.verdict == "cancelled"


def test_kr_attribution_keeps_the_own_acknowledged_cancel_requirement() -> None:
    verdict = _kr_verdict(
        detail=[_kr_detail(), _kr_cancel_confirm_detail()], own_cancel=False
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "cancel_without_own_acknowledged_request"


@pytest.mark.parametrize("none_value", ["0000000", "", "0", None])
def test_kr_no_original_order_is_not_attributed(none_value: str | None) -> None:
    confirm = _kr_cancel_confirm_detail()
    confirm["ori_ord"] = none_value
    verdict = _kr_verdict(detail=[_kr_detail(), confirm])
    assert verdict.verdict == "unknown"
    assert verdict.reason == "terminal_without_reason_marker"


def test_kr_confirmation_of_another_order_is_not_attributed() -> None:
    other = _kr_cancel_confirm_detail()
    other["ori_ord"] = "0159000"
    verdict = _kr_verdict(detail=[_kr_detail(), other])
    assert verdict.reason == "terminal_without_reason_marker"


def test_kr_confirmation_with_another_symbol_is_an_attribute_mismatch() -> None:
    verdict = _kr_verdict(
        detail=[_kr_detail(), _kr_cancel_confirm_detail(stk_cd="A000660")]
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "attribute_mismatch"


def test_kr_non_numeric_original_order_makes_the_evidence_incomplete() -> None:
    confirm = _kr_cancel_confirm_detail()
    confirm["ori_ord"] = "abc"
    evidence = evidence_from_payloads(
        order_status=_kr_status_payload(),
        order_detail=_kr_detail_payload(_kr_detail(), confirm),
    )
    assert evidence.complete is False
    assert evidence.reason == "broker_rows_unparseable"


def test_kr_cancel_confirmation_does_not_close_an_order_that_is_still_working() -> None:
    verdict = _kr_verdict(
        detail=[_kr_detail(ord_remnq="1"), _kr_cancel_confirm_detail()]
    )
    assert verdict.verdict == "pending"


def test_kr_recorded_fill_still_contradicts_an_attributed_cancel() -> None:
    verdict = classify_verdict(
        ledger=LedgerOrder(
            client_order_id="kwoomm-1",
            status="accepted",
            broker_order_id=KR_ORIGINAL,
            quantity=1,
            symbol=KR_SYMBOL,
            recorded_filled_quantity=1,
        ),
        evidence=evidence_from_payloads(
            order_status=_kr_status_payload(),
            order_detail=_kr_detail_payload(_kr_detail(), _kr_cancel_confirm_detail()),
        ),
        own_cancel_acknowledged=True,
    )
    assert verdict.reason == "recorded_fill_contradicts_broker"


def test_kr_modify_confirmation_is_reported_not_closed_as_a_cancel() -> None:
    verdict = _kr_verdict(
        detail=[
            _kr_detail(),
            _kr_detail(ord_no=KR_CONFIRM, ori_ord=KR_ORIGINAL, mdfy_cncl="정정확인"),
        ]
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "replaced_by_modification"
    assert verdict.requires_manual_review is True


def test_kr_cancel_and_modify_text_together_is_unresolved() -> None:
    verdict = _kr_verdict(
        detail=[
            _kr_detail(),
            _kr_detail(
                ord_no=KR_CONFIRM, ori_ord=KR_ORIGINAL, mdfy_cncl="취소/정정확인"
            ),
        ]
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "child_operation_unresolved"


@pytest.mark.parametrize("child_text", ["거부", "만료", "취소/만료"])
def test_kr_rejected_or_expired_child_operation_is_not_the_originals_marker(
    child_text: str,
) -> None:
    verdict = _kr_verdict(
        detail=[
            _kr_detail(),
            _kr_detail(ord_no=KR_CONFIRM, ori_ord=KR_ORIGINAL, mdfy_cncl=child_text),
        ],
        own_cancel=True,
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "child_operation_unresolved"


def test_kr_child_reject_does_not_hide_the_originals_own_terminal_marker() -> None:
    verdict = _kr_verdict(
        detail=[
            _kr_detail(mdfy_cncl="만료"),
            _kr_detail(ord_no=KR_CONFIRM, ori_ord=KR_ORIGINAL, mdfy_cncl="거부"),
        ]
    )
    assert verdict.verdict == "expired"


def test_kr_conflicting_confirmation_markers_are_unknown() -> None:
    verdict = _kr_verdict(
        detail=[_kr_detail(mdfy_cncl="만료"), _kr_cancel_confirm_detail()]
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "conflicting_terminal_markers"


def test_kr_uncertain_order_with_a_cancel_confirmation_is_not_promoted() -> None:
    verdict = _kr_verdict(
        detail=[_kr_detail(ord_remnq="1"), _kr_cancel_confirm_detail()],
        ledger_status="uncertain",
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "uncertain_broker_terminal_evidence"


# --- US ust21510 / ust21050 -------------------------------------------------


def _us_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ord_no": US_ORIGINAL,
        "orig_ord_no": "000000000",
        "stk_cd": "NVDA",
        "ord_qty": "1",
        "cntr_qty": "0",
        "cntr_uv": "0.0000",
        "ord_remnq": "0",
        "ord_stat": "접수",
        "ord_cntr_tp": "10",
    }
    row.update(overrides)
    return row


def _us_payload(*rows: dict[str, Any], key: str = "result_list") -> dict[str, Any]:
    return {"return_code": 0, "continuation": _cont(), key: list(rows)}


def _us_verdict(
    today: list[dict[str, Any]],
    open_: list[dict[str, Any]] | None = None,
    *,
    own_cancel: bool = True,
    quantity: int = 1,
):
    return classify_verdict(
        ledger=LedgerOrder(
            client_order_id="kwoomus-1",
            status="accepted",
            broker_order_id=US_ORIGINAL,
            quantity=quantity,
            symbol="NVDA",
        ),
        evidence=us_evidence_from_payloads(
            today_orders=_us_payload(*today),
            open_orders=_us_payload(*(open_ or [])),
        ),
        own_cancel_acknowledged=own_cancel,
        require_fill_price=False,
    )


def _us_cancel_confirm(**overrides: Any) -> dict[str, Any]:
    return _us_row(
        ord_no=US_CONFIRM,
        orig_ord_no=US_ORIGINAL,
        ord_stat="취소완료",
        ord_cntr_tp="12",
        **overrides,
    )


def test_us_cancel_confirmation_row_closes_the_original_via_ust21510() -> None:
    verdict = _us_verdict([_us_row(), _us_cancel_confirm()])
    assert verdict.verdict == "cancelled"


def test_us_cancel_confirmation_row_closes_the_original_via_ust21050() -> None:
    verdict = _us_verdict([_us_row()], open_=[_us_cancel_confirm()])
    assert verdict.verdict == "cancelled"


def test_us_original_alone_has_no_marker() -> None:
    verdict = _us_verdict([_us_row()])
    assert verdict.verdict == "unknown"
    assert verdict.reason == "terminal_without_reason_marker"


def test_us_zero_original_order_is_not_attributed() -> None:
    confirm = _us_cancel_confirm()
    confirm["orig_ord_no"] = "000000000"
    verdict = _us_verdict([_us_row(), confirm])
    assert verdict.reason == "terminal_without_reason_marker"


def test_us_cancel_confirmation_keeps_the_own_cancel_requirement() -> None:
    verdict = _us_verdict([_us_row(), _us_cancel_confirm()], own_cancel=False)
    assert verdict.reason == "cancel_without_own_acknowledged_request"


def test_us_modify_confirmation_is_reported_not_closed() -> None:
    modify = _us_row(
        ord_no=US_CONFIRM,
        orig_ord_no=US_ORIGINAL,
        ord_stat="정정완료",
        ord_cntr_tp="11",
    )
    verdict = _us_verdict([_us_row(), modify])
    assert verdict.verdict == "unknown"
    assert verdict.reason == "replaced_by_modification"


def test_us_invalid_cancel_request_does_not_reject_the_original() -> None:
    """A rejected cancel (무효주문) is the child failing, not the original ending."""
    invalid_cancel = _us_row(
        ord_no=US_CONFIRM,
        orig_ord_no=US_ORIGINAL,
        ord_stat="무효주문",
        ord_cntr_tp="12",
    )
    for own_cancel in (True, False):
        verdict = _us_verdict([_us_row(), invalid_cancel], own_cancel=own_cancel)
        assert verdict.verdict == "unknown"
        assert verdict.reason == "child_operation_unresolved"


def test_us_expired_child_row_does_not_expire_the_original() -> None:
    child = _us_row(
        ord_no=US_CONFIRM, orig_ord_no=US_ORIGINAL, ord_stat="만료", ord_cntr_tp="12"
    )
    verdict = _us_verdict([_us_row(), child])
    assert verdict.verdict == "unknown"
    assert verdict.reason == "child_operation_unresolved"


# --- hk 5419 r2: request-only rows, disagreeing views, open remainder -------


def test_kr_cancel_request_row_is_not_a_confirmation() -> None:
    """kt00009 ``취소주문`` with no confirmed quantity only says "received"."""
    verdict = _kr_verdict(
        detail=[_kr_detail()],
        status=[
            _kr_status(),
            _kr_status(
                ord_no=KR_CONFIRM,
                orig_ord_no=KR_ORIGINAL,
                mdfy_cncl_tp="취소주문",
                cnfm_qty="0",
            ),
        ],
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "child_operation_unresolved"


def test_kr_detail_cancel_text_without_confirmation_is_not_a_confirmation() -> None:
    verdict = _kr_verdict(
        detail=[
            _kr_detail(),
            _kr_detail(ord_no=KR_CONFIRM, ori_ord=KR_ORIGINAL, mdfy_cncl="취소주문"),
        ]
    )
    assert verdict.reason == "child_operation_unresolved"


@pytest.mark.parametrize("status_text", ["거부", "취소/만료", "만료"])
def test_kr_child_views_that_disagree_fail_closed(status_text: str) -> None:
    """detail says 취소확인, status says the same child was rejected/expired."""
    verdict = _kr_verdict(
        detail=[_kr_detail(), _kr_cancel_confirm_detail()],
        status=[
            _kr_status(),
            _kr_status(
                ord_no=KR_CONFIRM,
                orig_ord_no=KR_ORIGINAL,
                mdfy_cncl_tp=status_text,
                cnfm_qty="1",
            ),
        ],
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "child_operation_unresolved"


def test_us_cancel_request_row_is_not_a_confirmation() -> None:
    request_only = _us_row(
        ord_no=US_CONFIRM, orig_ord_no=US_ORIGINAL, ord_stat="접수", ord_cntr_tp="12"
    )
    for own_cancel in (True, False):
        verdict = _us_verdict([_us_row(), request_only], own_cancel=own_cancel)
        assert verdict.verdict == "unknown"
        assert verdict.reason == "child_operation_unresolved"


def test_us_child_views_that_disagree_fail_closed() -> None:
    done = _us_cancel_confirm()
    rejected = _us_row(
        ord_no=US_CONFIRM, orig_ord_no=US_ORIGINAL, ord_stat="거부", ord_cntr_tp="12"
    )
    verdict = _us_verdict([_us_row(), done], open_=[rejected])
    assert verdict.verdict == "unknown"
    assert verdict.reason == "child_operation_unresolved"


def test_us_child_cancel_does_not_close_a_status_only_original_with_remainder() -> None:
    """ust21050 shows the original still open; the today listing lacks it."""
    verdict = _us_verdict(
        [_us_cancel_confirm()],
        open_=[_us_row(ord_remnq="1")],
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "original_remainder_unproven"


def test_us_child_cancel_closes_a_status_only_original_with_zero_remainder() -> None:
    verdict = _us_verdict(
        [_us_cancel_confirm()],
        open_=[_us_row(ord_remnq="0")],
    )
    assert verdict.verdict == "cancelled"


# --- hk 5419 r3: every child view, unresolved siblings, remainder agreement --


def test_us_completed_view_does_not_outrank_a_request_view_of_the_same_child() -> None:
    request = _us_row(
        ord_no=US_CONFIRM, orig_ord_no=US_ORIGINAL, ord_stat="접수", ord_cntr_tp="12"
    )
    verdict = _us_verdict([_us_row(), _us_cancel_confirm()], open_=[request])
    assert verdict.verdict == "unknown"
    assert verdict.reason == "child_operation_unresolved"


def test_kr_completed_view_does_not_outrank_a_modification_view_of_the_same_child() -> (
    None
):
    verdict = _kr_verdict(
        detail=[_kr_detail(), _kr_cancel_confirm_detail()],
        status=[
            _kr_status(),
            _kr_status(
                ord_no=KR_CONFIRM, orig_ord_no=KR_ORIGINAL, mdfy_cncl_tp="정정주문"
            ),
        ],
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "child_operation_unresolved"


def test_a_clean_child_does_not_outrank_an_unresolved_sibling() -> None:
    sibling_request = _us_row(
        ord_no="000000065", orig_ord_no=US_ORIGINAL, ord_stat="접수", ord_cntr_tp="12"
    )
    verdict = _us_verdict([_us_row(), _us_cancel_confirm(), sibling_request])
    assert verdict.verdict == "unknown"
    assert verdict.reason == "child_operation_unresolved"


def test_a_consistent_modification_alone_stays_replaced() -> None:
    modify = _us_row(
        ord_no=US_CONFIRM,
        orig_ord_no=US_ORIGINAL,
        ord_stat="정정완료",
        ord_cntr_tp="11",
    )
    verdict = _us_verdict([_us_row(), modify], open_=[modify])
    assert verdict.reason == "replaced_by_modification"


def test_us_two_original_views_that_disagree_on_remainder_fail_closed() -> None:
    """ust21510 says 0 left, ust21050 says 1 still working."""
    verdict = _us_verdict(
        [_us_row(), _us_cancel_confirm()], open_=[_us_row(ord_remnq="1")]
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "original_remainder_unproven"


def test_us_unreadable_status_remainder_fails_closed() -> None:
    open_row = _us_row()
    del open_row["ord_remnq"]
    verdict = _us_verdict([_us_row(), _us_cancel_confirm()], open_=[open_row])
    assert verdict.reason == "original_remainder_unproven"


def test_us_partial_fill_close_also_needs_the_remainder_agreement() -> None:
    filled = {"ord_qty": "2", "cntr_qty": "1", "cntr_uv": "155.2600"}
    today = [_us_row(**filled), _us_cancel_confirm()]
    disagreeing = _us_verdict(
        today, open_=[_us_row(**filled, ord_remnq="1")], quantity=2
    )
    assert disagreeing.verdict == "unknown"
    assert disagreeing.reason == "original_remainder_unproven"

    agreeing = _us_verdict(today, open_=[_us_row(**filled)], quantity=2)
    assert agreeing.verdict == "cancelled"
    assert agreeing.filled_quantity == 1
    assert agreeing.average_price == Decimal("155.2600")


def test_us_partial_fill_close_needs_the_acknowledged_cancel() -> None:
    filled = {"ord_qty": "2", "cntr_qty": "1", "cntr_uv": "155.2600"}
    verdict = _us_verdict(
        [_us_row(**filled), _us_cancel_confirm()], quantity=2, own_cancel=False
    )
    assert verdict.reason == "cancel_without_own_acknowledged_request"


def test_kr_derived_status_remainder_is_neutral_next_to_a_proven_detail_zero() -> None:
    """kt00009 derives its remainder (ordered - filled); it never contradicts."""
    verdict = _kr_verdict(
        detail=[_kr_detail(), _kr_cancel_confirm_detail()],
        status=[_kr_status()],
    )
    assert verdict.verdict == "cancelled"


def test_kr_detail_remainder_positive_never_closes_via_a_child() -> None:
    verdict = _kr_verdict(
        detail=[_kr_detail(ord_remnq="1"), _kr_cancel_confirm_detail()]
    )
    assert verdict.verdict == "pending"


def test_original_own_terminal_evidence_stands_despite_an_unresolved_child() -> None:
    request = _us_row(
        ord_no=US_CONFIRM, orig_ord_no=US_ORIGINAL, ord_stat="접수", ord_cntr_tp="12"
    )
    verdict = _us_verdict([_us_row(ord_stat="만료"), request])
    assert verdict.verdict == "expired"


# --- US 무효주문 -----------------------------------------------------------


def test_us_invalid_order_without_a_fill_is_a_no_fill_reject() -> None:
    verdict = _us_verdict([_us_row(ord_stat="무효주문")])
    assert verdict.verdict == "rejected"
    assert verdict.reason == "broker_reject_confirmed"
    assert verdict.filled_quantity is None


def test_us_invalid_order_with_a_fill_is_a_contradiction() -> None:
    verdict = _us_verdict(
        [_us_row(ord_stat="무효주문", cntr_qty="1", cntr_uv="155.2600")]
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "invalid_order_reports_fill"
    assert verdict.requires_manual_review is True


def test_us_invalid_order_contradicts_a_recorded_fill() -> None:
    verdict = classify_verdict(
        ledger=LedgerOrder(
            client_order_id="kwoomus-1",
            status="accepted",
            broker_order_id=US_ORIGINAL,
            quantity=1,
            symbol="NVDA",
            recorded_filled_quantity=1,
        ),
        evidence=us_evidence_from_payloads(
            today_orders=_us_payload(_us_row(ord_stat="무효주문")),
            open_orders=_us_payload(),
        ),
        own_cancel_acknowledged=False,
        require_fill_price=False,
    )
    assert verdict.reason == "recorded_fill_contradicts_broker"


def test_us_invalid_order_is_not_a_filled_order_even_with_a_price() -> None:
    row = _us_row(ord_stat="무효주문", cntr_qty="1", cntr_uv="155.2600")
    evidence = us_evidence_from_payloads(
        today_orders=_us_payload(row), open_orders=_us_payload()
    )
    assert evidence.rows[0].average_price == Decimal("155.2600")
    assert evidence.rows[0].invalid_order is True


# --- US result_list / result_lsit ------------------------------------------


def test_us_listing_rows_accepts_exactly_one_spelling_for_ust21510() -> None:
    rows = [{"ord_no": "1"}]
    assert us_listing_rows({"result_list": rows}, allow_typo_key=True) == rows
    assert us_listing_rows({"result_lsit": rows}, allow_typo_key=True) == rows
    assert (
        us_listing_rows({"result_list": rows, "result_lsit": rows}, allow_typo_key=True)
        is None
    )
    assert us_listing_rows({}, allow_typo_key=True) is None
    # ust21050 does not tolerate the typo.
    assert us_listing_rows({"result_lsit": rows}) is None


def test_us_evidence_fixtures_for_the_four_list_key_shapes() -> None:
    row = _us_row()
    good = us_evidence_from_payloads(
        today_orders=_us_payload(row), open_orders=_us_payload()
    )
    typo = us_evidence_from_payloads(
        today_orders=_us_payload(row, key="result_lsit"), open_orders=_us_payload()
    )
    both = us_evidence_from_payloads(
        today_orders={**_us_payload(row), "result_lsit": [row]},
        open_orders=_us_payload(),
    )
    neither = us_evidence_from_payloads(
        today_orders={"return_code": 0, "continuation": _cont()},
        open_orders=_us_payload(),
    )

    assert good.complete is True
    assert typo.complete is True
    assert typo.rows == good.rows
    for evidence in (both, neither):
        assert evidence.complete is False
        assert evidence.reason == "broker_rows_unparseable"


def test_us_open_orders_listing_does_not_accept_the_typo_key() -> None:
    evidence = us_evidence_from_payloads(
        today_orders=_us_payload(),
        open_orders=_us_payload(_us_row(), key="result_lsit"),
    )
    assert evidence.complete is False
