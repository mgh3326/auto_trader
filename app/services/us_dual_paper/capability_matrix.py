"""Declarative, secret-free capability matrix for US dual-paper preview (ROB-326).

Keyed by canonical account_scope tokens (kis_mock, alpaca_paper). market is "us".
This issue supports long/buy + limit only; submit is confirm-only/default-disabled.
"""

from __future__ import annotations

from typing import Any

SUPPORTED_ACCOUNT_SCOPES: tuple[str, ...] = ("kis_mock", "alpaca_paper")


def get_capability_matrix() -> dict[str, dict[str, Any]]:
    common: dict[str, Any] = {
        "market": "us",
        "asset_class": "us_equity",
        "supported_sides": ["buy"],
        "supported_order_types": ["limit"],
        "preview_supported": True,
        "submit_gate": "confirm_only_default_disabled",
        "positions_read": True,
    }
    return {
        "kis_mock": {
            **common,
            "broker": "kis",
            "account_mode": "kis_mock",
            # ROB-951: VTTS3007R was verified on the mock host on 2026-07-17.
            # Its ord_psbl_frcr_amt is USD orderable cash and exrt supplies the
            # exchange rate in the same broker response. This does not imply
            # mock overseas pending-order support (TTTS3018R remains blocked).
            "account_cash_read": True,
            "open_orders_read": False,
            "market_session_note": (
                "Mock overseas holdings and VTTS3007R USD buying power read on the "
                "mock host; overseas pending orders remain unavailable; quote freshness "
                "is operator-supplied until a US quote adapter lands."
            ),
            "known_unknown_fields": [
                "open_orders",
                "live_quote_state",
            ],
        },
        "alpaca_paper": {
            **common,
            "broker": "alpaca",
            "account_mode": "alpaca_paper",
            "account_cash_read": True,
            "open_orders_read": True,
            "market_session_note": (
                "Paper account/positions readable anytime; limit preview is "
                "qty + limit_price (notional not allowed for equity limit)."
            ),
            "known_unknown_fields": [],
        },
    }
