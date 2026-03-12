from __future__ import annotations

import pytest

from app.mcp_server.tooling.fundamentals_sources_naver import _coerce_optional_number

pytestmark = pytest.mark.unit


def test_coerce_optional_number_treats_missing_and_nan_values_as_none() -> None:
    assert _coerce_optional_number(None) is None
    assert _coerce_optional_number(True) is None
    assert _coerce_optional_number(float("nan")) is None
    assert _coerce_optional_number("123") is None


def test_coerce_optional_number_preserves_real_numbers() -> None:
    assert _coerce_optional_number(12) == 12
    assert _coerce_optional_number(12.5) == 12.5
