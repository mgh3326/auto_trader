"""KR Korean-name precedence assertion tests (ROB-170 Task 5).

Verifies that for KR screener rows, the Korean name from kr_symbol_universe
takes precedence over whatever name the upstream screener service returns.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.invest_view_model.screener_service import build_screener_results


class _FakeResolver:
    def relation(self, market: str, symbol: str) -> str:
        return "none"


def _fake_screener_service(results: list[dict[str, Any]]) -> Any:
    svc = MagicMock()
    svc.list_screening = AsyncMock(
        return_value={
            "results": results,
            "warnings": [],
            "timestamp": dt.datetime.now(dt.UTC).isoformat(),
        }
    )
    return svc


_TEST_SYMBOL = "T70KR1"  # unique 6-char sentinel, not in any other test fixture


@pytest.mark.asyncio
async def test_kr_row_uses_kr_universe_name_over_upstream(db_session):
    """When kr_symbol_universe has a Korean name, it replaces the upstream row name."""
    db_session.add(
        KRSymbolUniverse(
            symbol=_TEST_SYMBOL,
            name="테스트종목",
            exchange="KOSPI",
            is_active=True,
        )
    )
    await db_session.commit()

    screening_svc = _fake_screener_service(
        [
            {
                "symbol": _TEST_SYMBOL,
                "market": "kr",
                "name": "Test Corp English",  # English name from upstream
                "close": 78500,
                "change_rate": 0.77,
                "consecutive_up_days": 3,
            }
        ]
    )
    response = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=screening_svc,
        resolver=_FakeResolver(),
        market="kr",
        session=db_session,
    )
    assert response.results[0].name == "테스트종목"


@pytest.mark.asyncio
async def test_kr_row_falls_back_to_upstream_name_when_universe_missing(db_session):
    """When kr_symbol_universe has no row, upstream name is used directly."""
    # No KRSymbolUniverse row added for 999999
    screening_svc = _fake_screener_service(
        [
            {
                "symbol": "999999",
                "market": "kr",
                "name": "Unknown Stock",
                "close": 10000,
                "consecutive_up_days": 1,
            }
        ]
    )
    response = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=screening_svc,
        resolver=_FakeResolver(),
        market="kr",
        session=db_session,
    )
    assert response.results[0].name == "Unknown Stock"


@pytest.mark.asyncio
async def test_kr_row_no_session_uses_upstream_name():
    """Without a session, name comes from the upstream screener row (legacy behavior)."""
    screening_svc = _fake_screener_service(
        [
            {
                "symbol": "005930",
                "market": "kr",
                "name": "삼성전자",
                "close": 78500,
                "consecutive_up_days": 5,
            }
        ]
    )
    response = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=screening_svc,
        resolver=_FakeResolver(),
        market="kr",
        # session not provided — legacy path
    )
    # No DB lookup — name is taken directly from the row
    assert response.results[0].name == "삼성전자"
