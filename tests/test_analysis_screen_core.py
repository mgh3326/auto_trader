from __future__ import annotations

import pytest

from app.mcp_server.tooling.analysis_screen_core import (
    _clean_text,
    _to_optional_float,
    _to_optional_int,
)

pytestmark = pytest.mark.unit


def test_to_optional_float_treats_nan_values_as_missing() -> None:
    assert _to_optional_float(None) is None
    assert _to_optional_float(float("nan")) is None
    assert _to_optional_float("nan") is None


def test_to_optional_float_preserves_normal_numbers() -> None:
    assert _to_optional_float(12) == 12.0
    assert _to_optional_float("12.5") == 12.5


def test_to_optional_int_treats_nan_values_as_missing() -> None:
    assert _to_optional_int(None) is None
    assert _to_optional_int(float("nan")) is None
    assert _to_optional_int("nan") is None


def test_to_optional_int_preserves_normal_numbers() -> None:
    assert _to_optional_int(12) == 12
    assert _to_optional_int("12") == 12


def test_clean_text_normalizes_nan_and_whitespace() -> None:
    assert _clean_text(None) == ""
    assert _clean_text(float("nan")) == ""
    assert _clean_text("nan") == ""
    assert _clean_text("  hello  ") == "hello"
    assert _clean_text(123) == "123"
