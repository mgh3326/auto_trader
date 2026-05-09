"""Unit tests for app.core.normalizers.to_float."""

from __future__ import annotations

import pytest

# This import will FAIL until Task 2 creates the module.
from app.core.normalizers import to_float


class TestToFloat:
    def test_none_returns_default(self):
        assert to_float(None) == 0.0

    def test_empty_string_returns_default(self):
        assert to_float("") == 0.0

    def test_custom_default_on_none(self):
        assert to_float(None, default=-1.0) == -1.0

    def test_custom_default_on_empty_string(self):
        assert to_float("", default=99.0) == 99.0

    def test_int_value(self):
        assert to_float(42) == 42.0

    def test_float_value(self):
        assert to_float(3.14) == pytest.approx(3.14)

    def test_string_float(self):
        assert to_float("1234.56") == pytest.approx(1234.56)

    def test_decimal_string(self):
        assert to_float("0.00100000") == pytest.approx(0.001)

    def test_bad_string_returns_default(self):
        assert to_float("bad") == 0.0

    def test_bad_string_custom_default(self):
        assert to_float("n/a", default=-99.0) == -99.0

    def test_zero_string(self):
        assert to_float("0") == 0.0

    def test_negative_string(self):
        assert to_float("-5.5") == pytest.approx(-5.5)

    def test_default_is_keyword_only(self):
        # Calling with positional second arg must raise TypeError
        with pytest.raises(TypeError):
            to_float("1.0", 99.0)  # type: ignore[call-arg]

    def test_list_returns_default(self):
        # Unconvertible type — returns default
        assert to_float([1, 2], default=-1.0) == -1.0
