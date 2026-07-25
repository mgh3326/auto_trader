"""Integration tests for the generic sell alert pipeline.

Covers:
- sell_conditions_service DB CRUD
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.sell_condition import SellCondition
from app.services.sell_conditions_service import (
    get_active_sell_conditions,
    get_sell_condition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sell_condition(
    symbol: str = "000660",
    name: str = "SK하이닉스",
    is_active: bool = True,
    price_threshold: float = 1_152_000.0,
    stoch_rsi_threshold: float = 80.0,
    foreign_days: int = 2,
    rsi_high: float = 70.0,
    rsi_low: float = 65.0,
    bb_upper_ref: float = 1_142_000.0,
) -> MagicMock:
    cond = MagicMock(spec=SellCondition)
    cond.symbol = symbol
    cond.name = name
    cond.is_active = is_active
    cond.price_threshold = price_threshold
    cond.stoch_rsi_threshold = stoch_rsi_threshold
    cond.foreign_days = foreign_days
    cond.rsi_high = rsi_high
    cond.rsi_low = rsi_low
    cond.bb_upper_ref = bb_upper_ref
    return cond


# ---------------------------------------------------------------------------
# sell_conditions_service — DB CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSellConditionsService:
    @pytest.mark.asyncio
    async def test_get_sell_condition_returns_match(self):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        expected = _make_sell_condition()
        mock_result.scalar_one_or_none.return_value = expected
        mock_db.execute.return_value = mock_result

        result = await get_sell_condition(mock_db, "000660")
        assert result is expected
        mock_db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_sell_condition_returns_none_for_missing(self):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await get_sell_condition(mock_db, "999999")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_active_sell_conditions_returns_active_only(self):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        active_conds = [
            _make_sell_condition("000660", "SK하이닉스"),
            _make_sell_condition("005930", "삼성전자"),
        ]
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = active_conds
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        result = await get_active_sell_conditions(mock_db)
        assert len(result) == 2
        assert result[0].symbol == "000660"
        assert result[1].symbol == "005930"

    @pytest.mark.asyncio
    async def test_get_active_sell_conditions_returns_empty(self):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        result = await get_active_sell_conditions(mock_db)
        assert result == []
