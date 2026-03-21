import asyncio
from typing import Any

import pytest

from app.mcp_server.tooling import portfolio_holdings
from app.mcp_server.tooling.screening import kr as screening_kr


def _mock_kr_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stk: list[dict[str, Any]],
    ksq: list[dict[str, Any]] | None = None,
    etfs: list[dict[str, Any]] | None = None,
    valuations: dict[str, dict[str, Any]] | None = None,
) -> None:
    async def mock_fetch_stock_all_cached(market: str) -> list[dict[str, Any]]:
        if market == "STK":
            return await asyncio.sleep(0, result=[dict(item) for item in stk])
        if market == "KSQ":
            return await asyncio.sleep(0, result=[dict(item) for item in (ksq or [])])
        return await asyncio.sleep(0, result=[])

    async def mock_fetch_etf_all_cached() -> list[dict[str, Any]]:
        return await asyncio.sleep(0, result=[dict(item) for item in (etfs or [])])

    async def mock_fetch_valuation_all_cached(
        market: str,
    ) -> dict[str, dict[str, Any]]:
        del market
        return await asyncio.sleep(0, result=valuations or {})

    monkeypatch.setattr(
        screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
    )
    monkeypatch.setattr(screening_kr, "fetch_etf_all_cached", mock_fetch_etf_all_cached)
    monkeypatch.setattr(
        screening_kr,
        "fetch_valuation_all_cached",
        mock_fetch_valuation_all_cached,
    )


def _mock_empty_holdings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mock_collect_portfolio_positions(
        *,
        account: str | None,
        market: str | None,
        include_current_price: bool,
        user_id: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
        del include_current_price, user_id
        return await asyncio.sleep(0, result=([], [], market, account))

    monkeypatch.setattr(
        portfolio_holdings,
        "_collect_portfolio_positions",
        mock_collect_portfolio_positions,
    )
