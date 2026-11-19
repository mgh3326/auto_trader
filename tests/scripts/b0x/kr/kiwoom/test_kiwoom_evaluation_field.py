"""hk 5419 — the b0x KR lane reads only the spec's kt00018 evaluation field.

kt00018 rows carry ``evlt_amt`` (평가금액). ``evltv_amt`` and ``evlu_amt`` are
not in the official response table, so a row that only has them reports no
evaluation (the existing narrowing ``0``), never a value read from them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.b0x.kr import kiwoom as kiwoom_lane

pytestmark = pytest.mark.unit


def test_reads_the_spec_field_evlt_amt() -> None:
    row = {"stk_cd": "A005930", "evlt_amt": "000000000070000"}
    assert kiwoom_lane._echoed_evaluation(row) == Decimal("70000")


@pytest.mark.parametrize("not_in_spec", ["evltv_amt", "evlu_amt"])
def test_ignores_fields_the_spec_does_not_define(not_in_spec: str) -> None:
    row = {"stk_cd": "A005930", not_in_spec: "70000"}
    assert kiwoom_lane._echoed_evaluation(row) == Decimal("0")


def test_evaluation_field_tuple_is_exactly_the_spec_field() -> None:
    assert kiwoom_lane._EVALUATION_FIELDS == ("evlt_amt",)
