"""Tests for read-only watch order intent ledger MCP tools (ROB-103)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp_server.tooling import watch_order_intent_ledger_read as mod


def _make_fake_row(**overrides: object):
    base = {
        "id": 1,
        "correlation_id": "corr-mcp-1",
        "idempotency_key": "k",
        "market": "kr",
        "target_kind": "asset",
        "symbol": "005930",
        "condition_type": "price_below",
        "threshold": 70000.0,
        "threshold_key": "70000",
        "action": "create_order_intent",
        "side": "buy",
        "account_mode": "kis_mock",
        "execution_source": "watch",
        "lifecycle_state": "previewed",
        "quantity": 1.0,
        "limit_price": 70000.0,
        "notional": 70000.0,
        "currency": "KRW",
        "notional_krw_input": None,
        "max_notional_krw": 1500000.0,
        "notional_krw_evaluated": 70000.0,
        "fx_usd_krw_used": None,
        "approval_required": True,
        "execution_allowed": False,
        "blocking_reasons": [],
        "blocked_by": None,
        "detail": {},
        "preview_line": {"lifecycle_state": "previewed"},
        "triggered_value": 69000.0,
        "kst_date": "2026-05-04",
        "created_at": datetime(2026, 5, 4, 0, 30, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _patched_session(monkeypatch: pytest.MonkeyPatch, rows):
    """Patch AsyncSessionLocal so the MCP tool sees a fake AsyncSession."""

    class _Scalars:
        def all(self):
            return rows

    class _Result:
        def scalar_one_or_none(self):
            return rows[0] if rows else None

        def scalars(self):
            return _Scalars()

    fake_session = AsyncMock()
    fake_session.execute = AsyncMock(return_value=_Result())

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_session)
    cm.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: cm)
    return fake_session


@pytest.mark.asyncio
async def test_list_recent_returns_serialized_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_session(monkeypatch, [_make_fake_row()])

    result = await mod.watch_order_intent_ledger_list_recent_impl()
    assert result["success"] is True
    assert result["count"] == 1
    assert result["items"][0]["correlation_id"] == "corr-mcp-1"


@pytest.mark.asyncio
async def test_list_recent_clamps_limit_to_one_hundred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_session(monkeypatch, [])

    result = await mod.watch_order_intent_ledger_list_recent_impl(limit=10_000)
    assert result["success"] is True
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_get_returns_item_when_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_session(monkeypatch, [_make_fake_row(correlation_id="corr-mcp-x")])

    result = await mod.watch_order_intent_ledger_get_impl("corr-mcp-x")
    assert result["success"] is True
    assert result["item"]["correlation_id"] == "corr-mcp-x"


@pytest.mark.asyncio
async def test_get_returns_not_found_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_session(monkeypatch, [])

    result = await mod.watch_order_intent_ledger_get_impl("does-not-exist")
    assert result["success"] is False
    assert result["error"] == "not_found"
