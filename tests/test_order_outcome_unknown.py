"""ROB-645: a timed-out / network-failed order POST has an UNKNOWN outcome.

Since we no longer retry order sends (retry = double-submit), the tool response
must surface an explicit, non-blank error telling the operator to reconcile
rather than re-send.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.unit
class TestReconcileToolFor:
    def test_kr_live(self):
        from app.mcp_server.tooling.order_execution import _reconcile_tool_for

        assert (
            _reconcile_tool_for(market_type="equity_kr", is_mock=False)
            == "kis_live_reconcile_orders"
        )

    def test_us_live(self):
        from app.mcp_server.tooling.order_execution import _reconcile_tool_for

        assert (
            _reconcile_tool_for(market_type="equity_us", is_mock=False)
            == "live_reconcile_orders"
        )

    def test_crypto_live(self):
        from app.mcp_server.tooling.order_execution import _reconcile_tool_for

        assert (
            _reconcile_tool_for(market_type="crypto", is_mock=False)
            == "live_reconcile_orders"
        )

    def test_mock_has_no_reconcile_tool(self):
        from app.mcp_server.tooling.order_execution import _reconcile_tool_for

        # No kis_mock_reconcile_orders tool exists — must not name a phantom tool.
        assert _reconcile_tool_for(market_type="equity_kr", is_mock=True) is None


@pytest.mark.unit
class TestAugmentErrorForUnknownOutcome:
    def _base(self):
        return {
            "success": False,
            "error": "ReadTimeout",
            "source": "kis",
            "symbol": "005930",
            "instrument_type": "equity_kr",
        }

    def test_timeout_flags_outcome_unknown_and_names_reconcile_tool(self):
        from app.mcp_server.tooling.order_execution import (
            OrderSendOutcomeUnknown,
            _augment_error_for_unknown_outcome,
        )

        result = _augment_error_for_unknown_outcome(
            self._base(),
            OrderSendOutcomeUnknown(httpx.ReadTimeout("")),
            market_type="equity_kr",
            is_mock=False,
        )

        assert result["success"] is False
        assert result["outcome_unknown"] is True
        assert result["reconcile_tool"] == "kis_live_reconcile_orders"
        # Non-blank, actionable reason mentioning the reconcile tool + uncertainty.
        assert result["error"].strip()
        assert "kis_live_reconcile_orders" in result["error"]
        assert "불확실" in result["error"]
        # The concrete transport reason is preserved (ROB-600: never blank).
        assert "ReadTimeout" in result["error"]

    def test_us_timeout_points_at_live_reconcile(self):
        from app.mcp_server.tooling.order_execution import (
            OrderSendOutcomeUnknown,
            _augment_error_for_unknown_outcome,
        )

        result = _augment_error_for_unknown_outcome(
            self._base(),
            OrderSendOutcomeUnknown(httpx.ConnectTimeout("")),
            market_type="equity_us",
            is_mock=False,
        )
        assert result["reconcile_tool"] == "live_reconcile_orders"
        assert "live_reconcile_orders" in result["error"]

    def test_mock_timeout_has_no_phantom_tool(self):
        from app.mcp_server.tooling.order_execution import (
            OrderSendOutcomeUnknown,
            _augment_error_for_unknown_outcome,
        )

        result = _augment_error_for_unknown_outcome(
            self._base(),
            OrderSendOutcomeUnknown(httpx.ReadTimeout("")),
            market_type="equity_kr",
            is_mock=True,
        )
        assert result["outcome_unknown"] is True
        assert result["reconcile_tool"] is None
        assert "불확실" in result["error"]

    def test_non_request_error_is_left_unchanged(self):
        from app.mcp_server.tooling.order_execution import (
            _augment_error_for_unknown_outcome,
        )

        base = self._base()
        result = _augment_error_for_unknown_outcome(
            base,
            RuntimeError("EGW00215 초당 거래건수 초과"),
            market_type="equity_kr",
            is_mock=False,
        )
        # A definitive broker rejection is NOT outcome-unknown.
        assert "outcome_unknown" not in result
        assert result == base

    def test_raw_presend_request_error_is_left_unchanged(self):
        from app.mcp_server.tooling.order_execution import (
            _augment_error_for_unknown_outcome,
        )

        base = self._base()
        # A bare RequestError (e.g. price fetch before the order is sent) means the
        # order was NEVER submitted — must not be flagged outcome-unknown.
        result = _augment_error_for_unknown_outcome(
            base,
            httpx.ReadTimeout(""),
            market_type="equity_kr",
            is_mock=False,
        )
        assert "outcome_unknown" not in result
        assert result == base
