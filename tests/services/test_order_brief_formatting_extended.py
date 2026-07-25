"""Tests for extended formatter helpers moved from n8n_daily_brief_service."""

from __future__ import annotations

from app.services.order_brief_formatting import (
    _fmt_days,
    _fmt_krw,
    _fmt_pct,
    _format_g2_lines,
)


class TestFmtKrw:
    def test_none_returns_dash(self) -> None:
        assert _fmt_krw(None) == "-"

    def test_zero(self) -> None:
        assert _fmt_krw(0.0) == "0 KRW"

    def test_positive_float(self) -> None:
        # f"{1234.5:,.0f}" uses banker's rounding → 1,234
        assert _fmt_krw(1234.5) == "1,234 KRW"

    def test_integer_input(self) -> None:
        assert _fmt_krw(1000) == "1,000 KRW"


class TestFmtPct:
    def test_none_returns_dash(self) -> None:
        assert _fmt_pct(None) == "-"

    def test_decimal(self) -> None:
        assert _fmt_pct(1.567) == "1.6%"

    def test_zero(self) -> None:
        assert _fmt_pct(0.0) == "0.0%"

    def test_integer_input(self) -> None:
        assert _fmt_pct(5) == "5.0%"


class TestFmtDays:
    def test_none_returns_dash(self) -> None:
        assert _fmt_days(None) == "-"

    def test_float(self) -> None:
        assert _fmt_days(3.5) == "3.50일"

    def test_integer_input(self) -> None:
        assert _fmt_days(7) == "7.00일"


class TestFormatG2Lines:
    def test_template_substitution(self) -> None:
        lines = ["{amount}원 / {days}일"]
        result = _format_g2_lines(lines, amount=10000, days=7)
        assert result == ["10,000원 / 7일"]

    def test_multiple_lines(self) -> None:
        lines = ["{amount}원", "{days}일 남음"]
        result = _format_g2_lines(lines, amount=5000, days=3)
        assert result == ["5,000원", "3일 남음"]

    def test_empty_list(self) -> None:
        assert _format_g2_lines([], amount=1000, days=1) == []

    def test_line_without_placeholders(self) -> None:
        lines = ["고정 텍스트"]
        result = _format_g2_lines(lines, amount=1000, days=1)
        assert result == ["고정 텍스트"]
