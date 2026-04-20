from __future__ import annotations

import pytest

from app.mcp_server.tooling import (
    watch_alerts_registration as watch_alerts_registration,
)
from app.mcp_server.tooling.registry import register_all_tools


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):
        _ = description

        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def build_tools() -> dict[str, object]:
    mcp = DummyMCP()
    register_all_tools(mcp)
    return mcp.tools


class _FakeWatchAlertService:
    def __init__(self) -> None:
        self.add_calls: list[tuple[str, str, str, float, str | None]] = []
        self.remove_calls: list[tuple[str, str, str, float, str | None]] = []
        self.list_calls: list[str | None] = []
        self.closed = False

    async def add_watch(
        self,
        market: str,
        symbol: str,
        condition_type: str,
        threshold: float,
        target_kind: str | None = None,
    ) -> dict[str, object]:
        self.add_calls.append((market, symbol, condition_type, threshold, target_kind))
        return {"created": True, "already_exists": False, "target_kind": target_kind}

    async def remove_watch(
        self,
        market: str,
        symbol: str,
        condition_type: str,
        threshold: float,
        target_kind: str | None = None,
    ) -> dict[str, object]:
        self.remove_calls.append(
            (market, symbol, condition_type, threshold, target_kind)
        )
        return {"removed": True}

    async def list_watches(
        self,
        market: str | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        self.list_calls.append(market)
        return {"crypto": []}

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_manage_watch_alerts_add_maps_metric_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = build_tools()

    fake_service = _FakeWatchAlertService()
    monkeypatch.setattr(
        watch_alerts_registration, "WatchAlertService", lambda: fake_service
    )

    result = await tools["manage_watch_alerts"](
        action="add",
        market="crypto",
        symbol="btc",
        metric="price",
        operator="below",
        threshold=90000000,
    )

    assert result["success"] is True
    assert result["symbol"] == "BTC"
    assert fake_service.add_calls[0][2] == "price_below"
    assert result["condition_type"] == "price_below"
    assert result["threshold"] == 90000000.0
    assert fake_service.closed is True


@pytest.mark.asyncio
async def test_manage_watch_alerts_passes_target_kind_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = build_tools()

    fake_service = _FakeWatchAlertService()
    monkeypatch.setattr(
        watch_alerts_registration, "WatchAlertService", lambda: fake_service
    )

    result = await tools["manage_watch_alerts"](
        action="add",
        market="kr",
        target_kind="index",
        symbol="kospi",
        metric="price",
        operator="below",
        threshold=6176.75,
    )

    assert result["success"] is True
    assert result["target_kind"] == "index"
    assert fake_service.add_calls == [("kr", "kospi", "price_below", 6176.75, "index")]


@pytest.mark.asyncio
async def test_manage_watch_alerts_accepts_trade_value_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = build_tools()

    fake_service = _FakeWatchAlertService()
    monkeypatch.setattr(
        watch_alerts_registration, "WatchAlertService", lambda: fake_service
    )

    result = await tools["manage_watch_alerts"](
        action="add",
        market="kr",
        symbol="005930",
        metric="trade_value",
        operator="above",
        threshold=1_000_000_000,
    )

    assert result["success"] is True
    assert result["condition_type"] == "trade_value_above"
    assert fake_service.add_calls[0][2] == "trade_value_above"


@pytest.mark.asyncio
async def test_manage_watch_alerts_rejects_unknown_action() -> None:
    tools = build_tools()

    result = await tools["manage_watch_alerts"](action="unknown")

    assert result["success"] is False
    assert "unknown action" in str(result["error"]).lower()
