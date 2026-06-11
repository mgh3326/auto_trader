# ROB-517 Operating Briefing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only MCP tools for active watch discovery and one-call operating briefing so a new trading session can bootstrap from holdings, pending orders, active watches, latest report, and session context.

**Architecture:** Create a focused operating briefing MCP module rather than expanding the already-large investment report handler. Reuse existing read models: `InvestmentReportsRepository.list_active_alerts`, `portfolio_holdings._get_holdings_impl`, pending order snapshot collector, report query service, and ROB-516 `SessionContextService`. Extract pending-order snapshot metadata into a shared helper so both `investment_report_context_get` and the new briefing use one collector path.

**Tech Stack:** Python 3.13, FastMCP tool registration, SQLAlchemy async sessions, Pydantic v2 schemas, pytest/pytest-asyncio, Ruff, ty.

---

## Decisions Locked For This Plan

- `list_active_watches` defaults to actionable active rows only: `status='active'` and `valid_until > now_kst()`.
- `list_active_watches(include_expired_status_rows=True)` may include rows that are still `status='active'` even if `valid_until <= now`; these rows are marked in metadata so diagnostics can see scanner lag.
- `symbol` filter is exact after `strip().upper()` for US/crypto and `strip()` for KR. No fuzzy symbol matching in this slice.
- `get_operating_briefing` is read-only. It never submits, modifies, cancels, reconciles, activates, expires, or mutates orders/watches/session context.
- `get_operating_briefing` requires `market` and accepts optional `account_scope`; if omitted, defaults are `kr/us -> kis_live`, `crypto -> upbit_live`.
- Session context is first-class because ROB-516 is now merged into `main`.
- Pending-order `expected_expiry` is added only when it is factually derivable. For KR KIS day orders, derive from `placed_at` date at `20:00:00+09:00`; for US/crypto return `None` unless existing source data provides a reliable expiry.

## File Structure

- Create `app/mcp_server/tooling/pending_orders_snapshot.py`: shared fail-open collector wrapper returning orders plus `as_of`, `freshness_status`, and unavailable reason.
- Create `app/mcp_server/tooling/operating_briefing.py`: read-only tool implementations for `list_active_watches` and `get_operating_briefing`.
- Create `app/mcp_server/tooling/operating_briefing_registration.py`: FastMCP registration and tool name set.
- Modify `app/mcp_server/tooling/investment_reports_handlers.py`: use the shared pending-order helper without changing `investment_report_context_get` response shape.
- Modify `app/mcp_server/tooling/registry.py`: register operating briefing tools with the always-on read-only MCP surface.
- Modify `app/schemas/investment_reports.py`: add small response DTOs for active watches and operating briefing.
- Modify `app/services/investment_reports/repository.py`: extend `list_active_alerts` with `symbol`, `include_expired_status_rows`, and `limit`.
- Modify `app/services/action_report/snapshot_backed/collectors/pending_orders.py`: include `expected_expiry` in normalized pending orders where derivable.
- Modify `app/mcp_server/README.md`: document the two tools and response semantics.
- Test `tests/test_investment_reports_mcp.py`: direct handler tests for active watch listing and context compatibility.
- Test `tests/mcp_server/test_operating_briefing_tools.py`: tool registration and briefing composition tests.
- Test `tests/services/action_report/test_pending_orders_collector.py` or existing collector test file if present: `expected_expiry` normalization.

---

### Task 1: Add Repository Filtering For Active Watches

**Files:**
- Modify: `app/services/investment_reports/repository.py:326`
- Test: `tests/test_investment_reports_mcp.py`

- [ ] **Step 1: Write the failing active-watch filter test**

Add this test near the existing watch activation tests in `tests/test_investment_reports_mcp.py`:

```python
@pytest.mark.asyncio
async def test_list_active_watches_filters_market_symbol_and_expiry(
    session: AsyncSession,
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.core.db import AsyncSessionLocal
    from app.models.investment_reports import InvestmentWatchAlert
    from app.services.investment_reports.repository import InvestmentReportsRepository

    now = datetime(2026, 6, 11, 3, 0, tzinfo=UTC)
    active_future = InvestmentWatchAlert(
        idempotency_key="rob517:active:005930",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="above",
        threshold=100000,
        threshold_key="price:above:100000",
        intent="trend_recovery_review",
        action_mode="notify_only",
        rationale="keep active",
        trigger_checklist=[],
        max_action={},
        valid_until=now + timedelta(days=1),
        status="active",
    )
    active_expired = InvestmentWatchAlert(
        idempotency_key="rob517:expired:005930",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=90000,
        threshold_key="price:below:90000",
        intent="risk_review",
        action_mode="notify_only",
        rationale="scanner lag row",
        trigger_checklist=[],
        max_action={},
        valid_until=now - timedelta(minutes=1),
        status="active",
    )
    inactive = InvestmentWatchAlert(
        idempotency_key="rob517:triggered:005930",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="above",
        threshold=110000,
        threshold_key="price:above:110000",
        intent="trend_recovery_review",
        action_mode="notify_only",
        rationale="already triggered",
        trigger_checklist=[],
        max_action={},
        valid_until=now + timedelta(days=1),
        status="triggered",
    )

    async with AsyncSessionLocal() as db:
        db.add_all([active_future, active_expired, inactive])
        await db.commit()

    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        actionable = await repo.list_active_alerts(
            market="kr",
            symbol="005930",
            valid_at=now,
            include_expired_status_rows=False,
            limit=100,
        )
        diagnostic = await repo.list_active_alerts(
            market="kr",
            symbol="005930",
            valid_at=now,
            include_expired_status_rows=True,
            limit=100,
        )

    assert [a.idempotency_key for a in actionable] == ["rob517:active:005930"]
    assert {a.idempotency_key for a in diagnostic} == {
        "rob517:active:005930",
        "rob517:expired:005930",
    }
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py::test_list_active_watches_filters_market_symbol_and_expiry -v
```

Expected: FAIL because `list_active_alerts` does not accept `symbol`, `include_expired_status_rows`, or `limit`.

- [ ] **Step 3: Extend the repository method**

Change `app/services/investment_reports/repository.py`:

```python
    async def list_active_alerts(
        self,
        *,
        market: str | None = None,
        symbol: str | None = None,
        valid_at: datetime | None = None,
        include_expired_status_rows: bool = False,
        limit: int = 100,
    ) -> list[InvestmentWatchAlert]:
        capped_limit = max(1, min(int(limit), 250))
        stmt = sa.select(InvestmentWatchAlert).where(
            InvestmentWatchAlert.status == "active"
        )
        if market is not None:
            stmt = stmt.where(InvestmentWatchAlert.market == market)
        if symbol is not None:
            stmt = stmt.where(InvestmentWatchAlert.symbol == symbol)
        if valid_at is not None and not include_expired_status_rows:
            stmt = stmt.where(InvestmentWatchAlert.valid_until > valid_at)
        stmt = stmt.order_by(InvestmentWatchAlert.activated_at.desc()).limit(capped_limit)
        result = await self._session.scalars(stmt)
        return list(result.all())
```

- [ ] **Step 4: Run the test again**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py::test_list_active_watches_filters_market_symbol_and_expiry -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/repository.py tests/test_investment_reports_mcp.py
git commit -m "feat(ROB-517): filter active watch alerts"
```

---

### Task 2: Add `list_active_watches` MCP Tool

**Files:**
- Create: `app/mcp_server/tooling/operating_briefing.py`
- Create: `app/mcp_server/tooling/operating_briefing_registration.py`
- Modify: `app/mcp_server/tooling/registry.py:129`
- Modify: `app/schemas/investment_reports.py:1037`
- Test: `tests/mcp_server/test_operating_briefing_tools.py`

- [ ] **Step 1: Write registration and handler tests**

Create `tests/mcp_server/test_operating_briefing_tools.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.operating_briefing import list_active_watches_impl
from app.mcp_server.tooling.operating_briefing_registration import (
    OPERATING_BRIEFING_TOOL_NAMES,
    register_operating_briefing_tools,
)
from app.models.investment_reports import InvestmentWatchAlert


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *, name: str, description: str):
        assert description

        def decorator(fn):
            self.tools[name] = fn
            return fn

        return decorator


def test_operating_briefing_tool_names_register() -> None:
    mcp = FakeMCP()

    register_operating_briefing_tools(mcp)  # type: ignore[arg-type]

    assert "list_active_watches" in OPERATING_BRIEFING_TOOL_NAMES
    assert "get_operating_briefing" in OPERATING_BRIEFING_TOOL_NAMES
    assert set(mcp.tools) == OPERATING_BRIEFING_TOOL_NAMES


@pytest.mark.asyncio
async def test_list_active_watches_impl_returns_rationale_and_filters(
    db_session: AsyncSession,
) -> None:
    future = datetime.now(tz=UTC) + timedelta(days=1)
    db_session.add(
        InvestmentWatchAlert(
            idempotency_key="rob517:list-active",
            source_report_uuid=uuid.uuid4(),
            source_item_uuid=uuid.uuid4(),
            market="kr",
            target_kind="asset",
            symbol="005930",
            metric="price",
            operator="above",
            threshold=100000,
            threshold_key="price:above:100000",
            intent="trend_recovery_review",
            action_mode="notify_only",
            rationale="breakout watch",
            trigger_checklist=[{"check": "volume"}],
            max_action={},
            valid_until=future,
            status="active",
        )
    )
    await db_session.commit()

    result = await list_active_watches_impl(market="kr", symbol="005930")

    assert result["success"] is True
    assert result["count"] == 1
    assert result["filters"]["market"] == "kr"
    assert result["active_watches"][0]["symbol"] == "005930"
    assert result["active_watches"][0]["rationale"] == "breakout watch"
    assert result["active_watches"][0]["source_item_uuid"]
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py -v
```

Expected: FAIL because the module and registration do not exist.

- [ ] **Step 3: Add response DTOs**

Append after `PreviousReportContextResponse` in `app/schemas/investment_reports.py`:

```python
class ActiveWatchesListResponse(BaseModel):
    """``list_active_watches`` MCP return shape."""

    success: Literal[True] = True
    count: int
    as_of: datetime
    filters: dict[str, Any]
    active_watches: list[InvestmentWatchAlertResponse]


class OperatingBriefingResponse(BaseModel):
    """``get_operating_briefing`` MCP return shape."""

    success: Literal[True] = True
    market: MarketLiteral
    account_scope: AccountScopeLiteral
    as_of: datetime
    staleness: dict[str, Any]
    holdings: dict[str, Any]
    pending_orders: dict[str, Any]
    active_watches: dict[str, Any]
    latest_report: dict[str, Any] | None
    session_context: dict[str, Any]
```

- [ ] **Step 4: Implement `operating_briefing.py` with `list_active_watches_impl`**

Create `app/mcp_server/tooling/operating_briefing.py`:

```python
"""Read-only operating briefing MCP tools for ROB-517."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.schemas.investment_reports import (
    ActiveWatchesListResponse,
    InvestmentWatchAlertResponse,
)
from app.services.investment_reports.repository import InvestmentReportsRepository


def _normalize_watch_symbol(symbol: str | None, market: str | None) -> str | None:
    if symbol is None:
        return None
    stripped = str(symbol).strip()
    if not stripped:
        return None
    if market in {"us", "crypto"}:
        return stripped.upper()
    return stripped


async def list_active_watches_impl(
    market: str | None = None,
    symbol: str | None = None,
    include_expired_status_rows: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    as_of = now_kst()
    capped_limit = max(1, min(int(limit), 250))
    normalized_symbol = _normalize_watch_symbol(symbol, market)
    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        rows = await repo.list_active_alerts(
            market=market,
            symbol=normalized_symbol,
            valid_at=as_of,
            include_expired_status_rows=include_expired_status_rows,
            limit=capped_limit,
        )
        response = ActiveWatchesListResponse(
            count=len(rows),
            as_of=as_of,
            filters={
                "market": market,
                "symbol": normalized_symbol,
                "include_expired_status_rows": include_expired_status_rows,
                "limit": capped_limit,
            },
            active_watches=[
                InvestmentWatchAlertResponse.model_validate(row) for row in rows
            ],
        )
    return response.model_dump(mode="json", by_alias=True)


async def get_operating_briefing_impl(
    market: str,
    account_scope: str | None = None,
    session_context_limit: int = 10,
    include_current_price: bool = True,
) -> dict[str, Any]:
    raise NotImplementedError("Task 5 implements get_operating_briefing_impl")


__all__ = [
    "get_operating_briefing_impl",
    "list_active_watches_impl",
]
```

- [ ] **Step 5: Add registration**

Create `app/mcp_server/tooling/operating_briefing_registration.py`:

```python
"""MCP registration for ROB-517 operating briefing tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.operating_briefing import (
    get_operating_briefing_impl,
    list_active_watches_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


OPERATING_BRIEFING_TOOL_NAMES: set[str] = {
    "list_active_watches",
    "get_operating_briefing",
}


def register_operating_briefing_tools(mcp: FastMCP) -> None:
    mcp.tool(
        name="list_active_watches",
        description=(
            "Read-only: list actionable investment_watch_alerts rows with "
            "status='active'. Defaults to rows whose valid_until is still in "
            "the future; pass include_expired_status_rows=True for diagnostics."
        ),
    )(list_active_watches_impl)
    mcp.tool(
        name="get_operating_briefing",
        description=(
            "Read-only: one-call session bootstrap for current operating state. "
            "Returns holdings summary, pending orders, active watches, latest "
            "advisory report summary, recent session context, and per-section "
            "staleness metadata. No broker/order/watch/session mutation."
        ),
    )(get_operating_briefing_impl)


__all__ = [
    "OPERATING_BRIEFING_TOOL_NAMES",
    "register_operating_briefing_tools",
]
```

- [ ] **Step 6: Register in the MCP registry**

Modify `app/mcp_server/tooling/registry.py`:

```python
from app.mcp_server.tooling.operating_briefing_registration import (
    register_operating_briefing_tools,
)
```

Add after `register_session_context_tools(mcp)`:

```python
    register_operating_briefing_tools(mcp)
```

- [ ] **Step 7: Run tests**

Run:

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/schemas/investment_reports.py app/mcp_server/tooling/operating_briefing.py app/mcp_server/tooling/operating_briefing_registration.py app/mcp_server/tooling/registry.py tests/mcp_server/test_operating_briefing_tools.py
git commit -m "feat(ROB-517): add active watch MCP listing"
```

---

### Task 3: Extract Pending Orders Snapshot Metadata

**Files:**
- Create: `app/mcp_server/tooling/pending_orders_snapshot.py`
- Modify: `app/mcp_server/tooling/investment_reports_handlers.py:275`
- Test: `tests/test_investment_reports_mcp.py`

- [ ] **Step 1: Add context compatibility test**

Add to the existing pending-orders test section in `tests/test_investment_reports_mcp.py`:

```python
@pytest.mark.asyncio
async def test_context_get_pending_orders_shape_unchanged_after_shared_helper(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    import datetime as dt
    from unittest.mock import AsyncMock

    from app.services.investment_snapshots.collectors import (
        SnapshotCollectorRegistry,
        SnapshotCollectResult,
    )

    fake_orders = [{"symbol": "005930", "market": "kr", "expected_expiry": None}]
    fake_result = SnapshotCollectResult(
        snapshot_kind="pending_orders",
        market="kr",
        account_scope="kis_live",
        source_kind="auto_trader_mcp",
        payload_json={"pending_orders": fake_orders, "count": 1},
        as_of=dt.datetime.now(tz=dt.UTC),
        freshness_status="fresh",
    )
    fake_collector = AsyncMock()
    fake_collector.snapshot_kind = "pending_orders"
    fake_collector.collect = AsyncMock(return_value=[fake_result])

    def _fake_registry(_db: object) -> SnapshotCollectorRegistry:
        reg = SnapshotCollectorRegistry()
        reg.register(fake_collector)
        return reg

    monkeypatch.setattr(
        "app.services.action_report.snapshot_backed.collectors.registry."
        "production_collector_registry",
        _fake_registry,
    )

    await investment_report_create_impl(
        items=[_action_item_dict()],
        **_create_kwargs(market="kr", kst_date="2026-06-11"),
    )

    ctx = await investment_report_context_get_impl(
        market="kr",
        account_scope="kis_live",
    )

    assert ctx["success"] is True
    assert ctx["pending_orders"] == fake_orders
```

- [ ] **Step 2: Run the compatibility test**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py::test_context_get_pending_orders_shape_unchanged_after_shared_helper -v
```

Expected: PASS before extraction, then keep it passing after extraction.

- [ ] **Step 3: Create the shared helper**

Create `app/mcp_server/tooling/pending_orders_snapshot.py`:

```python
"""Shared read-only pending order snapshot collection for MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE: dict[str, str] = {
    "kr": "kis_live",
    "us": "kis_live",
    "crypto": "upbit_live",
}


@dataclass(frozen=True)
class PendingOrdersSnapshot:
    orders: list[dict[str, Any]] | None
    as_of: str | None
    freshness_status: str | None
    unavailable_reason: str | None
    account_scope: str | None


async def collect_pending_orders_snapshot(
    db: Any,
    *,
    market: str,
    account_scope: str | None,
) -> PendingOrdersSnapshot:
    from app.services.action_report.snapshot_backed.collectors.registry import (
        production_collector_registry,
    )
    from app.services.investment_snapshots.collectors import CollectorRequest

    effective_scope = account_scope or DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE.get(market)
    if effective_scope is None:
        return PendingOrdersSnapshot(
            orders=None,
            as_of=None,
            freshness_status=None,
            unavailable_reason="unsupported_market",
            account_scope=None,
        )

    try:
        registry = production_collector_registry(db)
    except Exception as exc:  # noqa: BLE001
        return PendingOrdersSnapshot(
            orders=None,
            as_of=None,
            freshness_status=None,
            unavailable_reason=f"collector_registry_failed:{type(exc).__name__}:{exc}",
            account_scope=effective_scope,
        )
    collector = registry.get("pending_orders")
    if collector is None:
        return PendingOrdersSnapshot(
            orders=None,
            as_of=None,
            freshness_status=None,
            unavailable_reason="collector_missing",
            account_scope=effective_scope,
        )

    try:
        results = await collector.collect(
            CollectorRequest(
                market=market,  # type: ignore[arg-type]
                account_scope=effective_scope,  # type: ignore[arg-type]
                policy_snapshot={},
            )
        )
    except Exception as exc:  # noqa: BLE001
        return PendingOrdersSnapshot(
            orders=None,
            as_of=None,
            freshness_status=None,
            unavailable_reason=f"collector_failed:{type(exc).__name__}:{exc}",
            account_scope=effective_scope,
        )
    if not results:
        return PendingOrdersSnapshot(
            orders=None,
            as_of=None,
            freshness_status=None,
            unavailable_reason="collector_returned_no_results",
            account_scope=effective_scope,
        )

    result = results[0]
    as_of = result.as_of.isoformat() if result.as_of is not None else None
    freshness = result.freshness_status
    errors = result.errors_json or {}
    if freshness in ("unavailable", "hard_stale"):
        return PendingOrdersSnapshot(
            orders=None,
            as_of=as_of,
            freshness_status=freshness,
            unavailable_reason=str(errors.get("reason") or freshness),
            account_scope=effective_scope,
        )
    payload = result.payload_json or {}
    orders = payload.get("pending_orders")
    return PendingOrdersSnapshot(
        orders=list(orders) if orders is not None else [],
        as_of=as_of,
        freshness_status=freshness,
        unavailable_reason=None,
        account_scope=effective_scope,
    )
```

- [ ] **Step 4: Update `investment_reports_handlers.py` compatibility wrapper**

Replace `_DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE` and `_collect_pending_orders_snapshot` with:

```python
async def _collect_pending_orders_snapshot(
    db: Any,
    *,
    market: str,
    account_scope: str | None,
) -> list[dict[str, Any]] | None:
    from app.mcp_server.tooling.pending_orders_snapshot import (
        collect_pending_orders_snapshot,
    )

    snapshot = await collect_pending_orders_snapshot(
        db,
        market=market,
        account_scope=account_scope,
    )
    return snapshot.orders
```

- [ ] **Step 5: Run existing pending-orders context tests**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py::test_context_get_includes_pending_orders_when_collector_succeeds tests/test_investment_reports_mcp.py::test_context_get_surfaces_pending_orders_unavailable_as_null tests/test_investment_reports_mcp.py::test_context_get_pending_orders_shape_unchanged_after_shared_helper -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/pending_orders_snapshot.py app/mcp_server/tooling/investment_reports_handlers.py tests/test_investment_reports_mcp.py
git commit -m "refactor(ROB-517): share pending order snapshot collection"
```

---

### Task 4: Add `expected_expiry` To Pending Order Normalization

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/pending_orders.py`
- Test: `tests/services/action_report/test_pending_orders_collector.py` or create `tests/services/action_report/test_pending_orders_snapshot_collector.py`

- [ ] **Step 1: Locate existing collector tests**

Run:

```bash
rg -n "PendingOrdersSnapshotCollector|_normalize_kis_order|pending_orders" tests/services tests/mcp_server tests
```

Expected: find an existing collector test file if present. If no focused collector test exists, create `tests/services/action_report/test_pending_orders_snapshot_collector.py`.

- [ ] **Step 2: Write the failing normalizer test**

Add this test to the focused collector test file:

```python
from __future__ import annotations

from app.services.action_report.snapshot_backed.collectors.pending_orders import (
    _normalize_kis_order,
)


def test_normalize_kis_kr_order_adds_expected_day_expiry_from_placed_at() -> None:
    row = {
        "ord_no": "0011001100",
        "pdno": "005930",
        "sll_buy_dvsn_cd": "02",
        "ord_unpr": "70000",
        "ord_qty": "3",
        "nccs_qty": "3",
        "ord_dt": "20260611",
        "ord_tmd": "093000",
    }

    out = _normalize_kis_order(row, market="kr")

    assert out["placed_at"] == "2026-06-11T09:30:00+09:00"
    assert out["expected_expiry"] == "2026-06-11T20:00:00+09:00"


def test_normalize_kis_us_order_keeps_expected_expiry_unknown() -> None:
    row = {
        "odno": "US-1",
        "pdno": "AAPL",
        "sll_buy_dvsn_cd": "02",
        "ord_unpr": "200",
        "ord_qty": "1",
        "nccs_qty": "1",
        "ord_dt": "20260611",
        "ord_tmd": "230000",
    }

    out = _normalize_kis_order(row, market="us")

    assert out["expected_expiry"] is None
```

- [ ] **Step 3: Run the failing test**

Run the exact file discovered or created:

```bash
uv run pytest tests/services/action_report/test_pending_orders_snapshot_collector.py -v
```

Expected: FAIL because `expected_expiry` is absent.

- [ ] **Step 4: Implement expiry helper**

In `app/services/action_report/snapshot_backed/collectors/pending_orders.py`, add near `_kis_placed_at`:

```python
def _kis_expected_expiry(placed_at: dt.datetime | None, *, market: str) -> str | None:
    if market != "kr" or placed_at is None:
        return None
    local = placed_at.astimezone(_KST)
    expiry = local.replace(hour=20, minute=0, second=0, microsecond=0)
    return expiry.isoformat()
```

Update `_normalize_kis_order` return dict:

```python
        "expected_expiry": _kis_expected_expiry(placed_at, market=market),
```

- [ ] **Step 5: Run collector tests**

Run:

```bash
uv run pytest tests/services/action_report/test_pending_orders_snapshot_collector.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/pending_orders.py tests/services/action_report/test_pending_orders_snapshot_collector.py
git commit -m "feat(ROB-517): expose pending order expected expiry"
```

---

### Task 5: Implement `get_operating_briefing`

**Files:**
- Modify: `app/mcp_server/tooling/operating_briefing.py`
- Modify: `app/schemas/investment_reports.py`
- Test: `tests/mcp_server/test_operating_briefing_tools.py`

- [ ] **Step 1: Add composition test with monkeypatched providers**

Append to `tests/mcp_server/test_operating_briefing_tools.py`:

```python
@pytest.mark.asyncio
async def test_get_operating_briefing_composes_all_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from app.mcp_server.tooling import operating_briefing as ob

    async def fake_holdings(**kwargs):
        assert kwargs["market"] == "kr"
        assert kwargs["include_current_price"] is True
        return {
            "total_positions": 2,
            "summary": {"total_value": 1234567},
            "accounts": [
                {
                    "account": "kis",
                    "positions": [
                        {
                            "symbol": "005930",
                            "profit_rate": 3.2,
                            "profit_loss": 1000,
                            "evaluation_amount": 100000,
                        },
                        {
                            "symbol": "000660",
                            "profit_rate": -1.5,
                            "profit_loss": -500,
                            "evaluation_amount": 50000,
                        },
                    ],
                }
            ],
            "errors": [],
        }

    class FakePendingSnapshot:
        orders = [{"symbol": "005930", "expected_expiry": "2026-06-11T20:00:00+09:00"}]
        as_of = "2026-06-11T01:00:00+00:00"
        freshness_status = "fresh"
        unavailable_reason = None
        account_scope = "kis_live"

    async def fake_pending(db, *, market, account_scope):
        assert market == "kr"
        assert account_scope == "kis_live"
        return FakePendingSnapshot()

    async def fake_active_watches(**kwargs):
        return {
            "success": True,
            "count": 1,
            "as_of": datetime.now(tz=UTC).isoformat(),
            "filters": kwargs,
            "active_watches": [{"symbol": "005930", "rationale": "watch"}],
        }

    async def fake_latest_report(db, *, market, account_scope):
        return {
            "report_uuid": "11111111-1111-1111-1111-111111111111",
            "title": "latest plan",
            "status": "draft",
            "created_at": "2026-06-11T00:00:00+00:00",
            "items": {
                "total": 2,
                "by_status": {"approved": 1, "deferred": 1},
                "top": [{"symbol": "005930", "status": "approved"}],
            },
        }

    async def fake_session_context(db, *, market, account_scope, limit):
        return {
            "count": 1,
            "entries": [{"title": "handoff", "entry_type": "next_action"}],
        }

    monkeypatch.setattr(ob, "_get_holdings_impl", fake_holdings)
    monkeypatch.setattr(ob, "collect_pending_orders_snapshot", fake_pending)
    monkeypatch.setattr(ob, "list_active_watches_impl", fake_active_watches)
    monkeypatch.setattr(ob, "_latest_report_summary", fake_latest_report)
    monkeypatch.setattr(ob, "_recent_session_context", fake_session_context)

    result = await ob.get_operating_briefing_impl(
        market="kr",
        account_scope="kis_live",
    )

    assert result["success"] is True
    assert result["market"] == "kr"
    assert result["account_scope"] == "kis_live"
    assert result["holdings"]["summary"]["total_value"] == 1234567
    assert result["holdings"]["top_movers"][0]["symbol"] == "005930"
    assert result["pending_orders"]["orders"][0]["expected_expiry"].endswith("+09:00")
    assert result["active_watches"]["count"] == 1
    assert result["latest_report"]["title"] == "latest plan"
    assert result["session_context"]["entries"][0]["title"] == "handoff"
    assert result["staleness"]["pending_orders"]["freshness_status"] == "fresh"
```

- [ ] **Step 2: Run failing test**

Run:

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py::test_get_operating_briefing_composes_all_sections -v
```

Expected: FAIL because `get_operating_briefing_impl` raises `NotImplementedError`.

- [ ] **Step 3: Implement account-scope defaults and holdings routing**

In `app/mcp_server/tooling/operating_briefing.py`, add imports:

```python
from app.mcp_server.tooling.pending_orders_snapshot import (
    DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE,
    collect_pending_orders_snapshot,
)
from app.mcp_server.tooling.portfolio_holdings import _get_holdings_impl
from app.services.investment_reports.query_service import InvestmentReportQueryService
from app.services.session_context import SessionContextService
from app.schemas.session_context import SessionContextResponse
```

Add helpers:

```python
def _default_account_scope(market: str, account_scope: str | None) -> str:
    if account_scope:
        return account_scope
    default = DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE.get(market)
    if default is None:
        raise ValueError(f"unsupported market for operating briefing: {market}")
    return default


def _holdings_kwargs(market: str, account_scope: str, include_current_price: bool) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "market": market,
        "include_current_price": include_current_price,
        "routing_account_mode": account_scope,
    }
    if account_scope == "kis_mock":
        kwargs["is_mock"] = True
    if account_scope == "upbit_live":
        kwargs["account"] = "upbit"
    if account_scope == "alpaca_paper":
        kwargs["account"] = "paper"
    return kwargs
```

- [ ] **Step 4: Implement holdings top movers**

Add helper:

```python
def _flatten_positions(holdings: dict[str, Any]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for account in holdings.get("accounts") or []:
        account_name = account.get("account")
        for position in account.get("positions") or []:
            row = dict(position)
            row.setdefault("account", account_name)
            positions.append(row)
    return positions


def _top_movers(holdings: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    candidates = []
    for position in _flatten_positions(holdings):
        profit_rate = position.get("profit_rate")
        if profit_rate is None:
            continue
        try:
            abs_rate = abs(float(profit_rate))
        except (TypeError, ValueError):
            continue
        candidates.append((abs_rate, position))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "symbol": row.get("symbol"),
            "account": row.get("account"),
            "profit_rate": row.get("profit_rate"),
            "profit_loss": row.get("profit_loss"),
            "evaluation_amount": row.get("evaluation_amount"),
        }
        for _, row in candidates[:limit]
    ]
```

- [ ] **Step 5: Implement latest report summary**

Add helper:

```python
async def _latest_report_summary(
    db: Any,
    *,
    market: str,
    account_scope: str,
) -> dict[str, Any] | None:
    service = InvestmentReportQueryService(db)
    report = await service._repo.latest_report(  # existing repo access inside service
        market=market,
        account_scope=account_scope,
    )
    if report is None:
        return None
    bundle = await service.get_bundle(report.report_uuid)
    items = bundle["items"] if bundle is not None else []
    by_status: dict[str, int] = {}
    top_items: list[dict[str, Any]] = []
    for item in items:
        status = str(getattr(item, "status", "unknown"))
        by_status[status] = by_status.get(status, 0) + 1
        if len(top_items) < 5:
            top_items.append(
                {
                    "item_uuid": str(item.item_uuid),
                    "symbol": item.symbol,
                    "item_kind": item.item_kind,
                    "intent": item.intent,
                    "status": item.status,
                    "rationale": item.rationale,
                }
            )
    return {
        "report_uuid": str(report.report_uuid),
        "title": report.title,
        "status": report.status,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "items": {
            "total": len(items),
            "by_status": by_status,
            "top": top_items,
        },
    }
```

- [ ] **Step 6: Implement session context helper**

Add helper:

```python
async def _recent_session_context(
    db: Any,
    *,
    market: str,
    account_scope: str,
    limit: int,
) -> dict[str, Any]:
    service = SessionContextService(db)
    rows = await service.get_recent(
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        limit=max(1, min(int(limit), 100)),
    )
    return {
        "count": len(rows),
        "entries": [
            SessionContextResponse.model_validate(row).model_dump(mode="json")
            for row in rows
        ],
    }
```

- [ ] **Step 7: Implement the briefing body**

Replace the `NotImplementedError` body:

```python
async def get_operating_briefing_impl(
    market: str,
    account_scope: str | None = None,
    session_context_limit: int = 10,
    include_current_price: bool = True,
) -> dict[str, Any]:
    as_of = now_kst()
    effective_scope = _default_account_scope(market, account_scope)
    holdings = await _get_holdings_impl(
        **_holdings_kwargs(market, effective_scope, include_current_price)
    )
    async with AsyncSessionLocal() as db:
        pending = await collect_pending_orders_snapshot(
            db,
            market=market,
            account_scope=effective_scope,
        )
        latest_report = await _latest_report_summary(
            db,
            market=market,
            account_scope=effective_scope,
        )
        session_context = await _recent_session_context(
            db,
            market=market,
            account_scope=effective_scope,
            limit=session_context_limit,
        )
    active_watches = await list_active_watches_impl(market=market)
    response = {
        "success": True,
        "market": market,
        "account_scope": effective_scope,
        "as_of": as_of.isoformat(),
        "staleness": {
            "holdings": {
                "as_of": as_of.isoformat(),
                "freshness_status": "live_or_best_effort",
                "errors": holdings.get("errors") or [],
            },
            "pending_orders": {
                "as_of": pending.as_of,
                "freshness_status": pending.freshness_status,
                "unavailable_reason": pending.unavailable_reason,
            },
            "active_watches": {
                "as_of": active_watches.get("as_of"),
                "freshness_status": "db_read",
            },
            "latest_report": {
                "freshness_status": "db_read" if latest_report else "not_found",
            },
            "session_context": {
                "freshness_status": "db_read",
            },
        },
        "holdings": {
            "filters": holdings.get("filters"),
            "total_accounts": holdings.get("total_accounts"),
            "total_positions": holdings.get("total_positions"),
            "summary": holdings.get("summary"),
            "top_movers": _top_movers(holdings),
            "errors": holdings.get("errors") or [],
        },
        "pending_orders": {
            "count": len(pending.orders or []),
            "orders": pending.orders,
            "unavailable_reason": pending.unavailable_reason,
        },
        "active_watches": {
            "count": active_watches.get("count", 0),
            "watches": active_watches.get("active_watches", []),
        },
        "latest_report": latest_report,
        "session_context": session_context,
    }
    return response
```

- [ ] **Step 8: Run briefing tests**

Run:

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add app/mcp_server/tooling/operating_briefing.py app/schemas/investment_reports.py tests/mcp_server/test_operating_briefing_tools.py
git commit -m "feat(ROB-517): add operating briefing MCP tool"
```

---

### Task 6: Add End-To-End Direct Handler Test

**Files:**
- Test: `tests/mcp_server/test_operating_briefing_tools.py`

- [ ] **Step 1: Add direct DB-backed integration-style test**

Append:

```python
@pytest.mark.asyncio
async def test_get_operating_briefing_reads_active_watch_and_session_context(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.mcp_server.tooling import operating_briefing as ob
    from app.mcp_server.tooling.session_context_tools import session_context_append

    async def fake_holdings(**kwargs):
        return {
            "filters": {"market": kwargs["market"]},
            "total_accounts": 1,
            "total_positions": 0,
            "summary": {},
            "accounts": [],
            "errors": [],
        }

    class EmptyPendingSnapshot:
        orders: list[dict] = []
        as_of = "2026-06-11T01:00:00+00:00"
        freshness_status = "fresh"
        unavailable_reason = None
        account_scope = "kis_live"

    async def fake_pending(db, *, market, account_scope):
        return EmptyPendingSnapshot()

    monkeypatch.setattr(ob, "_get_holdings_impl", fake_holdings)
    monkeypatch.setattr(ob, "collect_pending_orders_snapshot", fake_pending)

    db_session.add(
        InvestmentWatchAlert(
            idempotency_key="rob517:briefing-active",
            source_report_uuid=uuid.uuid4(),
            source_item_uuid=uuid.uuid4(),
            market="kr",
            target_kind="asset",
            symbol="005930",
            metric="price",
            operator="above",
            threshold=100000,
            threshold_key="price:above:100000",
            intent="trend_recovery_review",
            action_mode="notify_only",
            rationale="briefing watch",
            trigger_checklist=[],
            max_action={},
            valid_until=datetime.now(tz=UTC) + timedelta(days=1),
            status="active",
        )
    )
    await db_session.commit()
    await session_context_append(
        entries=[
            {
                "kst_date": "2026-06-11",
                "market": "kr",
                "account_scope": "kis_live",
                "entry_type": "next_action",
                "title": "재평가",
                "body": "내일 20:00 만료 주문 재평가",
            }
        ]
    )

    result = await ob.get_operating_briefing_impl(
        market="kr",
        account_scope="kis_live",
    )

    assert result["success"] is True
    assert result["active_watches"]["count"] == 1
    assert result["active_watches"]["watches"][0]["rationale"] == "briefing watch"
    assert result["session_context"]["count"] == 1
    assert result["session_context"]["entries"][0]["title"] == "재평가"
```

- [ ] **Step 2: Run the test**

Run:

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py::test_get_operating_briefing_reads_active_watch_and_session_context -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/mcp_server/test_operating_briefing_tools.py
git commit -m "test(ROB-517): cover briefing DB handoff reads"
```

---

### Task 7: Update MCP README

**Files:**
- Modify: `app/mcp_server/README.md`
- Test: none

- [ ] **Step 1: Add documentation section**

Add near the investment report / watch alert documentation:

```markdown
### `list_active_watches`

Read-only active watch discovery for `review.investment_watch_alerts`.

Parameters:
- `market`: optional `kr`, `us`, or `crypto`.
- `symbol`: optional exact symbol filter.
- `include_expired_status_rows`: default `false`. When `false`, only returns `status='active'` rows whose `valid_until` is still in the future. When `true`, includes rows that remain `status='active'` even if `valid_until` has passed, for scanner-lag diagnostics.
- `limit`: default `100`, clamped to `1..250`.

Response includes `active_watches[]` with `symbol`, `operator`, `threshold`, `valid_until`, `rationale`, `source_report_uuid`, and `source_item_uuid`.

### `get_operating_briefing`

Read-only one-call bootstrap for a new operating session.

Parameters:
- `market`: required `kr`, `us`, or `crypto`.
- `account_scope`: optional. Defaults are `kr/us -> kis_live`, `crypto -> upbit_live`.
- `session_context_limit`: default `10`, clamped by the session context service.
- `include_current_price`: default `true`.

Response sections:
- `holdings`: summary and top movers derived from `get_holdings`.
- `pending_orders`: pending-order snapshot with `expected_expiry` when factually derivable.
- `active_watches`: same active watch rows as `list_active_watches`.
- `latest_report`: latest report summary and item status counts, or `null`.
- `session_context`: recent ROB-516 handoff entries.
- `staleness`: per-section `as_of`, freshness, and unavailable reason where available.

The tool never submits, modifies, cancels, reconciles, activates, expires, or mutates orders/watches/session context.
```

- [ ] **Step 2: Commit**

```bash
git add app/mcp_server/README.md
git commit -m "docs(ROB-517): document operating briefing tools"
```

---

### Task 8: Run Focused Verification

**Files:**
- No code edits.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/mcp_server/test_operating_briefing_tools.py tests/test_investment_reports_mcp.py::test_list_active_watches_filters_market_symbol_and_expiry tests/test_investment_reports_mcp.py::test_context_get_includes_pending_orders_when_collector_succeeds tests/test_investment_reports_mcp.py::test_context_get_surfaces_pending_orders_unavailable_as_null tests/test_investment_reports_mcp.py::test_context_get_pending_orders_shape_unchanged_after_shared_helper -v
```

Expected: PASS.

- [ ] **Step 2: Run pending-order collector tests**

Run the collector test path used in Task 4:

```bash
uv run pytest tests/services/action_report/test_pending_orders_snapshot_collector.py -v
```

Expected: PASS.

- [ ] **Step 3: Run type check on touched modules**

Run:

```bash
uv run ty check app/mcp_server/tooling/operating_briefing.py app/mcp_server/tooling/operating_briefing_registration.py app/mcp_server/tooling/pending_orders_snapshot.py app/services/investment_reports/repository.py app/services/action_report/snapshot_backed/collectors/pending_orders.py app/schemas/investment_reports.py
```

Expected: PASS.

- [ ] **Step 4: Run lint on touched modules**

Run:

```bash
uv run ruff check app/mcp_server/tooling/operating_briefing.py app/mcp_server/tooling/operating_briefing_registration.py app/mcp_server/tooling/pending_orders_snapshot.py app/services/investment_reports/repository.py app/services/action_report/snapshot_backed/collectors/pending_orders.py app/schemas/investment_reports.py tests/mcp_server/test_operating_briefing_tools.py tests/test_investment_reports_mcp.py
```

Expected: PASS.

- [ ] **Step 5: Commit final verification note only if files changed**

No commit is needed if all changes are already committed in prior tasks.

---

### Task 9: Linear Status And Risk Labels

**Files:**
- No code edits.

- [ ] **Step 1: Add Linear implementation comment**

Post a ROB-517 comment with this shape:

```text
Implementation ready for ROB-517.

Scope:
- Added read-only list_active_watches.
- Added read-only get_operating_briefing one-call bootstrap.
- Included holdings summary, pending_orders with expected_expiry where derivable, active watches, latest report summary, and ROB-516 session context.

Verification:
- [paste focused pytest result]
- [paste ty result]
- [paste ruff result]

Risk:
- Read-only MCP surface, no broker/order/watch/session mutation.
- Depends on ROB-516 session context already merged to main.
```

- [ ] **Step 2: Apply labels if needed**

Use `candidate_for_sonnet` if the diff is larger than expected or touches shared briefing/query contracts. Do not apply `high_risk_change` unless implementation adds a DB migration, auth/permission changes, live order approval boundary changes, or deployment automation.

---

## Self-Review Notes

- Spec coverage: plan covers both ROB-517 requested tools, pending order expected expiry, ROB-516 session context consumption, docs, and tests.
- Scope control: no new DB tables, no broker mutation, no live order execution, no frontend.
- Ambiguity resolved: active watches default to not-expired actionable rows; diagnostic inclusion is opt-in.
- Remaining operator checkpoint: if the operator wants `list_active_watches` to include expired `status='active'` rows by default, flip the default before Task 2.
