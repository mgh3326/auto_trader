from __future__ import annotations

from typing import Any

import pytest

from app.mcp_server.tooling import analysis_tool_handlers, portfolio_holdings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_build_batch_position_index_requests_no_sellable(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return [], [], kwargs.get("market"), None

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    index, err = await analysis_tool_handlers._build_batch_position_index("kr")

    assert err is None
    assert index == {}
    # ROB-685: the batch index never reads sellable_quantity.
    assert captured["need_sellable"] is False
    assert captured["include_current_price"] is False


async def test_collect_portfolio_positions_forwards_need_sellable_to_toss(monkeypatch):
    calls: list[bool] = []

    async def fake_fetch(*, need_sellable: bool = True, **_):
        calls.append(need_sellable)

        class _Snap:
            positions: list[Any] = []
            errors: list[Any] = []

        return _Snap()

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch)

    # Isolate the sibling collectors — with market=None, _collect_portfolio_positions
    # otherwise fans out to the REAL _collect_kis_positions / _collect_upbit_positions
    # (live KIS/Upbit HTTP) and _collect_manual_positions (AsyncSessionLocal DB). That
    # makes this a slow, non-hermetic test, and _collect_upbit_positions can surface
    # UpbitSymbolUniverseLookupError, which _collect_portfolio_positions RE-RAISES
    # (portfolio_holdings.py:857-858) → the test would crash before asserting. Stub
    # all three to empty so only the toss forwarding path is exercised. They are
    # module globals resolved at call time, so setattr on the module patches them.
    async def _empty(*args, **kwargs):
        return [], []

    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", _empty)
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", _empty)
    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", _empty)

    # Default path keeps sellable (MCP / sell-classification contract).
    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False
    )
    # Explicit opt-out threads through.
    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False, need_sellable=False
    )

    assert calls == [True, False]


async def test_collect_toss_api_positions_defaults_need_sellable_true(monkeypatch):
    seen: list[bool] = []

    async def fake_fetch(*, need_sellable: bool = True, **_):
        seen.append(need_sellable)

        class _Snap:
            positions: list[Any] = []
            errors: list[Any] = []

        return _Snap()

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch)

    await portfolio_holdings._collect_toss_api_positions(None)
    await portfolio_holdings._collect_toss_api_positions(None, need_sellable=False)

    assert seen == [True, False]


async def test_collect_toss_api_positions_uses_shared_cache_and_skips_cash(monkeypatch):
    seen: dict[str, Any] = {}
    sentinel_cache = object()

    async def fake_fetch(*, need_sellable=True, need_cash=True, sellable_cache=None):
        seen["need_sellable"] = need_sellable
        seen["need_cash"] = need_cash
        seen["sellable_cache"] = sellable_cache

        class _Snap:
            positions: list[Any] = []
            errors: list[Any] = []

        return _Snap()

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(portfolio_holdings, "fetch_toss_portfolio_snapshot", fake_fetch)
    monkeypatch.setattr(
        portfolio_holdings, "get_shared_sellable_cache", lambda: sentinel_cache
    )

    # Default: shared cache is used; cash fanout skipped.
    await portfolio_holdings._collect_toss_api_positions(None)
    assert seen["need_sellable"] is True
    assert seen["need_cash"] is False
    assert seen["sellable_cache"] is sentinel_cache

    # fresh_sellable=True bypasses the cache (fresh fanout), still skips cash.
    await portfolio_holdings._collect_toss_api_positions(None, fresh_sellable=True)
    assert seen["need_cash"] is False
    assert seen["sellable_cache"] is None


async def test_collect_portfolio_positions_forwards_fresh_sellable(monkeypatch):
    seen: list[bool] = []

    async def fake_collect_toss(market_filter, *, need_sellable=True, fresh_sellable=False):
        seen.append(fresh_sellable)
        return [], [], False

    async def _empty(*args, **kwargs):
        return [], []

    monkeypatch.setattr(portfolio_holdings.settings, "toss_api_enabled", True)
    monkeypatch.setattr(
        portfolio_holdings, "_collect_toss_api_positions", fake_collect_toss
    )
    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", _empty)
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", _empty)
    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", _empty)

    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False
    )
    await portfolio_holdings._collect_portfolio_positions(
        account=None, market=None, include_current_price=False, fresh_sellable=True
    )

    assert seen == [False, True]


async def test_get_holdings_impl_forwards_fresh_sellable(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_collect(**kwargs):
        seen.update(kwargs)
        return [], [], None, None

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    await portfolio_holdings._get_holdings_impl()
    assert seen["fresh_sellable"] is False

    await portfolio_holdings._get_holdings_impl(fresh_sellable=True)
    assert seen["fresh_sellable"] is True
