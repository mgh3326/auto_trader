"""C7 read projection coverage for the protected-quantity policy."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit


def _position(
    *,
    broker: str,
    source: str,
    instrument_type: str,
    market: str,
    symbol: str,
) -> dict[str, Any]:
    return {
        "broker": broker,
        "source": source,
        "instrument_type": instrument_type,
        "market": market,
        "symbol": symbol,
        "quantity": 100.0,
        "sellable_quantity": 80.0,
        "broker_sellable_quantity": 80.0,
        "sellable_observed": True,
    }


@pytest.mark.asyncio
async def test_c7_maps_live_positions_to_exact_policy_keys_and_preserves_raw_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import portfolio_holdings

    positions = [
        _position(
            broker="kis",
            source="kis_api",
            instrument_type="equity_kr",
            market="kr",
            symbol="005930",
        ),
        _position(
            broker="toss",
            source="toss_api",
            instrument_type="equity_us",
            market="us",
            symbol="BRK.B",
        ),
        _position(
            broker="upbit",
            source="upbit_api",
            instrument_type="crypto",
            market="crypto",
            symbol="KRW-BTC",
        ),
        _position(
            broker="manual",
            source="manual",
            instrument_type="equity_kr",
            market="kr",
            symbol="005930",
        ),
    ]
    original = deepcopy(positions)
    calls: list[dict[str, Any]] = []

    async def project(position: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            **position,
            "sellable_quantity": 20.0,
            "tactical_sellable_quantity": 20.0,
            "protected_quantity": 60.0,
            "protection_state": "covered",
        }

    monkeypatch.setattr(portfolio_holdings, "apply_position_protection", project)

    result = await portfolio_holdings._apply_protection_to_live_positions(
        positions,
        is_mock=False,
    )

    assert calls == [
        {"account_scope": "kis_live", "market": "kr", "symbol": "005930"},
        {"account_scope": "toss_live", "market": "us", "symbol": "BRK.B"},
        {
            "account_scope": "upbit_live",
            "market": "crypto",
            "symbol": "KRW-BTC",
        },
    ]
    assert result[0]["quantity"] == original[0]["quantity"]
    assert (
        result[0]["broker_sellable_quantity"] == original[0]["broker_sellable_quantity"]
    )
    assert result[0]["sellable_quantity"] == 20.0
    assert result[3] == original[3]


@pytest.mark.asyncio
async def test_c7_mock_positions_skip_policy_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import portfolio_holdings

    lookup = AsyncMock()
    monkeypatch.setattr(portfolio_holdings, "apply_position_protection", lookup)
    positions = [
        _position(
            broker="kis",
            source="kis_api",
            instrument_type="equity_kr",
            market="kr",
            symbol="005930",
        )
    ]

    result = await portfolio_holdings._apply_protection_to_live_positions(
        positions,
        is_mock=True,
    )

    assert result == positions
    lookup.assert_not_awaited()


def test_c7_output_exposes_protection_evidence_without_rewriting_total_quantity() -> (
    None
):
    from app.mcp_server.tooling.portfolio_helpers import position_to_output

    result = position_to_output(
        {
            **_position(
                broker="kis",
                source="kis_api",
                instrument_type="equity_kr",
                market="kr",
                symbol="005930",
            ),
            "name": "Samsung",
            "avg_buy_price": 1.0,
            "current_price": 1.0,
            "evaluation_amount": 100.0,
            "profit_loss": 0.0,
            "profit_rate": 0.0,
            "tactical_sellable_quantity": 20.0,
            "protected_quantity": 60.0,
            "protection_state": "covered",
        }
    )

    assert result["quantity"] == 100.0
    assert result["sellable_quantity"] == 80.0
    assert result["broker_sellable_quantity"] == 80.0
    assert result["sellable_observed"] is True
    assert result["tactical_sellable_quantity"] == 20.0
    assert result["protected_quantity"] == 60.0
    assert result["protection_state"] == "covered"
