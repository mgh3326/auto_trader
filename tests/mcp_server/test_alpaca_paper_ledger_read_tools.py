"""Tests for read-only Alpaca Paper ledger MCP tools (ROB-84/ROB-90)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

TOOL_PATH = (
    Path(__file__).parents[2] / "app/mcp_server/tooling/alpaca_paper_ledger_read.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_row(**kwargs):
    defaults = {
        "id": 1,
        "client_order_id": "test-client-001",
        "lifecycle_correlation_id": "test-client-001",
        "record_kind": "execution",
        "broker": "alpaca",
        "account_mode": "alpaca_paper",
        "lifecycle_state": "anomaly",
        "execution_symbol": "BTCUSD",
        "execution_venue": "alpaca_paper",
        "instrument_type": "crypto",
        "side": "buy",
        "order_type": "limit",
        "currency": "USD",
        "__table__": SimpleNamespace(
            columns=[
                SimpleNamespace(name="id"),
                SimpleNamespace(name="client_order_id"),
                SimpleNamespace(name="lifecycle_correlation_id"),
                SimpleNamespace(name="record_kind"),
                SimpleNamespace(name="broker"),
                SimpleNamespace(name="account_mode"),
                SimpleNamespace(name="lifecycle_state"),
            ]
        ),
    }
    defaults.update(kwargs)
    ns = SimpleNamespace(**defaults)
    return ns


def _mock_svc(*, row=None, rows=None, corr_rows=None):
    svc = AsyncMock()
    svc.get_by_client_order_id = AsyncMock(return_value=row)
    svc.list_recent = AsyncMock(
        return_value=rows if rows is not None else ([row] if row else [])
    )
    svc.list_by_correlation_id = AsyncMock(
        return_value=corr_rows if corr_rows is not None else []
    )
    return svc


class _FakeDB:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


def _patch_ledger_service(monkeypatch, mod, mock_svc) -> None:
    monkeypatch.setattr(mod, "_session_factory", lambda: lambda: _FakeDB())
    monkeypatch.setattr(
        "app.mcp_server.tooling.alpaca_paper_ledger_read.AlpacaPaperLedgerService",
        lambda db: mock_svc,
    )


# ---------------------------------------------------------------------------
# alpaca_paper_ledger_list_recent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_recent_returns_success_dict(monkeypatch):
    import app.mcp_server.tooling.alpaca_paper_ledger_read as mod

    row = _fake_row()
    mock_svc = _mock_svc(rows=[row])

    _patch_ledger_service(monkeypatch, mod, mock_svc)

    result = await mod.alpaca_paper_ledger_list_recent(limit=10)

    assert result["success"] is True
    assert result["account_mode"] == "alpaca_paper"
    assert result["source"] == "alpaca_paper_ledger"
    assert result["limit"] == 10
    assert isinstance(result["items"], list)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_recent_empty_returns_zero_count(monkeypatch):
    import app.mcp_server.tooling.alpaca_paper_ledger_read as mod

    mock_svc = _mock_svc(rows=[])

    _patch_ledger_service(monkeypatch, mod, mock_svc)

    result = await mod.alpaca_paper_ledger_list_recent(limit=50)
    assert result["count"] == 0
    assert result["items"] == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_recent_limit_capped_at_200(monkeypatch):
    import app.mcp_server.tooling.alpaca_paper_ledger_read as mod

    mock_svc = _mock_svc(rows=[])

    _patch_ledger_service(monkeypatch, mod, mock_svc)

    result = await mod.alpaca_paper_ledger_list_recent(limit=999)
    assert result["limit"] == 200


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_recent_invalid_limit_raises(monkeypatch):
    from app.mcp_server.tooling.alpaca_paper_ledger_read import (
        alpaca_paper_ledger_list_recent,
    )

    with pytest.raises(ValueError, match="limit must be >= 1"):
        await alpaca_paper_ledger_list_recent(limit=0)


# ---------------------------------------------------------------------------
# alpaca_paper_ledger_get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ledger_get_found(monkeypatch):
    import app.mcp_server.tooling.alpaca_paper_ledger_read as mod

    row = _fake_row()
    mock_svc = _mock_svc(row=row)

    _patch_ledger_service(monkeypatch, mod, mock_svc)

    result = await mod.alpaca_paper_ledger_get("test-client-001")
    assert result["success"] is True
    assert result["found"] is True
    assert result["client_order_id"] == "test-client-001"
    assert result["item"] is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ledger_get_not_found(monkeypatch):
    import app.mcp_server.tooling.alpaca_paper_ledger_read as mod

    mock_svc = _mock_svc(row=None)

    _patch_ledger_service(monkeypatch, mod, mock_svc)

    result = await mod.alpaca_paper_ledger_get("nonexistent")
    assert result["success"] is False
    assert result["found"] is False
    assert result["item"] is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ledger_get_empty_client_order_id_raises():
    from app.mcp_server.tooling.alpaca_paper_ledger_read import alpaca_paper_ledger_get

    with pytest.raises(ValueError, match="client_order_id is required"):
        await alpaca_paper_ledger_get("")


# ---------------------------------------------------------------------------
# alpaca_paper_execution_preflight_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_execution_preflight_check_returns_blocking_report(monkeypatch):
    import app.mcp_server.tooling.alpaca_paper_ledger_read as mod

    mock_svc = _mock_svc(rows=[])

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(mod, "_session_factory", lambda: lambda: _FakeDB())
    monkeypatch.setattr(
        "app.mcp_server.tooling.alpaca_paper_ledger_read.AlpacaPaperLedgerService",
        lambda db: mock_svc,
    )

    result = await mod.alpaca_paper_execution_preflight_check(
        limit=20,
        open_orders=[{"id": "order-1", "status": "new", "symbol": "BTCUSD"}],
    )

    assert result["success"] is True
    assert result["read_only"] is True
    assert result["source"] == "alpaca_paper_execution_preflight"
    assert result["should_block"] is True
    assert result["anomalies"][0]["check_id"] == "unexpected_open_orders"
    mock_svc.list_recent.assert_awaited_once_with(limit=20)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_execution_preflight_check_invalid_inputs_raise():
    from app.mcp_server.tooling.alpaca_paper_ledger_read import (
        alpaca_paper_execution_preflight_check,
    )

    with pytest.raises(ValueError, match="limit must be >= 1"):
        await alpaca_paper_execution_preflight_check(limit=0)
    with pytest.raises(ValueError, match="stale_after_minutes must be >= 1"):
        await alpaca_paper_execution_preflight_check(stale_after_minutes=0)


# ---------------------------------------------------------------------------
# alpaca_paper_ledger_get_by_correlation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ledger_get_by_correlation_returns_rows(monkeypatch):
    import app.mcp_server.tooling.alpaca_paper_ledger_read as mod

    buy_row = _fake_row(
        client_order_id="buy-001",
        lifecycle_correlation_id="corr-test",
        side="buy",
        lifecycle_state="filled",
    )
    sell_row = _fake_row(
        client_order_id="sell-001",
        lifecycle_correlation_id="corr-test",
        side="sell",
        lifecycle_state="closed",
    )
    mock_svc = _mock_svc(corr_rows=[buy_row, sell_row])

    _patch_ledger_service(monkeypatch, mod, mock_svc)

    result = await mod.alpaca_paper_ledger_get_by_correlation("corr-test")
    assert result["success"] is True
    assert result["lifecycle_correlation_id"] == "corr-test"
    assert result["count"] == 2
    assert len(result["items"]) == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ledger_get_by_correlation_empty_returns_zero_count(monkeypatch):
    import app.mcp_server.tooling.alpaca_paper_ledger_read as mod

    mock_svc = _mock_svc(corr_rows=[])

    _patch_ledger_service(monkeypatch, mod, mock_svc)

    result = await mod.alpaca_paper_ledger_get_by_correlation("no-such-corr")
    assert result["success"] is True
    assert result["count"] == 0
    assert result["items"] == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ledger_get_by_correlation_empty_id_raises():
    from app.mcp_server.tooling.alpaca_paper_ledger_read import (
        alpaca_paper_ledger_get_by_correlation,
    )

    with pytest.raises(ValueError, match="lifecycle_correlation_id is required"):
        await alpaca_paper_ledger_get_by_correlation("")


# ---------------------------------------------------------------------------
# Registered as read-only
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ledger_tool_names_are_in_alpaca_readonly_set():
    from app.mcp_server.tooling.alpaca_paper import ALPACA_PAPER_READONLY_TOOL_NAMES

    assert "alpaca_paper_ledger_list_recent" in ALPACA_PAPER_READONLY_TOOL_NAMES
    assert "alpaca_paper_ledger_get" in ALPACA_PAPER_READONLY_TOOL_NAMES
    assert "alpaca_paper_ledger_get_by_correlation" in ALPACA_PAPER_READONLY_TOOL_NAMES
    assert "alpaca_paper_execution_preflight_check" in ALPACA_PAPER_READONLY_TOOL_NAMES


# ---------------------------------------------------------------------------
# Static safety: no broker mutation in tool file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_file_has_no_broker_mutation():
    source = TOOL_PATH.read_text()
    forbidden = [
        "submit_order",
        "cancel_order",
        "place_order",
        "modify_order",
        "AlpacaPaperBrokerService",
        "from app.services.brokers",
        "from app.services.kis",
        "from app.services.upbit",
        "watch_alert",
        "order_intent",
        "scheduler",
    ]
    for term in forbidden:
        assert term not in source, f"Forbidden term in MCP tool file: {term!r}"


@pytest.mark.unit
def test_tool_file_is_valid_python():
    source = TOOL_PATH.read_text()
    tree = ast.parse(source)
    assert tree is not None
