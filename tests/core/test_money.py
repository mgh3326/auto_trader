"""Unit tests for app.core.money quantization helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest

# These imports will FAIL until Task 2 creates the module.
from app.core.money import quantize_crypto_qty, quantize_money, quantize_pct


class TestQuantizeMoney:
    def test_rounds_to_4dp(self):
        assert quantize_money(Decimal("1.23456789")) == Decimal("1.2346")

    def test_round_half_up(self):
        # 1.00005 → rounds up to 1.0001
        assert quantize_money(Decimal("1.00005")) == Decimal("1.0001")

    def test_accepts_float(self):
        result = quantize_money(1.5)
        assert result == Decimal("1.5000")
        assert isinstance(result, Decimal)

    def test_accepts_int(self):
        assert quantize_money(100) == Decimal("100.0000")

    def test_zero(self):
        assert quantize_money(0) == Decimal("0.0000")

    def test_large_value(self):
        assert quantize_money(Decimal("100000000.1234")) == Decimal("100000000.1234")

    def test_negative(self):
        assert quantize_money(Decimal("-5.12345")) == Decimal("-5.1235")


class TestQuantizeCryptoQty:
    def test_rounds_to_8dp(self):
        assert quantize_crypto_qty(Decimal("0.123456789")) == Decimal("0.12345679")

    def test_round_half_up(self):
        # 0.000000005 → rounds up to 0.00000001
        assert quantize_crypto_qty(Decimal("0.000000005")) == Decimal("0.00000001")

    def test_accepts_float(self):
        result = quantize_crypto_qty(0.001)
        assert isinstance(result, Decimal)
        assert result == Decimal("0.00100000")

    def test_zero(self):
        assert quantize_crypto_qty(0) == Decimal("0.00000000")

    def test_large_btc_qty(self):
        assert quantize_crypto_qty(Decimal("0.00100000")) == Decimal("0.00100000")


class TestQuantizePct:
    def test_rounds_to_2dp(self):
        assert quantize_pct(Decimal("12.345")) == Decimal("12.35")

    def test_round_half_up(self):
        # 0.005 → rounds up to 0.01
        assert quantize_pct(Decimal("0.005")) == Decimal("0.01")

    def test_accepts_float(self):
        result = quantize_pct(3.14159)
        assert isinstance(result, Decimal)
        assert result == Decimal("3.14")

    def test_negative(self):
        assert quantize_pct(Decimal("-1.235")) == Decimal("-1.24")

    def test_zero(self):
        assert quantize_pct(0) == Decimal("0.00")
