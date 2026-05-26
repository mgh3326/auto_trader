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
            # Verified by live smoke (2026-05-27): KIS 모의투자 offers no overseas
            # foreign-margin service (OPSQ0002 "없는 서비스 코드"), so USD cash /
            # buying-power cannot be read; the overseas pending-orders inquiry
            # (TTTS3018R) is hard-blocked in mock. Overseas holdings/positions DO
            # read on the mock host (openapivts) via VTTS3012R.
            "account_cash_read": False,
            "open_orders_read": False,
            "market_session_note": (
                "Mock overseas holdings read pre/post regular session on the mock "
                "host; USD cash/buying-power unsupported (OPSQ0002); quote freshness "
                "is operator-supplied until a US quote adapter lands."
            ),
            "known_unknown_fields": [
                "cash_usd",
                "buying_power_usd",
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
