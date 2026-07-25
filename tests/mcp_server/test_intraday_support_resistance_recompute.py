"""ROB-541 SLICE C: intraday S/R distance re-sign against the live quote price.

The EOD support_resistance payload computes distance_pct and the support vs
resistance split against the daily-close ``current_price``. On an intraday gap,
that misclassifies levels relative to where the symbol is actually trading. The
analyze-stock assembly recomputes distance_pct AND re-splits supports vs
resistances against the LIVE quote.price for KR/crypto, leaving the EOD price
LEVELS intact and never touching the shared ``_support_resistance.py`` module.
"""

from __future__ import annotations

import pytest

from app.mcp_server.tooling.analysis_analyze import (
    _recompute_intraday_support_resistance,
)


def _sr_payload() -> dict:
    # EOD close = 100. Levels split against EOD close:
    #   95  -> support     (below 100)
    #   105 -> resistance   (above 100)
    #   120 -> resistance   (above 100)
    return {
        "support_resistance": {
            "symbol": "005930",
            "current_price": 100.0,
            "supports": [{"price": 95.0, "strength": "moderate", "distance_pct": -5.0}],
            "resistances": [
                {"price": 105.0, "strength": "weak", "distance_pct": 5.0},
                {"price": 120.0, "strength": "strong", "distance_pct": 20.0},
            ],
        }
    }


class TestRecomputeIntradaySupportResistance:
    def test_kr_gap_up_resplits_and_resigns_distance(self) -> None:
        analysis = _sr_payload()
        # Live KR quote gapped up to 110 (above EOD close 100).
        analysis["quote"] = {"price": 110.0}

        _recompute_intraday_support_resistance(analysis, "equity_kr")

        sr = analysis["support_resistance"]
        # 95 stays a support (below live 110); 105 FLIPS from resistance to
        # support (now below live 110). 120 stays a resistance.
        support_prices = {level["price"] for level in sr["supports"]}
        resistance_prices = {level["price"] for level in sr["resistances"]}
        assert support_prices == {95.0, 105.0}
        assert resistance_prices == {120.0}

        # distance_pct uses the LIVE price (110), not the EOD close (100).
        by_price = {
            level["price"]: level["distance_pct"]
            for level in [*sr["supports"], *sr["resistances"]]
        }
        assert by_price[95.0] == pytest.approx((95.0 - 110.0) / 110.0 * 100, abs=0.01)
        assert by_price[105.0] == pytest.approx((105.0 - 110.0) / 110.0 * 100, abs=0.01)
        assert by_price[120.0] == pytest.approx((120.0 - 110.0) / 110.0 * 100, abs=0.01)

        # EOD price levels are preserved; basis annotated as live.
        assert {95.0, 105.0, 120.0} == support_prices | resistance_prices
        assert sr["distance_basis"] == "live_quote"
        assert sr["distance_basis_price"] == pytest.approx(110.0)

    def test_below_eod_close_but_below_live_price_stays_support(self) -> None:
        analysis = _sr_payload()
        analysis["quote"] = {"price": 110.0}

        _recompute_intraday_support_resistance(analysis, "equity_kr")

        sr = analysis["support_resistance"]
        # The 95 level (below EOD close AND below live price) stays a support and
        # its distance is computed against the live price.
        support_95 = next(s for s in sr["supports"] if s["price"] == 95.0)
        assert support_95["distance_pct"] == pytest.approx(
            (95.0 - 110.0) / 110.0 * 100, abs=0.01
        )

    def test_crypto_recomputes(self) -> None:
        analysis = _sr_payload()
        analysis["support_resistance"]["symbol"] = "KRW-BTC"
        analysis["quote"] = {"price": 110.0}

        _recompute_intraday_support_resistance(analysis, "crypto")

        sr = analysis["support_resistance"]
        assert {s["price"] for s in sr["supports"]} == {95.0, 105.0}
        assert sr["distance_basis"] == "live_quote"

    def test_us_market_left_untouched(self) -> None:
        analysis = _sr_payload()
        analysis["quote"] = {"price": 110.0}

        _recompute_intraday_support_resistance(analysis, "equity_us")

        sr = analysis["support_resistance"]
        # US is not in scope (it carries its own live-price source upstream).
        assert "distance_basis" not in sr
        assert {s["price"] for s in sr["supports"]} == {95.0}

    def test_missing_live_price_is_noop(self) -> None:
        analysis = _sr_payload()
        analysis["quote"] = {"price": None}

        _recompute_intraday_support_resistance(analysis, "equity_kr")

        sr = analysis["support_resistance"]
        assert "distance_basis" not in sr
        assert {s["price"] for s in sr["supports"]} == {95.0}

    def test_error_payload_is_noop(self) -> None:
        analysis = {
            "support_resistance": {"error": "boom"},
            "quote": {"price": 110.0},
        }

        _recompute_intraday_support_resistance(analysis, "equity_kr")

        assert analysis["support_resistance"] == {"error": "boom"}
