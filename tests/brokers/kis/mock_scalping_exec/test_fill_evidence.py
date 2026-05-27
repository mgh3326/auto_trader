"""ROB-334 — pure fill-evidence classifier tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    EvidenceCategory,
    FillVerdict,
    classify_fill_evidence,
)


def _row(**kw):
    base = {"odno": "0000123456", "pdno": "005930", "ord_qty": "1"}
    base.update(kw)
    return base


@pytest.mark.unit
def test_fully_filled_uses_avg_price() -> None:
    rows = [_row(tot_ccld_qty="1", avg_prvs="70000")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.FILLED
    assert ev.filled_qty == Decimal("1")
    assert ev.avg_price == Decimal("70000")
    assert ev.category is None


@pytest.mark.unit
def test_filled_falls_back_to_amount_over_qty() -> None:
    rows = [_row(ord_qty="2", tot_ccld_qty="2", tot_ccld_amt="140600")]
    ev = classify_fill_evidence(order_no="0000123456", rows=rows)
    assert ev.verdict is FillVerdict.FILLED
    assert ev.avg_price == Decimal("70300")


@pytest.mark.unit
def test_zero_filled_is_pending() -> None:
    rows = [_row(tot_ccld_qty="0")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.PENDING
    assert ev.category is None


@pytest.mark.unit
def test_partial_fill_is_partial_not_filled() -> None:
    rows = [_row(ord_qty="3", tot_ccld_qty="1", avg_prvs="70000")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.PARTIAL


@pytest.mark.unit
def test_no_matching_row_is_none_data_precondition() -> None:
    rows = [_row(odno="999", tot_ccld_qty="1", avg_prvs="70000")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.NONE
    assert ev.category is EvidenceCategory.DATA_PRECONDITION


@pytest.mark.unit
def test_filled_without_price_is_none_code() -> None:
    rows = [_row(tot_ccld_qty="1")]  # filled but no price/amount
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.NONE
    assert ev.category is EvidenceCategory.CODE


@pytest.mark.unit
def test_unparseable_qty_is_none_code() -> None:
    rows = [_row(tot_ccld_qty="abc", avg_prvs="70000")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.NONE
    assert ev.category is EvidenceCategory.CODE


@pytest.mark.unit
def test_leading_zero_order_no_matches() -> None:
    rows = [_row(odno="0000123456", tot_ccld_qty="1", avg_prvs="70000")]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.FILLED


@pytest.mark.unit
def test_split_fill_rows_aggregate() -> None:
    rows = [
        _row(odno="123456", ord_qty="2", tot_ccld_qty="1", tot_ccld_amt="70000"),
        _row(odno="123456", ord_qty="2", tot_ccld_qty="1", tot_ccld_amt="70200"),
    ]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.FILLED
    assert ev.filled_qty == Decimal("2")
    assert ev.avg_price == Decimal("70100")  # 140200 / 2


@pytest.mark.unit
def test_uppercase_keys_resolved() -> None:
    rows = [
        {"ODNO": "123456", "ORD_QTY": "1", "TOT_CCLD_QTY": "1", "AVG_PRVS": "70000"}
    ]
    ev = classify_fill_evidence(order_no="123456", rows=rows)
    assert ev.verdict is FillVerdict.FILLED
