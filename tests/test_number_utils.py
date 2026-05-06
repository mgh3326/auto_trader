"""Unit tests for app.core.number_utils."""

from __future__ import annotations

import pytest

from app.core.number_utils import parse_korean_number


class TestParseKoreanNumber:
    """Tests for parse_korean_number."""

    def test_simple_integer(self) -> None:
        assert parse_korean_number("1234") == 1234
        assert parse_korean_number("1,234") == 1234
        assert parse_korean_number("1,234,567") == 1234567

    def test_simple_float(self) -> None:
        assert parse_korean_number("12.34") == 12.34
        assert parse_korean_number("1,234.56") == 1234.56

    def test_percentage(self) -> None:
        result = parse_korean_number("5.67%")
        assert result is not None
        assert result == pytest.approx(0.0567, abs=0.0001)

        result = parse_korean_number("100%")
        assert result is not None
        assert result == pytest.approx(1.0, abs=0.0001)

    def test_korean_unit_jo(self) -> None:
        assert parse_korean_number("1조") == 1_0000_0000_0000
        assert parse_korean_number("2.5조") == 2_5000_0000_0000

    def test_korean_unit_eok(self) -> None:
        assert parse_korean_number("1억") == 1_0000_0000
        assert parse_korean_number("100억") == 100_0000_0000

    def test_korean_unit_man(self) -> None:
        assert parse_korean_number("1만") == 1_0000
        assert parse_korean_number("5만") == 5_0000

    def test_korean_units_combined(self) -> None:
        result = parse_korean_number("1조 2,345억")
        expected = 1_0000_0000_0000 + 2345 * 1_0000_0000
        assert result == expected

        result = parse_korean_number("400조 1,234억")
        expected = 400 * 1_0000_0000_0000 + 1234 * 1_0000_0000
        assert result == expected

    def test_negative_number_with_minus(self) -> None:
        assert parse_korean_number("-1,234") == -1234
        assert parse_korean_number("-5.67") == -5.67

    def test_negative_number_with_arrow(self) -> None:
        assert parse_korean_number("▼1,234") == -1234
        assert parse_korean_number("▼100") == -100

    def test_positive_number_with_arrow(self) -> None:
        assert parse_korean_number("▲1,234") == 1234

    def test_none_for_invalid(self) -> None:
        assert parse_korean_number("") is None
        assert parse_korean_number(None) is None
        assert parse_korean_number("N/A") is None
        assert parse_korean_number("--") is None

    def test_with_whitespace(self) -> None:
        assert parse_korean_number("  1,234  ") == 1234
        assert parse_korean_number("1 억") == 1_0000_0000

    # krx.py 버전에만 있던 엣지 케이스: 단일 "-"
    def test_single_dash_returns_none(self) -> None:
        assert parse_korean_number("-") is None
