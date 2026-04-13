# Paper Trading Account Management MCP Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Paper Trading account lifecycle (create / list / reset / delete) as 4 dedicated MCP tools so AI agents can manage virtual accounts.

**Architecture:** Add a new `paper_account_registration.py` module under `app/mcp_server/tooling/` that defines 4 tools. Each tool opens an async DB session via `AsyncSessionLocal`, instantiates `PaperTradingService`, calls the matching service method, and returns JSON-safe dicts (Decimal → float, datetime → ISO string). Wire it into `registry.register_all_tools`.

**Tech Stack:** FastMCP (`@mcp.tool`), SQLAlchemy async (`AsyncSessionLocal`, `AsyncSession`), existing `PaperTradingService` at `app/services/paper_trading_service.py`, pytest + `DummyMCP` helper from `tests/_mcp_tooling_support.py`.

---

## File Structure

- **Create:** `app/mcp_server/tooling/paper_account_registration.py` — 4 tool implementations + `register_paper_account_tools(mcp)` + a `_serialize_account` helper. All in one file (small surface, matches existing "inline registration" pattern from `orders_registration.py`).
- **Modify:** `app/mcp_server/tooling/registry.py` — add import and call to `register_paper_account_tools`.
- **Create:** `tests/test_paper_account_tools.py` — registration + behavioral tests using `DummyMCP` + `build_tools()` patterns.

## Design Notes

- **Session pattern:** Follow `trade_journal_tools.py` — `async with _session_factory()() as db:` where `_session_factory()` wraps `AsyncSessionLocal`. Each tool call opens its own session.
- **Error handling:** Service raises `ValueError` for "not found" / "duplicate name" (via DB `UniqueConstraint`) / etc. Tools catch `ValueError` and return `{"success": False, "error": "<message>"}`. Let other exceptions propagate.
- **`list_paper_accounts` enrichment:** For each account, call `service.get_portfolio_summary(account.id)` to get `positions_count`, `total_evaluated`, `total_pnl_pct`. Note: `get_portfolio_summary` fetches live prices for every position — this is by design so agents get a current snapshot.
- **Decimal serialization:** All `Decimal` money/qty values are converted to `float` for JSON safety (consistent with `trade_journal_tools._serialize_journal`). `datetime` → `.isoformat()`.
- **Duplicate-name handling:** `PaperAccount.name` has `UniqueConstraint("name", name="uq_paper_accounts_name")`. A second `create_account` with the same name raises `sqlalchemy.exc.IntegrityError` on commit. Catch it and return a friendly error.
- **KIS/US consideration:** `total_evaluated` in `get_portfolio_summary` sums KRW + USD position values as raw Decimals (no FX conversion). We surface it verbatim as `total_evaluated_krw` — documented in the tool description as a caveat so callers know it mixes currencies if both KRW and USD positions exist. Matches existing service behavior; do not add FX logic in this plan.

---

## Task 1: Skeleton — create module + registration wired through registry

**Files:**
- Create: `app/mcp_server/tooling/paper_account_registration.py`
- Modify: `app/mcp_server/tooling/registry.py:22` (add import) and `app/mcp_server/tooling/registry.py:40` (add call)
- Test: `tests/test_paper_account_tools.py`

- [ ] **Step 1: Write a failing registration test**

Create `tests/test_paper_account_tools.py`:

```python
"""Tests for paper trading account management MCP tools."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.paper_trading import PaperAccount
from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
async def test_paper_account_tools_registered() -> None:
    """All 4 paper account management tools must be registered."""
    tools = build_tools()
    assert "create_paper_account" in tools
    assert "list_paper_accounts" in tools
    assert "reset_paper_account" in tools
    assert "delete_paper_account" in tools
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_paper_account_tools.py::test_paper_account_tools_registered -v`
Expected: FAIL — the four tools are not yet in the registry.

- [ ] **Step 3: Create the stub registration module**

Create `app/mcp_server/tooling/paper_account_registration.py`:

```python
"""Paper Trading account management MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal

if TYPE_CHECKING:
    from fastmcp import FastMCP

PAPER_ACCOUNT_TOOL_NAMES: set[str] = {
    "create_paper_account",
    "list_paper_accounts",
    "reset_paper_account",
    "delete_paper_account",
}


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def register_paper_account_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="create_paper_account",
        description="Create a new paper trading account (stub).",
    )
    async def create_paper_account(
        name: str,
        initial_capital: float = 100_000_000.0,
        initial_capital_usd: float = 0.0,
        description: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @mcp.tool(
        name="list_paper_accounts",
        description="List paper trading accounts (stub).",
    )
    async def list_paper_accounts(is_active: bool = True) -> dict[str, Any]:
        raise NotImplementedError

    @mcp.tool(
        name="reset_paper_account",
        description="Reset a paper trading account (stub).",
    )
    async def reset_paper_account(name: str) -> dict[str, Any]:
        raise NotImplementedError

    @mcp.tool(
        name="delete_paper_account",
        description="Delete a paper trading account (stub).",
    )
    async def delete_paper_account(name: str) -> dict[str, Any]:
        raise NotImplementedError


__all__ = ["PAPER_ACCOUNT_TOOL_NAMES", "register_paper_account_tools"]
```

- [ ] **Step 4: Wire into `registry.py`**

Edit `app/mcp_server/tooling/registry.py`:

Add after the existing `watch_alerts_registration` import (line 22-24):

```python
from app.mcp_server.tooling.paper_account_registration import (
    register_paper_account_tools,
)
```

Add inside `register_all_tools` at the end (after `register_trade_journal_tools(mcp)`):

```python
    register_paper_account_tools(mcp)
```

- [ ] **Step 5: Run the registration test to confirm it passes**

Run: `uv run pytest tests/test_paper_account_tools.py::test_paper_account_tools_registered -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/paper_account_registration.py app/mcp_server/tooling/registry.py tests/test_paper_account_tools.py
git commit -m "feat(paper): scaffold paper account MCP tool registration"
```

---

## Task 2: Account serializer helper

**Files:**
- Modify: `app/mcp_server/tooling/paper_account_registration.py`
- Test: `tests/test_paper_account_tools.py`

`_serialize_account(account, *, positions_count=None, total_evaluated=None, total_pnl_pct=None)` converts a `PaperAccount` ORM row into a JSON-safe dict, optionally enriched with summary fields. Factored out so create/list/reset all reuse it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_paper_account_tools.py`:

```python
from datetime import datetime, timezone

from app.mcp_server.tooling.paper_account_registration import _serialize_account


def _make_account(**overrides) -> PaperAccount:
    defaults = dict(
        id=1,
        name="default",
        initial_capital=Decimal("100000000"),
        cash_krw=Decimal("95000000"),
        cash_usd=Decimal("0"),
        description=None,
        strategy_name=None,
        is_active=True,
        created_at=datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return PaperAccount(**defaults)


def test_serialize_account_basic_fields() -> None:
    acc = _make_account()
    out = _serialize_account(acc)
    assert out["id"] == 1
    assert out["name"] == "default"
    assert out["initial_capital"] == 100_000_000.0
    assert out["cash_krw"] == 95_000_000.0
    assert out["cash_usd"] == 0.0
    assert out["strategy_name"] is None
    assert out["created_at"] == "2026-04-13T10:00:00+00:00"
    # Summary fields absent when not provided
    assert "positions_count" not in out
    assert "total_evaluated_krw" not in out
    assert "total_pnl_pct" not in out


def test_serialize_account_with_summary() -> None:
    acc = _make_account()
    out = _serialize_account(
        acc,
        positions_count=3,
        total_evaluated=Decimal("98500000"),
        total_pnl_pct=Decimal("-1.50"),
    )
    assert out["positions_count"] == 3
    assert out["total_evaluated_krw"] == 98_500_000.0
    assert out["total_pnl_pct"] == -1.5


def test_serialize_account_none_totals_become_null() -> None:
    acc = _make_account()
    out = _serialize_account(
        acc,
        positions_count=0,
        total_evaluated=None,
        total_pnl_pct=None,
    )
    assert out["positions_count"] == 0
    assert out["total_evaluated_krw"] is None
    assert out["total_pnl_pct"] is None
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/test_paper_account_tools.py -v -k serialize`
Expected: FAIL — `_serialize_account` is not defined.

- [ ] **Step 3: Implement `_serialize_account`**

Add to `app/mcp_server/tooling/paper_account_registration.py` (before `register_paper_account_tools`):

```python
from decimal import Decimal

from app.models.paper_trading import PaperAccount


def _to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _serialize_account(
    account: PaperAccount,
    *,
    positions_count: int | None = None,
    total_evaluated: Decimal | None = None,
    total_pnl_pct: Decimal | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": account.id,
        "name": account.name,
        "initial_capital": float(account.initial_capital),
        "cash_krw": float(account.cash_krw),
        "cash_usd": float(account.cash_usd),
        "description": account.description,
        "strategy_name": account.strategy_name,
        "is_active": account.is_active,
        "created_at": account.created_at.isoformat() if account.created_at else None,
        "updated_at": account.updated_at.isoformat() if account.updated_at else None,
    }
    if positions_count is not None:
        data["positions_count"] = positions_count
        data["total_evaluated_krw"] = _to_float(total_evaluated)
        data["total_pnl_pct"] = _to_float(total_pnl_pct)
    return data
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/test_paper_account_tools.py -v -k serialize`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/paper_account_registration.py tests/test_paper_account_tools.py
git commit -m "feat(paper): add _serialize_account helper for MCP tools"
```

---

## Task 3: `create_paper_account` implementation

**Files:**
- Modify: `app/mcp_server/tooling/paper_account_registration.py`
- Test: `tests/test_paper_account_tools.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_paper_account_tools.py`:

```python
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError


class _SessionCtx:
    """Async-context wrapper that yields a pre-made mock db."""

    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _patch_session(monkeypatch, db) -> None:
    """Make _session_factory()() yield our mock db."""
    factory = MagicMock()
    factory.return_value = _SessionCtx(db)
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_account_registration._session_factory",
        lambda: factory,
    )


@pytest.mark.asyncio
async def test_create_paper_account_success(monkeypatch) -> None:
    db = AsyncMock()
    db.add = MagicMock()

    async def _refresh(instance):
        instance.id = 42
        instance.created_at = instance.created_at or None
        instance.updated_at = instance.updated_at or None

    db.refresh = AsyncMock(side_effect=_refresh)
    _patch_session(monkeypatch, db)

    tools = build_tools()
    result = await tools["create_paper_account"](
        name="bot-1",
        initial_capital=50_000_000,
        description="test",
    )

    assert result["success"] is True
    assert result["account"]["id"] == 42
    assert result["account"]["name"] == "bot-1"
    assert result["account"]["initial_capital"] == 50_000_000.0
    assert result["account"]["cash_krw"] == 50_000_000.0
    assert result["account"]["description"] == "test"


@pytest.mark.asyncio
async def test_create_paper_account_duplicate_name(monkeypatch) -> None:
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock(
        side_effect=IntegrityError("INSERT", {}, Exception("unique"))
    )
    _patch_session(monkeypatch, db)

    tools = build_tools()
    result = await tools["create_paper_account"](name="dup")

    assert result["success"] is False
    assert "already exists" in result["error"].lower() or "duplicate" in result["error"].lower()
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_paper_account_tools.py -v -k create_paper_account`
Expected: FAIL — tool still raises `NotImplementedError`.

- [ ] **Step 3: Implement `create_paper_account`**

Replace the stub inside `register_paper_account_tools` with the full version. First update imports at the top of `paper_account_registration.py`:

```python
import logging

from sqlalchemy.exc import IntegrityError

from app.services.paper_trading_service import PaperTradingService

logger = logging.getLogger(__name__)
```

Replace the `create_paper_account` tool body:

```python
    @mcp.tool(
        name="create_paper_account",
        description=(
            "Create a new paper trading (모의투자) account. "
            "initial_capital is the KRW opening balance (default 100,000,000 KRW = 1억). "
            "initial_capital_usd adds a separate USD cash balance for US equity simulation. "
            "Account name must be unique."
        ),
    )
    async def create_paper_account(
        name: str,
        initial_capital: float = 100_000_000.0,
        initial_capital_usd: float = 0.0,
        description: str | None = None,
    ) -> dict[str, Any]:
        try:
            async with _session_factory()() as db:
                service = PaperTradingService(db)
                account = await service.create_account(
                    name=name,
                    initial_capital_krw=Decimal(str(initial_capital)),
                    initial_capital_usd=Decimal(str(initial_capital_usd)),
                    description=description,
                )
                return {"success": True, "account": _serialize_account(account)}
        except IntegrityError:
            return {
                "success": False,
                "error": f"Paper account '{name}' already exists",
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/test_paper_account_tools.py -v -k create_paper_account`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/paper_account_registration.py tests/test_paper_account_tools.py
git commit -m "feat(paper): implement create_paper_account MCP tool"
```

---

## Task 4: `list_paper_accounts` implementation

**Files:**
- Modify: `app/mcp_server/tooling/paper_account_registration.py`
- Test: `tests/test_paper_account_tools.py`

For each account returned by `service.list_accounts`, the tool calls `service.get_portfolio_summary(account.id)` to enrich it with position count / total evaluated / pnl%.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_paper_account_tools.py`:

```python
@pytest.mark.asyncio
async def test_list_paper_accounts_returns_enriched(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    acc1 = _make_account(id=1, name="default")
    acc2 = _make_account(
        id=2, name="us-bot", cash_krw=Decimal("0"), cash_usd=Decimal("5000")
    )

    async def _list(is_active):
        assert is_active is True
        return [acc1, acc2]

    summaries = {
        1: {
            "total_invested": Decimal("0"),
            "total_evaluated": Decimal("98500000"),
            "total_pnl": Decimal("-1500000"),
            "total_pnl_pct": Decimal("-1.50"),
            "cash_krw": acc1.cash_krw,
            "cash_usd": acc1.cash_usd,
            "positions_count": 3,
        },
        2: {
            "total_invested": Decimal("0"),
            "total_evaluated": Decimal("5100"),
            "total_pnl": Decimal("100"),
            "total_pnl_pct": Decimal("2.00"),
            "cash_krw": acc2.cash_krw,
            "cash_usd": acc2.cash_usd,
            "positions_count": 1,
        },
    }

    async def _summary(account_id):
        return summaries[account_id]

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.list_accounts = AsyncMock(side_effect=_list)
        svc.get_portfolio_summary = AsyncMock(side_effect=_summary)

        tools = build_tools()
        result = await tools["list_paper_accounts"]()

    assert result["success"] is True
    assert len(result["accounts"]) == 2
    first = result["accounts"][0]
    assert first["id"] == 1
    assert first["positions_count"] == 3
    assert first["total_evaluated_krw"] == 98_500_000.0
    assert first["total_pnl_pct"] == -1.5
    second = result["accounts"][1]
    assert second["id"] == 2
    assert second["cash_usd"] == 5000.0


@pytest.mark.asyncio
async def test_list_paper_accounts_is_active_false(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    captured: dict[str, object] = {}

    async def _list(is_active):
        captured["is_active"] = is_active
        return []

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.list_accounts = AsyncMock(side_effect=_list)

        tools = build_tools()
        result = await tools["list_paper_accounts"](is_active=False)

    assert captured["is_active"] is False
    assert result == {"success": True, "accounts": []}
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/test_paper_account_tools.py -v -k list_paper_accounts`
Expected: FAIL — tool raises `NotImplementedError`.

- [ ] **Step 3: Implement `list_paper_accounts`**

Replace the stub tool body:

```python
    @mcp.tool(
        name="list_paper_accounts",
        description=(
            "List paper trading accounts with per-account summary "
            "(positions_count, total_evaluated_krw, total_pnl_pct). "
            "Note: total_evaluated_krw sums KRW and USD position values verbatim "
            "— it does not convert USD to KRW. "
            "is_active=True (default) filters to active accounts only."
        ),
    )
    async def list_paper_accounts(is_active: bool = True) -> dict[str, Any]:
        async with _session_factory()() as db:
            service = PaperTradingService(db)
            accounts = await service.list_accounts(is_active=is_active)

            out: list[dict[str, Any]] = []
            for account in accounts:
                try:
                    summary = await service.get_portfolio_summary(account.id)
                    out.append(
                        _serialize_account(
                            account,
                            positions_count=summary["positions_count"],
                            total_evaluated=summary.get("total_evaluated"),
                            total_pnl_pct=summary.get("total_pnl_pct"),
                        )
                    )
                except Exception as exc:  # summary is best-effort
                    logger.warning(
                        "get_portfolio_summary failed for account %s: %s",
                        account.id,
                        exc,
                    )
                    out.append(_serialize_account(account, positions_count=0))
            return {"success": True, "accounts": out}
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/test_paper_account_tools.py -v -k list_paper_accounts`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/paper_account_registration.py tests/test_paper_account_tools.py
git commit -m "feat(paper): implement list_paper_accounts MCP tool"
```

---

## Task 5: `reset_paper_account` implementation

**Files:**
- Modify: `app/mcp_server/tooling/paper_account_registration.py`
- Test: `tests/test_paper_account_tools.py`

Lookup by name → call `service.reset_account(account.id)`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_paper_account_tools.py`:

```python
@pytest.mark.asyncio
async def test_reset_paper_account_success(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    acc = _make_account(id=7, name="reset-me")
    reset_acc = _make_account(
        id=7, name="reset-me", cash_krw=Decimal("100000000"), cash_usd=Decimal("0")
    )

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=acc)
        svc.reset_account = AsyncMock(return_value=reset_acc)

        tools = build_tools()
        result = await tools["reset_paper_account"](name="reset-me")

    svc.reset_account.assert_awaited_once_with(7)
    assert result["success"] is True
    assert result["account"]["id"] == 7
    assert result["account"]["cash_krw"] == 100_000_000.0


@pytest.mark.asyncio
async def test_reset_paper_account_missing(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=None)

        tools = build_tools()
        result = await tools["reset_paper_account"](name="ghost")

    assert result["success"] is False
    assert "not found" in result["error"].lower()
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/test_paper_account_tools.py -v -k reset_paper_account`
Expected: FAIL.

- [ ] **Step 3: Implement `reset_paper_account`**

Replace the stub tool body:

```python
    @mcp.tool(
        name="reset_paper_account",
        description=(
            "Reset a paper trading account: deletes ALL positions and restores "
            "cash_krw to initial_capital (cash_usd goes to 0). Irreversible. "
            "Account is looked up by unique name."
        ),
    )
    async def reset_paper_account(name: str) -> dict[str, Any]:
        async with _session_factory()() as db:
            service = PaperTradingService(db)
            account = await service.get_account_by_name(name)
            if account is None:
                return {
                    "success": False,
                    "error": f"Paper account '{name}' not found",
                }
            try:
                refreshed = await service.reset_account(account.id)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            return {"success": True, "account": _serialize_account(refreshed)}
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/test_paper_account_tools.py -v -k reset_paper_account`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/paper_account_registration.py tests/test_paper_account_tools.py
git commit -m "feat(paper): implement reset_paper_account MCP tool"
```

---

## Task 6: `delete_paper_account` implementation

**Files:**
- Modify: `app/mcp_server/tooling/paper_account_registration.py`
- Test: `tests/test_paper_account_tools.py`

Lookup by name → `service.delete_account(account.id)`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_paper_account_tools.py`:

```python
@pytest.mark.asyncio
async def test_delete_paper_account_success(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    acc = _make_account(id=9, name="goodbye")

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=acc)
        svc.delete_account = AsyncMock(return_value=True)

        tools = build_tools()
        result = await tools["delete_paper_account"](name="goodbye")

    svc.delete_account.assert_awaited_once_with(9)
    assert result == {"success": True, "deleted": True, "name": "goodbye", "id": 9}


@pytest.mark.asyncio
async def test_delete_paper_account_missing(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=None)

        tools = build_tools()
        result = await tools["delete_paper_account"](name="ghost")

    assert result["success"] is False
    assert "not found" in result["error"].lower()
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/test_paper_account_tools.py -v -k delete_paper_account`
Expected: FAIL.

- [ ] **Step 3: Implement `delete_paper_account`**

Replace the stub tool body:

```python
    @mcp.tool(
        name="delete_paper_account",
        description=(
            "Delete a paper trading account and all associated positions/trades "
            "(FK cascade). Irreversible. Account is looked up by unique name."
        ),
    )
    async def delete_paper_account(name: str) -> dict[str, Any]:
        async with _session_factory()() as db:
            service = PaperTradingService(db)
            account = await service.get_account_by_name(name)
            if account is None:
                return {
                    "success": False,
                    "error": f"Paper account '{name}' not found",
                }
            deleted = await service.delete_account(account.id)
            return {
                "success": True,
                "deleted": bool(deleted),
                "name": name,
                "id": account.id,
            }
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/test_paper_account_tools.py -v -k delete_paper_account`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/paper_account_registration.py tests/test_paper_account_tools.py
git commit -m "feat(paper): implement delete_paper_account MCP tool"
```

---

## Task 7: End-to-end flow test + full suite green

**Files:**
- Test: `tests/test_paper_account_tools.py`

A single test exercises the spec's "계좌 생성 → 조회 → 리셋 → 삭제" happy path against the service layer mocked at the `PaperTradingService` boundary.

- [ ] **Step 1: Add the flow test**

Append to `tests/test_paper_account_tools.py`:

```python
@pytest.mark.asyncio
async def test_paper_account_full_flow(monkeypatch) -> None:
    """create → list → reset → delete all succeed against a mocked service."""
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    created = _make_account(id=101, name="flow")
    after_reset = _make_account(id=101, name="flow")

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.create_account = AsyncMock(return_value=created)
        svc.list_accounts = AsyncMock(return_value=[created])
        svc.get_portfolio_summary = AsyncMock(
            return_value={
                "total_invested": Decimal("0"),
                "total_evaluated": Decimal("100000000"),
                "total_pnl": Decimal("0"),
                "total_pnl_pct": Decimal("0.00"),
                "cash_krw": created.cash_krw,
                "cash_usd": created.cash_usd,
                "positions_count": 0,
            }
        )
        svc.get_account_by_name = AsyncMock(return_value=created)
        svc.reset_account = AsyncMock(return_value=after_reset)
        svc.delete_account = AsyncMock(return_value=True)

        tools = build_tools()

        create_result = await tools["create_paper_account"](name="flow")
        assert create_result["success"] is True

        list_result = await tools["list_paper_accounts"](is_active=True)
        assert list_result["success"] is True
        assert list_result["accounts"][0]["id"] == 101

        reset_result = await tools["reset_paper_account"](name="flow")
        assert reset_result["success"] is True

        delete_result = await tools["delete_paper_account"](name="flow")
        assert delete_result == {
            "success": True,
            "deleted": True,
            "name": "flow",
            "id": 101,
        }
```

- [ ] **Step 2: Run the full test file**

Run: `uv run pytest tests/test_paper_account_tools.py -v`
Expected: PASS (all tests from tasks 1–7).

- [ ] **Step 3: Lint + typecheck**

Run: `make lint`
Expected: no new errors in `paper_account_registration.py` or the test file.

- [ ] **Step 4: Run the broader registration test to confirm no regressions**

Run: `uv run pytest tests/test_mcp_tool_registration.py tests/test_paper_trading_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_paper_account_tools.py
git commit -m "test(paper): add end-to-end paper account MCP flow test"
```

---

## Self-Review Notes

- **Spec coverage:**
  - `create_paper_account` (Task 3) ✓
  - `list_paper_accounts` (Task 4) ✓ — returns `id`, `name`, `initial_capital`, `cash_krw`, `cash_usd`, `positions_count`, `total_evaluated_krw`, `total_pnl_pct`, `strategy_name`, `created_at` as specified.
  - `reset_paper_account` (Task 5) ✓
  - `delete_paper_account` (Task 6) ✓
  - Registry wiring (Task 1) ✓
  - Duplicate-name error (Task 3 test `test_create_paper_account_duplicate_name`) ✓
  - Delete-missing error (Task 6 test `test_delete_paper_account_missing`) ✓
  - Full create → list → reset → delete flow (Task 7) ✓
- **Placeholders:** None. All code blocks are complete.
- **Type consistency:** `_serialize_account` signature (same kwargs: `positions_count`, `total_evaluated`, `total_pnl_pct`) is used identically in Task 4's `list_paper_accounts`. `PaperTradingService` method names match the service module (`create_account`, `list_accounts`, `get_account_by_name`, `reset_account`, `delete_account`, `get_portfolio_summary`).
- **Caveat surfaced:** `total_evaluated_krw` mixes USD + KRW raw values — documented in both the tool description and plan design notes so it doesn't silently mislead downstream agents.
