"""hk 5441 — ``app.core.upbit_tick`` against the shared boundary fixture.

fillwire's Go table is checked against the same file
(``tests/fixtures/upbit_krw_tick/boundaries.json``), so a change here that the
fixture does not carry is a change the executor would not see.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.upbit_tick import (
    UPBIT_KRW_TICK_TABLE,
    is_on_upbit_krw_tick,
    upbit_krw_tick,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "upbit_krw_tick"
    / "boundaries.json"
)
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.unit
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["price"])
def test_fixture_case(case: dict[str, object]) -> None:
    price = Decimal(str(case["price"]))
    assert upbit_krw_tick(price) == Decimal(str(case["tick"]))
    assert is_on_upbit_krw_tick(price) is case["on_tick"]


@pytest.mark.unit
def test_fixture_covers_both_sides_of_every_boundary() -> None:
    prices = {Decimal(str(case["price"])) for case in CASES}
    for lower_bound, _tick in UPBIT_KRW_TICK_TABLE:
        assert lower_bound in prices
        assert any(price < lower_bound for price in prices)


@pytest.mark.unit
def test_table_is_ordered_from_the_highest_range_down() -> None:
    bounds = [lower_bound for lower_bound, _tick in UPBIT_KRW_TICK_TABLE]
    assert bounds == sorted(bounds, reverse=True)
    assert len(set(bounds)) == len(bounds)


@pytest.mark.unit
def test_differs_from_the_legacy_adjuster_between_1000_and_5000() -> None:
    # The legacy function snaps this band to 5 KRW; the published unit is 1 KRW.
    assert upbit_krw_tick(Decimal("3001")) == Decimal("1")
    assert is_on_upbit_krw_tick(Decimal("3001"))


@pytest.mark.unit
@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_non_positive_or_non_finite_price_is_refused(bad: Decimal) -> None:
    with pytest.raises(ValueError):
        upbit_krw_tick(bad)


@pytest.mark.unit
def test_float_price_is_refused() -> None:
    with pytest.raises(TypeError):
        upbit_krw_tick(3001.0)  # type: ignore[arg-type]
