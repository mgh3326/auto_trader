"""ROB-302 — Futures Demo smoke symbol-filter selection + quantity precision.

Pure-function coverage (no HTTP) for the two ``--order-test`` / ``--confirm``
sizing bugs:

  * exchangeInfo can return many symbols and the ``symbol=`` query param is not
    honored on demo-fapi; selecting ``symbols[0]`` applied BTCUSDT filters to
    XRPUSDT. Fix: match the requested symbol row, fail closed if absent.
  * Even with the right MARKET_LOT_SIZE step, ``format(qty, "f")`` emitted the
    step string's trailing zeros (``"0.10000000"`` -> ``"30.00000000"``) and
    Binance returned ``-1111 Precision is over the maximum``. Fix: quantize the
    submitted quantity to the symbol's quantityPrecision (Codex review #6).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import scripts.binance_futures_demo_smoke as smoke


def _exchange_info_multi() -> dict:
    """BTCUSDT first, XRPUSDT later — mirrors demo-fapi's unfiltered response."""
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "quantityPrecision": 3,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.00100000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00100000"},
                    {"filterType": "MIN_NOTIONAL", "notional": "100"},
                ],
            },
            {
                "symbol": "XRPUSDT",
                "quantityPrecision": 1,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.00100000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.10000000"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            },
        ]
    }


def test_parse_selects_requested_symbol_not_index_zero():
    filters = smoke._parse_symbol_filters(_exchange_info_multi(), "XRPUSDT")
    # XRPUSDT MARKET step 0.1, not BTCUSDT's 0.001; XRP min_notional 5, not 100.
    assert filters["step_size"] == Decimal("0.10000000")
    assert filters["min_notional"] == Decimal("5")
    assert filters["quantity_precision"] == 1


def test_parse_prefers_market_lot_size_over_lot_size():
    filters = smoke._parse_symbol_filters(_exchange_info_multi(), "XRPUSDT")
    # MARKET_LOT_SIZE (0.1) wins over LOT_SIZE (0.001) for MARKET orders.
    assert filters["step_size"] == Decimal("0.10000000")


def test_parse_falls_back_to_lot_size_when_no_market_lot_size():
    body = {
        "symbols": [
            {
                "symbol": "XRPUSDT",
                "quantityPrecision": 1,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.10000000"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
        ]
    }
    filters = smoke._parse_symbol_filters(body, "XRPUSDT")
    assert filters["step_size"] == Decimal("0.10000000")


def test_parse_missing_symbol_fails_closed():
    body = {"symbols": [{"symbol": "BTCUSDT", "filters": []}]}
    with pytest.raises(RuntimeError):
        smoke._parse_symbol_filters(body, "XRPUSDT")


def test_parse_empty_symbols_fails_closed():
    with pytest.raises(RuntimeError):
        smoke._parse_symbol_filters({"symbols": []}, "XRPUSDT")


def test_quantize_strips_step_trailing_zeros():
    """Codex #6: floored-to-step Decimal must serialize without trailing zeros."""
    step = Decimal("0.10000000")
    floored = Decimal("30.00000000")  # what step-floor produces
    qty = smoke._quantize_qty(floored, step_size=step, quantity_precision=1)
    # format(qty, "f") is how execution_client serializes the outbound quantity.
    assert format(qty, "f") == "30.0"
    assert format(qty, "f") != "30.00000000"


def test_quantize_precision_zero_yields_integer_string():
    qty = smoke._quantize_qty(
        Decimal("30.00000000"), step_size=Decimal("1"), quantity_precision=0
    )
    assert format(qty, "f") == "30"


def test_quantize_does_not_round_up():
    qty = smoke._quantize_qty(
        Decimal("30.19"), step_size=Decimal("0.1"), quantity_precision=1
    )
    assert qty == Decimal("30.1")


def test_quantize_without_precision_uses_normalized_step_exponent():
    qty = smoke._quantize_qty(
        Decimal("30.00000000"), step_size=Decimal("0.10000000"), quantity_precision=None
    )
    assert format(qty, "f") == "30.0"


def test_quantize_close_qty_from_position_amt_strips_trailing_zeros():
    """Codex: reduceOnly close uses abs(positionAmt) which can carry a fixed
    scale; it must be quantized so the close leg does not re-trigger -1111."""
    position_amt = Decimal("-30.00000000")  # short position, fixed-scale
    close_qty = smoke._quantize_qty(
        abs(position_amt), step_size=Decimal("0.1"), quantity_precision=1
    )
    assert format(close_qty, "f") == "30.0"


def test_parse_returns_lot_step_size_for_limit():
    filters = smoke._parse_symbol_filters(_exchange_info_multi(), "XRPUSDT")
    # LIMIT orders must use LOT_SIZE (0.001), not MARKET_LOT_SIZE (0.1).
    assert filters["lot_step_size"] == Decimal("0.00100000")
    assert filters["step_size"] == Decimal("0.10000000")


def test_step_for_order_type_selects_correct_step():
    filters = smoke._parse_symbol_filters(_exchange_info_multi(), "XRPUSDT")
    assert smoke._step_for_order_type(filters, "MARKET") == Decimal("0.10000000")
    assert smoke._step_for_order_type(filters, "LIMIT") == Decimal("0.00100000")


def test_lot_step_falls_back_to_market_when_lot_absent():
    body = {
        "symbols": [
            {
                "symbol": "XRPUSDT",
                "quantityPrecision": 1,
                "filters": [
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.10000000"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
        ]
    }
    filters = smoke._parse_symbol_filters(body, "XRPUSDT")
    assert filters["lot_step_size"] == Decimal("0.10000000")
