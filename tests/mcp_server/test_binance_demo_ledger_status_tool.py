"""ROB-907 — tests for the read-only binance_demo_ledger_status MCP tool.

No broker call, no DB write. The ledger service is mocked; these tests
exercise response shape, limit/validation guards, and the same DEFAULT-
profile flag gate (settings.binance_demo_scalping_enabled) as the existing
binance_demo_scalping_submit_decision tool.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import registry as registry_mod
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP

_TOOL = "binance_demo_ledger_status"


def _fake_recent_row(**kwargs: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "id": 1,
        "client_order_id": "demo-recent-1",
        "product": "spot",
        "lifecycle_state": "reconciled",
        "planned_at": dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=dt.UTC),
        "__table__": SimpleNamespace(
            columns=[
                SimpleNamespace(name="id"),
                SimpleNamespace(name="client_order_id"),
                SimpleNamespace(name="product"),
                SimpleNamespace(name="lifecycle_state"),
                SimpleNamespace(name="planned_at"),
            ]
        ),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _fake_stale_row(**kwargs: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "client_order_id": "demo-stale-1",
        "product": "usdm_futures",
        "instrument_id": 42,
        "lifecycle_state": "planned",
        "planned_at": dt.datetime(2026, 5, 1, 0, 0, 0, tzinfo=dt.UTC),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _mock_svc(
    *,
    status_distribution: dict[str, int] | None = None,
    open_root_count: int = 0,
    stale_roots: list[Any] | None = None,
    latest_activity_at: dt.datetime | None = None,
    recent: list[Any] | None = None,
) -> AsyncMock:
    svc = AsyncMock()
    svc.status_distribution = AsyncMock(
        return_value=status_distribution
        if status_distribution is not None
        else {"planned": 1, "reconciled": 3, "anomaly": 1}
    )
    svc.count_open_lifecycles = AsyncMock(return_value=open_root_count)
    svc.stale_open_roots = AsyncMock(
        return_value=stale_roots if stale_roots is not None else []
    )
    svc.latest_activity_at = AsyncMock(return_value=latest_activity_at)
    svc.list_recent = AsyncMock(return_value=recent if recent is not None else [])
    return svc


class _FakeDB:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


def _patch_ledger_service(monkeypatch, mod, mock_svc) -> None:
    monkeypatch.setattr(mod, "_session_factory", lambda: lambda: _FakeDB())
    monkeypatch.setattr(
        "app.mcp_server.tooling.binance_demo_ledger_status_read.BinanceDemoLedgerService",
        lambda db: mock_svc,
    )


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_status_returns_expected_shape(monkeypatch):
    import app.mcp_server.tooling.binance_demo_ledger_status_read as mod

    mock_svc = _mock_svc(
        status_distribution={"planned": 2, "reconciled": 5, "anomaly": 1},
        open_root_count=2,
        stale_roots=[_fake_stale_row()],
        latest_activity_at=dt.datetime(2026, 7, 16, 12, 0, 0, tzinfo=dt.UTC),
        recent=[_fake_recent_row()],
    )
    _patch_ledger_service(monkeypatch, mod, mock_svc)

    result = await mod.binance_demo_ledger_status(
        stale_age_seconds=3600, recent_limit=10
    )

    assert result["success"] is True
    assert result["read_only"] is True
    assert result["status_distribution"] == {
        "planned": 2,
        "reconciled": 5,
        "anomaly": 1,
    }
    assert result["open_root_count"] == 2
    assert result["anomaly_count"] == 1
    assert result["stale_age_seconds"] == 3600
    assert len(result["stale_open_roots"]) == 1
    assert result["stale_open_roots"][0]["client_order_id"] == "demo-stale-1"
    assert result["stale_open_roots"][0]["age_seconds"] > 0
    assert result["latest_activity_at"] == "2026-07-16T12:00:00+00:00"
    assert result["recent_limit"] == 10
    assert len(result["recent"]) == 1
    assert result["recent"][0]["client_order_id"] == "demo-recent-1"
    assert "as_of" in result


@pytest.mark.asyncio
@pytest.mark.unit
async def test_status_anomaly_count_zero_when_absent(monkeypatch):
    import app.mcp_server.tooling.binance_demo_ledger_status_read as mod

    mock_svc = _mock_svc(status_distribution={"planned": 1})
    _patch_ledger_service(monkeypatch, mod, mock_svc)

    result = await mod.binance_demo_ledger_status()
    assert result["anomaly_count"] == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_status_latest_activity_at_none_when_ledger_empty(monkeypatch):
    import app.mcp_server.tooling.binance_demo_ledger_status_read as mod

    mock_svc = _mock_svc(status_distribution={}, latest_activity_at=None)
    _patch_ledger_service(monkeypatch, mod, mock_svc)

    result = await mod.binance_demo_ledger_status()
    assert result["latest_activity_at"] is None
    assert result["status_distribution"] == {}


# ---------------------------------------------------------------------------
# Validation / caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_recent_limit_capped_at_200(monkeypatch):
    import app.mcp_server.tooling.binance_demo_ledger_status_read as mod

    mock_svc = _mock_svc()
    _patch_ledger_service(monkeypatch, mod, mock_svc)

    result = await mod.binance_demo_ledger_status(recent_limit=999)
    assert result["recent_limit"] == 200
    mock_svc.list_recent.assert_awaited_once_with(limit=200)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_recent_limit_below_one_raises():
    from app.mcp_server.tooling.binance_demo_ledger_status_read import (
        binance_demo_ledger_status,
    )

    with pytest.raises(ValueError, match="recent_limit must be >= 1"):
        await binance_demo_ledger_status(recent_limit=0)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_negative_stale_age_seconds_raises():
    from app.mcp_server.tooling.binance_demo_ledger_status_read import (
        binance_demo_ledger_status,
    )

    with pytest.raises(ValueError, match="stale_age_seconds must be >= 0"):
        await binance_demo_ledger_status(stale_age_seconds=-1)


# ---------------------------------------------------------------------------
# Registry gate — same DEFAULT-profile flag as binance_demo_scalping_submit_decision
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_absent_when_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry_mod.settings, "binance_demo_scalping_enabled", False, raising=False
    )
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    assert _TOOL not in set(mcp.tools.keys())


@pytest.mark.unit
def test_tool_present_when_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry_mod.settings, "binance_demo_scalping_enabled", True, raising=False
    )
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    assert _TOOL in set(mcp.tools.keys())
