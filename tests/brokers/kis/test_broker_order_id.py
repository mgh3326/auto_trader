"""ROB-843 — broker order-id normalization (whitespace/malformed fail-close)."""

from __future__ import annotations

import pytest

from app.services.brokers.kis.order_id import normalize_broker_order_id


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "\t\n",
        {"odno": "1"},  # malformed type
        ["0001"],  # malformed type
        3.14,  # malformed type
        True,  # bool is not a valid order id
    ],
)
def test_invalid_ids_normalize_to_none(value) -> None:
    assert normalize_broker_order_id(value) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,expected",
    [
        ("0001234567", "0001234567"),  # domestic
        ("0009999999", "0009999999"),  # overseas
        ("  0001234567  ", "0001234567"),  # leading/trailing whitespace stripped
        ("\t0001\n", "0001"),
        (123456, "123456"),  # int coerced
    ],
)
def test_valid_ids_are_stripped_and_kept(value, expected) -> None:
    assert normalize_broker_order_id(value) == expected
