"""hk 4722 (3797 T-P3) — pure Kiwoom mock terminal classifier (design §3.2).

Fixed broker-row fixtures, no I/O: every verdict of the design table is
reproduced here, plus the absence/ambiguity cases that must stay ``unknown``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.brokers.kiwoom.mock_reconcile import (
    LedgerOrder,
    classify_verdict,
    evidence_from_payloads,
)

pytestmark = pytest.mark.unit

SYMBOL = "005930"
ORDER_NO = "0001"


def _continuation(cont_yn: str = "N") -> dict[str, str]:
    return {"cont_yn": cont_yn, "next_key": ""}


def _detail_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ord_no": ORDER_NO,
        "stk_cd": SYMBOL,
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


def _status_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ord_no": ORDER_NO,
        "stk_cd": SYMBOL,
        "ord_qty": "10",
        "cntr_qty": "0",
        "ord_uv": "70000",
        "cntr_uv": "0",
        "mdfy_cncl_tp": "",
    }
    row.update(overrides)
    return row


def _detail_payload(*rows: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "return_code": 0,
        "continuation": _continuation(),
        "acnt_ord_cntr_prps_dtl": list(rows),
    }
    payload.update(overrides)
    return payload


def _status_payload(*rows: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "return_code": 0,
        "continuation": _continuation(),
        "acnt_ord_cntr_prst_array": list(rows),
    }
    payload.update(overrides)
    return payload


def _ledger(**overrides: Any) -> LedgerOrder:
    values: dict[str, Any] = {
        "client_order_id": "kwoomm-test",
        "status": "accepted",
        "broker_order_id": ORDER_NO,
        "quantity": 10,
        "symbol": SYMBOL,
    }
    values.update(overrides)
    return LedgerOrder(**values)


_UNSET = object()


def _classify(
    *,
    ledger: LedgerOrder | None = None,
    status: Any = _UNSET,
    detail: Any = _UNSET,
    own_cancel_acknowledged: bool = False,
    consult_status: bool = True,
):
    evidence = evidence_from_payloads(
        order_status=_status_payload() if status is _UNSET else status,
        order_detail=_detail_payload() if detail is _UNSET else detail,
        consult_order_status=consult_status,
    )
    return classify_verdict(
        ledger=ledger or _ledger(),
        evidence=evidence,
        own_cancel_acknowledged=own_cancel_acknowledged,
    )


# ---------------------------------------------------------------------------
# §3.2 verdicts from positive broker evidence.


def test_filled_needs_cumulative_quantity_and_parseable_price() -> None:
    verdict = _classify(
        detail=_detail_payload(
            _detail_row(cntr_qty="10", ord_remnq="0", cntr_uv="70100")
        )
    )
    assert verdict.verdict == "filled"
    assert verdict.reason == "broker_cumulative_fill"
    assert verdict.filled_quantity == 10
    assert verdict.average_price == 70100
    assert verdict.requires_manual_review is False


def test_partial_is_open_but_reported() -> None:
    verdict = _classify(
        detail=_detail_payload(
            _detail_row(cntr_qty="4", ord_remnq="6", cntr_uv="70050")
        )
    )
    assert verdict.verdict == "partial"
    assert verdict.filled_quantity == 4


def test_settled_partial_fill_is_terminal_with_its_fill() -> None:
    """4 of 10 filled and nothing left working: the fill rides the terminal row.

    The journal is written only at the terminal fill (hk 4734), so a settled
    partial that stayed ``partial`` would never journal its shares.
    """
    verdict = _classify(
        detail=_detail_payload(
            _detail_row(cntr_qty="4", ord_remnq="0", cntr_uv="70000", mdfy_cncl="만료")
        )
    )
    assert verdict.verdict == "expired"
    assert verdict.reason == "broker_expiry_confirmed"
    assert verdict.filled_quantity == 4
    assert verdict.average_price == 70000
    assert verdict.requires_manual_review is False


def test_settled_partial_cancel_still_needs_our_own_request() -> None:
    detail = _detail_payload(
        _detail_row(cntr_qty="4", ord_remnq="0", cntr_uv="70000", mdfy_cncl="취소")
    )
    assert _classify(detail=detail, own_cancel_acknowledged=True).verdict == "cancelled"
    unacknowledged = _classify(detail=detail, own_cancel_acknowledged=False)
    assert unacknowledged.verdict == "unknown"
    assert unacknowledged.reason == "cancel_without_own_acknowledged_request"


def test_settled_partial_without_a_marker_is_not_guessed() -> None:
    verdict = _classify(
        detail=_detail_payload(
            _detail_row(cntr_qty="4", ord_remnq="0", cntr_uv="70000")
        )
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "terminal_without_reason_marker"


def test_settled_partial_with_a_ledger_overcount_is_unknown() -> None:
    verdict = _classify(
        ledger=_ledger(recorded_filled_quantity=10),
        detail=_detail_payload(
            _detail_row(cntr_qty="4", ord_remnq="0", cntr_uv="70000", mdfy_cncl="만료")
        ),
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "recorded_fill_contradicts_broker"


def test_fill_with_nan_price_is_not_filled() -> None:
    # Unparseable price must never become a fill verdict.
    verdict = _classify(detail=_detail_payload(_detail_row(cntr_qty="10", cntr_uv="")))
    assert verdict.verdict == "unknown"
    assert verdict.requires_manual_review is True


def test_pending_row_stays_open() -> None:
    verdict = _classify(detail=_detail_payload(_detail_row()))
    assert verdict.verdict == "pending"
    assert verdict.reason == "broker_pending"
    assert verdict.requires_manual_review is False


def test_session_end_alone_never_expires() -> None:
    # No marker, no fill, still-open quantity: the clock is not expiry evidence.
    verdict = _classify(detail=_detail_payload(_detail_row()))
    assert verdict.verdict != "expired"


def test_cancelled_requires_own_acknowledged_request_and_broker_confirmation() -> None:
    detail = _detail_payload(_detail_row(ord_remnq="0", mdfy_cncl="취소"))
    assert _classify(detail=detail, own_cancel_acknowledged=True).verdict == "cancelled"
    assert _classify(detail=detail, own_cancel_acknowledged=False).verdict == "unknown"


def test_cancelled_can_be_confirmed_by_kt00009_cancel_flag() -> None:
    verdict = _classify(
        status=_status_payload(_status_row(mdfy_cncl_tp="취소")),
        detail=_detail_payload(_detail_row(ord_remnq="0")),
        own_cancel_acknowledged=True,
    )
    assert verdict.verdict == "cancelled"
    assert verdict.reason == "own_cancel_acknowledged_and_broker_confirmed"


def test_cancelled_evidence_without_kt00007_detail_row_still_pairs() -> None:
    verdict = _classify(
        status=_status_payload(_status_row(mdfy_cncl_tp="취소")),
        detail=_detail_payload(),
        own_cancel_acknowledged=True,
    )
    assert verdict.verdict == "cancelled"


def test_rejected_needs_a_positive_reject_marker() -> None:
    verdict = _classify(
        detail=_detail_payload(_detail_row(ord_remnq="0", mdfy_cncl="거부"))
    )
    assert verdict.verdict == "rejected"
    assert verdict.reason == "broker_reject_confirmed"


def test_expired_needs_a_positive_expiry_marker() -> None:
    verdict = _classify(
        detail=_detail_payload(_detail_row(ord_remnq="0", mdfy_cncl="만료"))
    )
    assert verdict.verdict == "expired"
    assert verdict.reason == "broker_expiry_confirmed"


def test_terminal_no_fill_without_marker_is_unknown_not_guessed() -> None:
    verdict = _classify(detail=_detail_payload(_detail_row(ord_remnq="0")))
    assert verdict.verdict == "unknown"
    assert verdict.reason == "terminal_without_reason_marker"


def test_ambiguous_marker_text_is_not_evidence() -> None:
    verdict = _classify(
        detail=_detail_payload(_detail_row(ord_remnq="0", mdfy_cncl="취소/만료"))
    )
    assert verdict.verdict == "unknown"


# ---------------------------------------------------------------------------
# I5 — absence is not evidence.


def test_missing_order_row_is_unknown() -> None:
    verdict = _classify(detail=_detail_payload())
    assert verdict.verdict == "unknown"
    assert verdict.reason == "no_exact_row"


def test_incomplete_listing_degrades_every_verdict() -> None:
    cases = [
        (
            evidence_from_payloads(
                order_status=_status_payload(),
                order_detail=_detail_payload(continuation=_continuation("Y")),
            ),
            "order_detail_listing_truncated",
        ),
        (
            evidence_from_payloads(
                order_status=_status_payload(),
                order_detail={"return_code": 0, "acnt_ord_cntr_prps_dtl": []},
            ),
            "order_detail_listing_truncated",
        ),
        (
            evidence_from_payloads(
                order_status=_status_payload(),
                order_detail=_detail_payload(return_code=2),
            ),
            "order_detail_broker_error",
        ),
        (
            evidence_from_payloads(order_status=_status_payload(), order_detail=None),
            "order_detail_unreadable",
        ),
        (
            evidence_from_payloads(order_status=None, order_detail=_detail_payload()),
            "order_status_unreadable",
        ),
    ]
    for evidence, reason in cases:
        verdict = classify_verdict(
            ledger=_ledger(),
            evidence=evidence,
            own_cancel_acknowledged=False,
        )
        assert verdict.verdict == "unknown"
        assert verdict.reason == reason


def test_broker_rows_unparseable_is_unknown() -> None:
    verdict = _classify(detail=_detail_payload(_detail_row(cntr_qty="11")))
    assert verdict.verdict == "unknown"
    assert verdict.reason == "broker_rows_unparseable"


def test_duplicate_order_rows_are_ambiguous() -> None:
    verdict = _classify(detail=_detail_payload(_detail_row(), _detail_row()))
    assert verdict.verdict == "unknown"
    assert verdict.reason == "ambiguous_rows"


def test_order_number_with_another_symbol_is_not_attributed() -> None:
    verdict = _classify(detail=_detail_payload(_detail_row(stk_cd="000660")))
    assert verdict.verdict == "unknown"
    assert verdict.reason == "attribute_mismatch"


def test_ledger_without_broker_order_id_is_unknown() -> None:
    verdict = _classify(ledger=_ledger(status="uncertain", broker_order_id=None))
    assert verdict.verdict == "unknown"
    assert verdict.reason == "no_broker_order_id"


# ---------------------------------------------------------------------------
# uncertain → accepted (NH T9b).


def test_uncertain_promotes_on_exact_positive_listing_match() -> None:
    verdict = _classify(
        ledger=_ledger(status="uncertain"), detail=_detail_payload(_detail_row())
    )
    assert verdict.verdict == "accepted"
    assert verdict.reason == "positive_listing_match"
    assert verdict.broker_order_id == ORDER_NO
    assert verdict.requires_manual_review is False


def test_uncertain_with_terminal_marker_is_not_promoted() -> None:
    # A broker-side cancel/reject cannot be tied to our own cancel request, so
    # writing ``accepted`` would fabricate an acceptance and strand the row.
    detail = _detail_payload(_detail_row(ord_remnq="0", mdfy_cncl="취소"))
    verdict = _classify(ledger=_ledger(status="uncertain"), detail=detail)
    assert verdict.verdict == "unknown"
    assert verdict.reason == "uncertain_broker_terminal_evidence"
    assert verdict.requires_manual_review is True


def test_uncertain_terminal_no_fill_without_marker_is_not_promoted() -> None:
    detail = _detail_payload(_detail_row(ord_remnq="0"))
    verdict = _classify(ledger=_ledger(status="uncertain"), detail=detail)
    assert verdict.verdict == "unknown"
    assert verdict.reason == "uncertain_broker_terminal_evidence"


def test_uncertain_with_a_positive_fill_is_promoted() -> None:
    # A fill proves the order was accepted; the next run classifies filled/partial.
    detail = _detail_payload(_detail_row(cntr_qty="10", ord_remnq="0", cntr_uv="70100"))
    verdict = _classify(ledger=_ledger(status="uncertain"), detail=detail)
    assert verdict.verdict == "accepted"


def test_recorded_fill_blocks_a_no_fill_close() -> None:
    detail = _detail_payload(_detail_row(ord_remnq="0", mdfy_cncl="취소"))
    verdict = _classify(
        ledger=_ledger(status="partial", recorded_filled_quantity=4),
        detail=detail,
        own_cancel_acknowledged=True,
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "recorded_fill_contradicts_broker"


def test_uncertain_without_listing_row_stays_uncertain() -> None:
    verdict = _classify(ledger=_ledger(status="uncertain"), detail=_detail_payload())
    assert verdict.verdict == "unknown"
    assert verdict.reason == "no_exact_row"


def test_uncertain_never_promotes_from_an_incomplete_listing() -> None:
    verdict = _classify(
        ledger=_ledger(status="uncertain"),
        detail=_detail_payload(continuation=_continuation("Y")),
    )
    assert verdict.verdict == "unknown"


def test_status_only_fill_blocks_a_cancel_close() -> None:
    # kt00009 reports four fills; closing as cancelled would strand them.
    verdict = _classify(
        status=_status_payload(_status_row(cntr_qty="4", mdfy_cncl_tp="취소")),
        detail=_detail_payload(),  # no kt00007 row
        own_cancel_acknowledged=True,
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "status_listing_reports_fill"


def test_zero_fill_detail_with_a_status_fill_is_a_conflict() -> None:
    verdict = _classify(
        status=_status_payload(_status_row(cntr_qty="4", mdfy_cncl_tp="취소")),
        detail=_detail_payload(_detail_row(ord_remnq="0", mdfy_cncl="취소")),
        own_cancel_acknowledged=True,
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "fill_evidence_conflict"


def test_differing_fill_quantities_between_listings_are_a_conflict() -> None:
    verdict = _classify(
        status=_status_payload(_status_row(cntr_qty="10")),
        detail=_detail_payload(
            _detail_row(cntr_qty="4", ord_remnq="6", cntr_uv="70050")
        ),
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "fill_evidence_conflict"


def test_status_only_reject_marker_is_positive_evidence() -> None:
    verdict = _classify(
        status=_status_payload(_status_row(mdfy_cncl_tp="거부")),
        detail=_detail_payload(),
    )
    assert verdict.verdict == "rejected"
    assert verdict.reason == "broker_reject_confirmed"


def test_status_only_expiry_marker_is_positive_evidence() -> None:
    verdict = _classify(
        status=_status_payload(_status_row(mdfy_cncl_tp="만료")),
        detail=_detail_payload(),
    )
    assert verdict.verdict == "expired"


def test_status_reject_marker_blocks_uncertain_promotion() -> None:
    verdict = _classify(
        ledger=_ledger(status="uncertain"),
        status=_status_payload(_status_row(mdfy_cncl_tp="거부")),
        detail=_detail_payload(),
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "uncertain_broker_terminal_evidence"


def test_ordered_quantity_mismatch_is_not_the_same_order() -> None:
    # Same number and symbol but a different size: not this ledger row's order.
    detail = _detail_payload(_detail_row(ord_qty="1", ord_remnq="0", mdfy_cncl="만료"))
    verdict = _classify(detail=detail)
    assert verdict.verdict == "unknown"
    assert verdict.reason == "quantity_mismatch"


def test_ordered_quantity_mismatch_blocks_uncertain_promotion() -> None:
    detail = _detail_payload(_detail_row(ord_qty="1", ord_remnq="1"))
    verdict = _classify(ledger=_ledger(status="uncertain"), detail=detail)
    assert verdict.verdict == "unknown"
    assert verdict.reason == "quantity_mismatch"


def test_ambiguous_status_text_is_not_collapsed_into_a_cancel() -> None:
    # normalize_orders would call this "cancelled" (it contains 취소); the
    # classifier must not turn the ambiguity into a terminal write.
    verdict = _classify(
        status=_status_payload(_status_row(mdfy_cncl_tp="취소/만료")),
        detail=_detail_payload(),
        own_cancel_acknowledged=True,
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "ambiguous_marker_text"


def test_status_only_quantity_mismatch_is_not_the_same_order() -> None:
    verdict = _classify(
        status=_status_payload(_status_row(ord_qty="1", mdfy_cncl_tp="만료")),
        detail=_detail_payload(),
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "quantity_mismatch"


def test_uncertain_with_ambiguous_change_text_is_not_promoted() -> None:
    detail = _detail_payload(_detail_row(mdfy_cncl="취소/만료"))
    verdict = _classify(ledger=_ledger(status="uncertain"), detail=detail)
    assert verdict.verdict == "unknown"
    assert verdict.reason == "uncertain_change_type_ambiguous"


def test_uncertain_with_unrecognised_change_text_is_not_promoted() -> None:
    detail = _detail_payload(_detail_row(mdfy_cncl="정정"))
    verdict = _classify(ledger=_ledger(status="uncertain"), detail=detail)
    assert verdict.verdict == "unknown"
    assert verdict.reason == "uncertain_change_type_unclear"


def test_uncertain_without_detail_quantity_proof_is_not_promoted() -> None:
    # A status row alone cannot prove the order is working.
    verdict = _classify(
        ledger=_ledger(status="uncertain"),
        status=_status_payload(_status_row()),
        detail=_detail_payload(),
    )
    assert verdict.verdict == "unknown"
    assert verdict.reason == "no_detail_evidence"


def test_past_trading_day_does_not_consult_todays_status_listing() -> None:
    # The detail listing alone decides the past-day row; kt00009 is not consulted
    # and its emptiness must not become "no such order".
    verdict = _classify(
        status=_status_payload(),
        detail=_detail_payload(
            _detail_row(cntr_qty="10", ord_remnq="0", cntr_uv="70100")
        ),
        consult_status=False,
    )
    assert verdict.verdict == "filled"
