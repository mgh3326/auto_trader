"""Shared scenario for the get_holdings default-output golden test (task #963).

The golden JSON was generated from the get_holdings implementation BEFORE the
opt-in ``include_ledger_lots`` parameter existed, so the default (flag omitted or
False) output is pinned byte-for-byte against pre-change behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.mcp_server.tooling import portfolio_holdings
from app.services import protected_quantity_service
from tests._mcp_tooling_support import DummyMCP

GOLDEN_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "get_holdings_golden"
    / "default_output.json"
)


def kis_kr_position(symbol: str, quantity: float, avg: float) -> dict[str, Any]:
    return {
        "account": "kis",
        "account_name": "기본 계좌",
        "broker": "kis",
        "source": "kis_api",
        "instrument_type": "equity_kr",
        "market": "kr",
        "symbol": symbol,
        "name": f"name-{symbol}",
        "quantity": quantity,
        "sellable_quantity": quantity,
        "broker_sellable_quantity": quantity,
        "sellable_observed": True,
        "avg_buy_price": avg,
        "current_price": avg * 0.9,
        "evaluation_amount": quantity * avg * 0.9,
        "profit_loss": quantity * avg * -0.1,
        "profit_rate": -10.0,
    }


def kis_us_position(symbol: str = "AAPL") -> dict[str, Any]:
    return {
        "account": "kis",
        "account_name": "기본 계좌",
        "broker": "kis",
        "source": "kis_api",
        "instrument_type": "equity_us",
        "market": "us",
        "symbol": symbol,
        "name": symbol,
        "quantity": 3.0,
        "avg_buy_price": 150.0,
        "current_price": 160.0,
        "evaluation_amount": 480.0,
        "profit_loss": 30.0,
        "profit_rate": 6.67,
    }


def toss_api_kr_position(symbol: str, quantity: float, avg: float) -> dict[str, Any]:
    return {
        "account": "toss",
        "account_name": "Toss",
        "broker": "toss",
        "source": "toss_api",
        "instrument_type": "equity_kr",
        "market": "kr",
        "symbol": symbol,
        "name": f"name-{symbol}",
        "quantity": quantity,
        "avg_buy_price": avg,
        "current_price": avg * 1.02,
        "evaluation_amount": quantity * avg * 1.02,
        "profit_loss": quantity * avg * 0.02,
        "profit_rate": 2.0,
        "sellable_quantity": None,
        "broker_sellable_quantity": None,
        "sellable_observed": False,
    }


def scenario_positions() -> list[dict[str, Any]]:
    """A KIS KR pair, a KIS US row and a Toss KR row (same symbol as a KIS row)."""
    return [
        kis_kr_position("196170", 12.0, 100000.0),
        kis_kr_position("171090", 40.0, 25000.0),
        kis_us_position("AAPL"),
        toss_api_kr_position("171090", 5.0, 26000.0),
    ]


def pin_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every process-state dependency from the golden scenario.

    ``protection_state`` comes from a DB read (a missing/failed read reads as
    "unverified", a healthy empty table as "unprotected") and ``order_routable``
    from Toss mutation flags, so a golden pinned without these is order- and
    worker-dependent.
    """

    async def no_declaration(_key: Any) -> None:
        return None

    monkeypatch.setattr(
        protected_quantity_service, "_read_head_snapshot", no_declaration
    )
    for name, value in (
        ("toss_api_enabled", False),
        ("toss_live_order_mutations_enabled", False),
        ("protected_quantity_mode_kis_live", "off"),
        ("protected_quantity_mode_toss_live", "off"),
        ("protected_quantity_mode_upbit_live", "off"),
    ):
        monkeypatch.setattr(settings, name, value, raising=False)


def install_fake_collect(
    monkeypatch: pytest.MonkeyPatch,
    positions: list[dict[str, Any]] | None = None,
) -> None:
    pin_environment(monkeypatch)
    rows = positions if positions is not None else scenario_positions()

    async def fake_collect(**_kwargs: Any):
        # Fresh copies: the impl mutates position dicts in place.
        return [dict(row) for row in rows], [], None, None

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )


async def call_get_holdings(**kwargs: Any) -> dict[str, Any]:
    mcp = DummyMCP()
    portfolio_holdings._register_portfolio_tools_impl(mcp)
    return await mcp.tools["get_holdings"](**kwargs)


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
