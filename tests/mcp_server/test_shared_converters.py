"""Unit tests for NaN-safe private converters in shared.py."""

from __future__ import annotations

from app.mcp_server.tooling.shared import _to_optional_float, _to_optional_int


class TestToOptionalFloat:
    def test_none_returns_none(self):
        assert _to_optional_float(None) is None

    def test_empty_string_returns_none(self):
        assert _to_optional_float("") is None

    def test_nan_string_returns_none(self):
        assert _to_optional_float("nan") is None

    def test_nan_float_returns_none(self):
        assert _to_optional_float(float("nan")) is None

    def test_valid_string_float(self):
        assert _to_optional_float("3.14") == 3.14

    def test_int_converts_to_float(self):
        assert _to_optional_float(42) == 42.0

    def test_invalid_string_returns_none(self):
        assert _to_optional_float("not_a_number") is None


class TestToOptionalInt:
    def test_none_returns_none(self):
        assert _to_optional_int(None) is None

    def test_empty_string_returns_none(self):
        assert _to_optional_int("") is None

    def test_nan_string_returns_none(self):
        assert _to_optional_int("nan") is None

    def test_nan_float_returns_none(self):
        assert _to_optional_int(float("nan")) is None

    def test_valid_string_int(self):
        assert _to_optional_int("3") == 3

    def test_float_truncates_to_int(self):
        assert _to_optional_int(3.7) == 3

    def test_invalid_string_returns_none(self):
        assert _to_optional_int("bad") is None
