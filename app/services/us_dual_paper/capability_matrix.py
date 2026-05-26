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
        "account_cash_read": True,
        "positions_read": True,
    }
    return {
        "kis_mock": {
            **common,
            "broker": "kis",
            "account_mode": "kis_mock",
            "open_orders_read": "partial",  # mock open-order reader may be unavailable
            "market_session_note": (
                "Mock overseas reads work pre/post regular session; quote freshness "
                "is operator-supplied until a US quote adapter lands."
            ),
            "known_unknown_fields": ["live_quote_state"],
        },
        "alpaca_paper": {
            **common,
            "broker": "alpaca",
            "account_mode": "alpaca_paper",
            "open_orders_read": True,
            "market_session_note": (
                "Paper account/positions readable anytime; limit preview is "
                "qty + limit_price (notional not allowed for equity limit)."
            ),
            "known_unknown_fields": [],
        },
    }
