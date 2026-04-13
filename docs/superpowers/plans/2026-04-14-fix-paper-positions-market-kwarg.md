# Fix Paper Positions `market` Kwarg Mismatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix runtime `TypeError` when MCP portfolio tools query paper accounts with a market filter, and add tests that prevent signature drift between fake and real services.

**Architecture:** Add optional `market` parameter to `PaperTradingService.get_positions()` with SQL-level filtering. Update fake services in two test files to mirror the real signature and apply the filter. Add an integration test exercising the real service against an in-memory SQLite DB.

**Tech Stack:** Python, SQLAlchemy (async), pytest, pytest-asyncio

---

### File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `app/services/paper_trading_service.py:436-437` | Add `market` param + SQL filter |
| Modify | `tests/test_paper_portfolio_handler.py:168-169` | Align `_FakePaperService` signature + filtering |
| Modify | `tests/test_mcp_portfolio_tools.py:2985-2986` | Align `_StubPaperService` signature + filtering |
| Create | `tests/integration/test_paper_positions_market_filter.py` | Integration test with real service + DB |

---

### Task 1: Add `market` parameter to `PaperTradingService.get_positions()`

**Files:**
- Modify: `app/services/paper_trading_service.py:436-437`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_paper_positions_market_filter.py`:

```python
"""Integration test: PaperTradingService.get_positions() market filtering."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.paper_trading import PaperAccount, PaperPosition
from app.models.trading import InstrumentType
from app.services.paper_trading_service import PaperTradingService


@pytest.fixture
async def async_db():
    """In-memory SQLite async session with schema translation.

    Paper trading models use ``schema="paper"`` (PostgreSQL). SQLite has no
    schema support, so we translate ``"paper" -> None`` via
    ``schema_translate_map`` at both DDL and DML time.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        echo=False,
        execution_options={"schema_translate_map": {"paper": None}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                PaperAccount.__table__,
                PaperPosition.__table__,
            ],
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _seed_positions(db: AsyncSession) -> int:
    """Insert one account with three positions (kr, us, crypto). Return account id."""
    account = PaperAccount(
        name="test",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
    )
    db.add(account)
    await db.flush()

    for symbol, itype in [
        ("005930", InstrumentType.equity_kr),
        ("AAPL", InstrumentType.equity_us),
        ("KRW-BTC", InstrumentType.crypto),
    ]:
        db.add(
            PaperPosition(
                account_id=account.id,
                symbol=symbol,
                instrument_type=itype,
                quantity=Decimal("1"),
                avg_price=Decimal("100"),
                total_invested=Decimal("100"),
            )
        )
    await db.flush()
    return account.id


@pytest.mark.asyncio
async def test_get_positions_no_market_returns_all(async_db: AsyncSession):
    account_id = await _seed_positions(async_db)
    service = PaperTradingService(async_db)

    with patch.object(service, "_fetch_current_price", new_callable=AsyncMock) as mock_price:
        mock_price.side_effect = Exception("skip price")
        positions = await service.get_positions(account_id)

    assert len(positions) == 3


@pytest.mark.asyncio
async def test_get_positions_market_equity_kr(async_db: AsyncSession):
    account_id = await _seed_positions(async_db)
    service = PaperTradingService(async_db)

    with patch.object(service, "_fetch_current_price", new_callable=AsyncMock) as mock_price:
        mock_price.side_effect = Exception("skip price")
        positions = await service.get_positions(account_id, market="equity_kr")

    assert len(positions) == 1
    assert positions[0]["symbol"] == "005930"
    assert positions[0]["instrument_type"] == "equity_kr"


@pytest.mark.asyncio
async def test_get_positions_market_equity_us(async_db: AsyncSession):
    account_id = await _seed_positions(async_db)
    service = PaperTradingService(async_db)

    with patch.object(service, "_fetch_current_price", new_callable=AsyncMock) as mock_price:
        mock_price.side_effect = Exception("skip price")
        positions = await service.get_positions(account_id, market="equity_us")

    assert len(positions) == 1
    assert positions[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_get_positions_market_crypto(async_db: AsyncSession):
    account_id = await _seed_positions(async_db)
    service = PaperTradingService(async_db)

    with patch.object(service, "_fetch_current_price", new_callable=AsyncMock) as mock_price:
        mock_price.side_effect = Exception("skip price")
        positions = await service.get_positions(account_id, market="crypto")

    assert len(positions) == 1
    assert positions[0]["symbol"] == "KRW-BTC"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_paper_positions_market_filter.py -v`

Expected: `test_get_positions_no_market_returns_all` passes, the other three fail with `TypeError: PaperTradingService.get_positions() got an unexpected keyword argument 'market'`.

- [ ] **Step 3: Implement the `market` parameter**

In `app/services/paper_trading_service.py`, change lines 436-437 from:

```python
    async def get_positions(self, account_id: int) -> list[dict[str, Any]]:
        stmt = select(PaperPosition).where(PaperPosition.account_id == account_id)
```

to:

```python
    async def get_positions(
        self, account_id: int, *, market: str | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(PaperPosition).where(PaperPosition.account_id == account_id)
        if market is not None:
            stmt = stmt.where(PaperPosition.instrument_type == market)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_paper_positions_market_filter.py -v`

Expected: All 4 tests PASS.

- [ ] **Step 5: Run existing tests to check for regressions**

Run: `uv run pytest tests/test_paper_portfolio_handler.py tests/test_mcp_portfolio_tools.py -v`

Expected: All existing tests still PASS (internal callers don't pass `market`, and the handler already passes it as a keyword argument which now matches the signature).

- [ ] **Step 6: Commit**

```bash
git add app/services/paper_trading_service.py tests/integration/test_paper_positions_market_filter.py
git commit -m "fix(paper): add market param to PaperTradingService.get_positions (#501)"
```

---

### Task 2: Align fake services in test files

**Files:**
- Modify: `tests/test_paper_portfolio_handler.py:168-169`
- Modify: `tests/test_mcp_portfolio_tools.py:2985-2986`

- [ ] **Step 1: Update `_FakePaperService` in `test_paper_portfolio_handler.py`**

Change lines 168-169 from:

```python
    async def get_positions(self, account_id, market=None):
        return list(self._positions.get(account_id, []))
```

to:

```python
    async def get_positions(self, account_id, *, market=None):
        positions = list(self._positions.get(account_id, []))
        if market is not None:
            positions = [p for p in positions if p.get("instrument_type") == market]
        return positions
```

- [ ] **Step 2: Update `_StubPaperService` in `test_mcp_portfolio_tools.py`**

Change lines 2985-2986 from:

```python
    async def get_positions(self, account_id, market=None):
        return self._p.get(account_id, [])
```

to:

```python
    async def get_positions(self, account_id, *, market=None):
        positions = self._p.get(account_id, [])
        if market is not None:
            positions = [p for p in positions if p.get("instrument_type") == market]
        return positions
```

- [ ] **Step 3: Run all paper-related tests**

Run: `uv run pytest tests/test_paper_portfolio_handler.py tests/test_mcp_portfolio_tools.py tests/integration/test_paper_positions_market_filter.py -v`

Expected: All tests PASS. The market filter test in `test_paper_portfolio_handler.py` (`test_collect_paper_positions_applies_market_filter`) now validates that the fake service itself filters correctly, not just the handler's defensive post-filter.

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest tests/ -v -m "not slow"`

Expected: No regressions.

- [ ] **Step 5: Commit**

```bash
git add tests/test_paper_portfolio_handler.py tests/test_mcp_portfolio_tools.py
git commit -m "test(paper): align fake services with real PaperTradingService signature (#501)"
```
