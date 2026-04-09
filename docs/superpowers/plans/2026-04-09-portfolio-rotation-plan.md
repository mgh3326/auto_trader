# Portfolio Rotation Plan Implementation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add crypto rotation recommendations to the existing portfolio analysis flow and dashboard, reusing existing holdings/journal/screener infrastructure.

**Architecture:** A new `PortfolioRotationService` classifies Upbit positions into sell/locked/ignored buckets using strategy signals and trade journal context, then fetches screener-based buy candidates. The service is consumed by both the MCP `analyze_portfolio` tool (via new optional flag) and a new thin REST endpoint rendered on the existing portfolio dashboard.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, existing MCP helpers (`_collect_portfolio_positions`, `screen_stocks_impl`, `TradeJournal` model)

**Spec:** `docs/superpowers/specs/2026-04-09-portfolio-rotation-plan-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `app/services/portfolio_rotation_service.py` | Create | Core rotation logic — classify positions, fetch buy candidates |
| `tests/test_portfolio_rotation_service.py` | Create | Unit tests for rotation service |
| `app/mcp_server/tooling/analysis_registration.py` | Modify (lines 122-131) | Add `include_rotation_plan` param to `analyze_portfolio` |
| `app/mcp_server/tooling/analysis_tool_handlers.py` | Modify (lines 495-515) | Wire rotation plan into `analyze_portfolio_impl` |
| `app/routers/portfolio.py` | Modify (add endpoint after line 341) | Add `GET /api/rotation-plan` |
| `app/templates/portfolio_dashboard.html` | Modify (lines 679-684, 689-701) | Add rotation panel HTML + JS |

---

### Task 1: Create `PortfolioRotationService` with tests

**Files:**
- Create: `app/services/portfolio_rotation_service.py`
- Create: `tests/test_portfolio_rotation_service.py`

- [ ] **Step 1: Write the failing test — unsupported market guard**

Create `tests/test_portfolio_rotation_service.py`:

```python
"""Tests for portfolio_rotation_service."""

from __future__ import annotations

import pytest

from app.services.portfolio_rotation_service import PortfolioRotationService


class TestBuildRotationPlan:
    """Tests for PortfolioRotationService.build_rotation_plan."""

    @pytest.fixture()
    def service(self) -> PortfolioRotationService:
        return PortfolioRotationService()

    @pytest.mark.asyncio
    async def test_unsupported_market_returns_not_supported(
        self, service: PortfolioRotationService
    ):
        result = await service.build_rotation_plan(market="kr")
        assert result["supported"] is False
        assert result["market"] == "kr"
        assert "warning" in result

    @pytest.mark.asyncio
    async def test_unsupported_market_us(
        self, service: PortfolioRotationService
    ):
        result = await service.build_rotation_plan(market="us")
        assert result["supported"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_portfolio_rotation_service.py::TestBuildRotationPlan::test_unsupported_market_returns_not_supported -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.portfolio_rotation_service'`

- [ ] **Step 3: Write minimal service with market guard**

Create `app/services/portfolio_rotation_service.py`:

```python
"""Portfolio rotation plan service.

Classifies crypto positions into sell/locked/ignored buckets based on
strategy signals and trade journal context, then fetches screener-based
buy candidates.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

LOCKED_STRATEGIES: frozenset[str] = frozenset({
    "coinmoogi_dca",
    "staking_hold",
    "index_dca",
})
DUST_THRESHOLD_KRW: float = 5_000
PARTIAL_REDUCE_PCT: int = 30


class PortfolioRotationService:
    """Build rotation plans for crypto portfolios."""

    async def build_rotation_plan(
        self,
        *,
        market: str = "crypto",
        account: str | None = None,
    ) -> dict[str, Any]:
        if market != "crypto":
            return {
                "supported": False,
                "market": market,
                "warning": "Rotation plan is currently supported for crypto only.",
            }

        return {
            "supported": True,
            "market": "crypto",
            "account": account or "upbit",
            "generated_at": None,
            "summary": {
                "total_positions": 0,
                "actionable_positions": 0,
                "locked_positions": 0,
                "ignored_positions": 0,
                "buy_candidates": 0,
            },
            "sell_candidates": [],
            "buy_candidates": [],
            "locked_positions": [],
            "ignored_positions": [],
            "warnings": [],
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_portfolio_rotation_service.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/portfolio_rotation_service.py tests/test_portfolio_rotation_service.py
git commit -m "feat: add PortfolioRotationService skeleton with market guard"
```

---

### Task 2: Add position classification logic with tests

**Files:**
- Modify: `tests/test_portfolio_rotation_service.py`
- Modify: `app/services/portfolio_rotation_service.py`

- [ ] **Step 1: Write failing tests for position classification**

Append to `tests/test_portfolio_rotation_service.py`:

```python
from unittest.mock import AsyncMock, patch


def _make_position(
    symbol: str = "KRW-BTC",
    name: str = "비트코인",
    evaluation_amount: float = 100_000,
    profit_rate: float = 5.0,
    current_price: float = 50_000_000,
    strategy_signal: dict | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": name,
        "account": "upbit",
        "instrument_type": "crypto",
        "market": "crypto",
        "current_price": current_price,
        "evaluation_amount": evaluation_amount,
        "profit_rate": profit_rate,
        "profit_loss": evaluation_amount * profit_rate / 100,
        "avg_buy_price": current_price / (1 + profit_rate / 100),
        "quantity": 0.002,
        "strategy_signal": strategy_signal,
    }


def _make_journal(
    symbol: str = "KRW-BTC",
    strategy: str | None = "coinmoogi_dca",
    status: str = "active",
    hold_until: str | None = "2099-12-31T00:00:00",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "strategy": strategy,
        "status": status,
        "hold_until": hold_until,
    }


from typing import Any


class TestClassifyPositions:
    """Tests for position classification logic."""

    @pytest.fixture()
    def service(self) -> PortfolioRotationService:
        return PortfolioRotationService()

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_locked_strategy_classification(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        mock_positions.return_value = (
            [_make_position(symbol="KRW-BTC", name="비트코인")],
            [],
        )
        mock_journals.return_value = {
            "KRW-BTC": _make_journal(symbol="KRW-BTC", strategy="coinmoogi_dca"),
        }
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")
        assert result["supported"] is True
        assert len(result["locked_positions"]) == 1
        assert result["locked_positions"][0]["symbol"] == "KRW-BTC"
        assert result["locked_positions"][0]["lock_reason"] == "locked strategy"

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_dust_position_ignored(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        mock_positions.return_value = (
            [_make_position(symbol="KRW-SHIB", name="시바이누", evaluation_amount=1_200)],
            [],
        )
        mock_journals.return_value = {}
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")
        assert len(result["ignored_positions"]) == 1
        assert result["ignored_positions"][0]["symbol"] == "KRW-SHIB"

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_sell_candidate_from_stop_loss(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        mock_positions.return_value = (
            [
                _make_position(
                    symbol="KRW-WLD",
                    name="월드코인",
                    profit_rate=-8.5,
                    strategy_signal={"action": "sell", "reason": "stop_loss"},
                )
            ],
            [],
        )
        mock_journals.return_value = {}
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")
        assert len(result["sell_candidates"]) == 1
        cand = result["sell_candidates"][0]
        assert cand["symbol"] == "KRW-WLD"
        assert cand["action"] == "reduce_full"
        assert cand["reduce_pct"] == 100

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_sell_candidate_partial_reduce_dca_oversold(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        mock_positions.return_value = (
            [_make_position(symbol="KRW-XRP", name="리플", profit_rate=-5.0)],
            [],
        )
        mock_journals.return_value = {
            "KRW-XRP": _make_journal(
                symbol="KRW-XRP", strategy="dca_oversold", hold_until=None
            ),
        }
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")
        assert len(result["sell_candidates"]) == 1
        cand = result["sell_candidates"][0]
        assert cand["action"] == "reduce_partial"
        assert cand["reduce_pct"] == 30

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_healthy_position_not_surfaced(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        """Profitable position with no sell signal goes to none of the buckets."""
        mock_positions.return_value = (
            [_make_position(symbol="KRW-ETH", name="이더리움", profit_rate=12.0)],
            [],
        )
        mock_journals.return_value = {}
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")
        assert len(result["sell_candidates"]) == 0
        assert len(result["locked_positions"]) == 0
        assert len(result["ignored_positions"]) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_portfolio_rotation_service.py::TestClassifyPositions -v`
Expected: FAIL — `_fetch_crypto_positions` not found

- [ ] **Step 3: Implement position classification in service**

Replace the body of `build_rotation_plan` in `app/services/portfolio_rotation_service.py` (the crypto branch) and add the helper functions:

```python
"""Portfolio rotation plan service.

Classifies crypto positions into sell/locked/ignored buckets based on
strategy signals and trade journal context, then fetches screener-based
buy candidates.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType

logger = logging.getLogger(__name__)

LOCKED_STRATEGIES: frozenset[str] = frozenset({
    "coinmoogi_dca",
    "staking_hold",
    "index_dca",
})
DUST_THRESHOLD_KRW: float = 5_000
PARTIAL_REDUCE_PCT: int = 30
DCA_OVERSOLD_LOSS_THRESHOLD: float = -3.0


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


async def _fetch_crypto_positions(
    account: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch Upbit crypto positions with current prices and strategy signals."""
    from app.mcp_server.tooling.portfolio_holdings import (
        _collect_portfolio_positions,
    )

    positions, errors, _market_filter, _account_filter = (
        await _collect_portfolio_positions(
            account=account,
            market="crypto",
            include_current_price=True,
        )
    )
    return positions, errors


async def _fetch_active_journals() -> dict[str, dict[str, Any]]:
    """Fetch active/draft crypto trade journals, keyed by symbol."""
    session_maker = _session_factory()
    async with session_maker() as session:
        stmt = (
            select(TradeJournal)
            .where(
                TradeJournal.instrument_type == InstrumentType.crypto,
                TradeJournal.status.in_(["draft", "active"]),
            )
            .order_by(TradeJournal.created_at.desc())
        )
        result = await session.execute(stmt)
        journals = result.scalars().all()

    journal_map: dict[str, dict[str, Any]] = {}
    for j in journals:
        symbol = j.symbol
        if symbol in journal_map:
            continue  # keep most recent (ordered desc)
        journal_map[symbol] = {
            "symbol": j.symbol,
            "strategy": j.strategy,
            "status": j.status,
            "hold_until": j.hold_until.isoformat() if j.hold_until else None,
        }
    return journal_map


async def _fetch_buy_candidates(
    held_symbols: set[str],
) -> list[dict[str, Any]]:
    """Fetch screener oversold candidates, excluding already-held symbols."""
    from app.mcp_server.tooling.analysis_tool_handlers import screen_stocks_impl

    try:
        result = await screen_stocks_impl(
            market="crypto",
            strategy="oversold",
            limit=20,
        )
    except Exception as exc:
        logger.warning("Failed to fetch buy candidates: %s", exc)
        return []

    candidates: list[dict[str, Any]] = []
    for row in result.get("results", []):
        symbol = row.get("symbol", "")
        if symbol in held_symbols:
            continue
        candidates.append({
            "symbol": symbol,
            "name": row.get("name", ""),
            "price": row.get("price"),
            "change_rate": row.get("change_rate"),
            "trade_amount_24h": row.get("trade_amount"),
            "screen_reason": ["RSI oversold", "sufficient liquidity"],
        })
        if len(candidates) >= 10:
            break
    return candidates


def _classify_position(
    position: dict[str, Any],
    journal: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """Classify a single position into a bucket.

    Returns:
        (bucket_name, detail_dict) where bucket_name is one of:
        "locked", "ignored", "sell", "healthy"
    """
    symbol = position.get("symbol", "")
    name = position.get("name", "")
    evaluation = position.get("evaluation_amount") or 0
    profit_rate = position.get("profit_rate") or 0
    strategy_signal = position.get("strategy_signal")
    journal_strategy = journal.get("strategy") if journal else None

    # 1. Dust check first
    if evaluation < DUST_THRESHOLD_KRW:
        return "ignored", {
            "symbol": symbol,
            "name": name,
            "evaluation_amount": evaluation,
            "ignore_reason": f"dust position (< {DUST_THRESHOLD_KRW:,.0f} KRW)",
        }

    # 2. Locked strategy check
    if journal_strategy and journal_strategy in LOCKED_STRATEGIES:
        return "locked", {
            "symbol": symbol,
            "name": name,
            "journal_strategy": journal_strategy,
            "lock_reason": "locked strategy",
        }

    # 3. Hold-until not expired
    if journal:
        hold_until_str = journal.get("hold_until")
        if hold_until_str:
            try:
                hold_until = datetime.fromisoformat(hold_until_str)
                if hold_until > now_kst():
                    return "locked", {
                        "symbol": symbol,
                        "name": name,
                        "journal_strategy": journal_strategy,
                        "lock_reason": f"hold until {hold_until_str[:10]}",
                    }
            except (ValueError, TypeError):
                pass

    # 4. Sell candidates
    reasons: list[str] = []
    is_stop_loss = False

    # 4a. Strategy signal says sell
    if isinstance(strategy_signal, dict) and strategy_signal.get("action") == "sell":
        signal_reason = strategy_signal.get("reason", "signal")
        reasons.append(f"{signal_reason} signal")
        if signal_reason == "stop_loss":
            is_stop_loss = True

    # 4b. dca_oversold with significant loss
    if journal_strategy == "dca_oversold" and profit_rate < DCA_OVERSOLD_LOSS_THRESHOLD:
        reasons.append("dca_oversold with significant loss")

    # 4c. No journal + negative P&L
    if not journal and profit_rate < 0:
        reasons.append("no active journal")

    if reasons:
        action = "reduce_full" if is_stop_loss else "reduce_partial"
        reduce_pct = 100 if is_stop_loss else PARTIAL_REDUCE_PCT
        return "sell", {
            "symbol": symbol,
            "name": name,
            "current_price": position.get("current_price"),
            "profit_rate": profit_rate,
            "evaluation_amount": evaluation,
            "action": action,
            "reduce_pct": reduce_pct,
            "reason": reasons,
            "journal_strategy": journal_strategy,
        }

    # 5. Healthy — not surfaced
    return "healthy", {}


class PortfolioRotationService:
    """Build rotation plans for crypto portfolios."""

    async def build_rotation_plan(
        self,
        *,
        market: str = "crypto",
        account: str | None = None,
    ) -> dict[str, Any]:
        if market != "crypto":
            return {
                "supported": False,
                "market": market,
                "warning": "Rotation plan is currently supported for crypto only.",
            }

        warnings: list[str] = []

        # 1. Fetch positions
        positions, pos_errors = await _fetch_crypto_positions(account=account)
        for err in pos_errors:
            warnings.append(str(err.get("error", err)))

        # 2. Fetch journals
        try:
            journal_map = await _fetch_active_journals()
        except Exception as exc:
            logger.warning("Failed to fetch journals: %s", exc)
            journal_map = {}
            warnings.append(f"Journal fetch failed: {exc}")

        # 3. Classify positions
        sell_candidates: list[dict[str, Any]] = []
        locked_positions: list[dict[str, Any]] = []
        ignored_positions: list[dict[str, Any]] = []

        held_symbols: set[str] = set()
        for pos in positions:
            symbol = pos.get("symbol", "")
            held_symbols.add(symbol)
            journal = journal_map.get(symbol)
            bucket, detail = _classify_position(pos, journal)
            if bucket == "sell":
                sell_candidates.append(detail)
            elif bucket == "locked":
                locked_positions.append(detail)
            elif bucket == "ignored":
                ignored_positions.append(detail)

        # 4. Fetch buy candidates
        buy_candidates = await _fetch_buy_candidates(held_symbols)

        generated_at = now_kst().isoformat()

        return {
            "supported": True,
            "market": "crypto",
            "account": account or "upbit",
            "generated_at": generated_at,
            "summary": {
                "total_positions": len(positions),
                "actionable_positions": len(sell_candidates),
                "locked_positions": len(locked_positions),
                "ignored_positions": len(ignored_positions),
                "buy_candidates": len(buy_candidates),
            },
            "sell_candidates": sell_candidates,
            "buy_candidates": buy_candidates,
            "locked_positions": locked_positions,
            "ignored_positions": ignored_positions,
            "warnings": warnings,
        }
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `uv run pytest tests/test_portfolio_rotation_service.py -v`
Expected: All 7 tests pass

- [ ] **Step 5: Commit**

```bash
git add app/services/portfolio_rotation_service.py tests/test_portfolio_rotation_service.py
git commit -m "feat: implement position classification in PortfolioRotationService"
```

---

### Task 3: Add buy-candidate and response-shape tests

**Files:**
- Modify: `tests/test_portfolio_rotation_service.py`

- [ ] **Step 1: Write failing tests for buy candidate filtering and response shape**

Append to `tests/test_portfolio_rotation_service.py`:

```python
class TestBuyCandidates:

    @pytest.fixture()
    def service(self) -> PortfolioRotationService:
        return PortfolioRotationService()

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_buy_candidates_exclude_held(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        """Buy candidates that match held symbols should already be filtered."""
        mock_positions.return_value = (
            [_make_position(symbol="KRW-BTC")],
            [],
        )
        mock_journals.return_value = {}
        # _fetch_buy_candidates receives held_symbols, verify it was called with them
        mock_buy.return_value = [
            {"symbol": "KRW-BARD", "name": "롬바드", "price": 100, "trade_amount_24h": 5e9, "screen_reason": ["RSI oversold"]},
        ]

        result = await service.build_rotation_plan(market="crypto")
        mock_buy.assert_called_once()
        call_args = mock_buy.call_args
        assert "KRW-BTC" in call_args[1].get("held_symbols", call_args[0][0] if call_args[0] else set())
        assert len(result["buy_candidates"]) == 1
        assert result["buy_candidates"][0]["symbol"] == "KRW-BARD"


class TestResponseShape:

    @pytest.fixture()
    def service(self) -> PortfolioRotationService:
        return PortfolioRotationService()

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_response_has_all_required_keys(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        mock_positions.return_value = ([], [])
        mock_journals.return_value = {}
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")

        required_keys = {
            "supported", "market", "account", "generated_at",
            "summary", "sell_candidates", "buy_candidates",
            "locked_positions", "ignored_positions", "warnings",
        }
        assert required_keys <= set(result.keys())

        summary_keys = {
            "total_positions", "actionable_positions",
            "locked_positions", "ignored_positions", "buy_candidates",
        }
        assert summary_keys <= set(result["summary"].keys())
        assert result["supported"] is True
        assert result["generated_at"] is not None
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_portfolio_rotation_service.py -v`
Expected: All 9 tests pass (they should pass immediately since the implementation is already complete)

- [ ] **Step 3: Commit**

```bash
git add tests/test_portfolio_rotation_service.py
git commit -m "test: add buy-candidate filtering and response shape tests"
```

---

### Task 4: Extend `analyze_portfolio` MCP tool

**Files:**
- Modify: `app/mcp_server/tooling/analysis_registration.py` (lines 122-131)
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py` (lines 495-515)

- [ ] **Step 1: Write failing test for analyze_portfolio with rotation flag**

Append to `tests/test_portfolio_rotation_service.py`:

```python
class TestAnalyzePortfolioRotation:

    @pytest.mark.asyncio
    @patch(
        "app.mcp_server.tooling.analysis_tool_handlers._run_batch_analysis",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service.PortfolioRotationService.build_rotation_plan",
        new_callable=AsyncMock,
    )
    async def test_analyze_portfolio_with_rotation_plan(
        self,
        mock_rotation: AsyncMock,
        mock_batch: AsyncMock,
    ):
        from app.mcp_server.tooling.analysis_tool_handlers import (
            analyze_portfolio_impl,
        )

        mock_batch.return_value = {
            "results": {"KRW-BTC": {"price": 50000000}},
            "summary": {"total_symbols": 1, "successful": 1, "failed": 0, "errors": []},
        }
        mock_rotation.return_value = {
            "supported": True,
            "market": "crypto",
            "sell_candidates": [],
            "buy_candidates": [],
        }

        result = await analyze_portfolio_impl(
            symbols=["KRW-BTC"],
            market="crypto",
            include_rotation_plan=True,
        )
        assert "rotation_plan" in result
        assert result["rotation_plan"]["supported"] is True
        mock_rotation.assert_called_once()

    @pytest.mark.asyncio
    @patch(
        "app.mcp_server.tooling.analysis_tool_handlers._run_batch_analysis",
        new_callable=AsyncMock,
    )
    async def test_analyze_portfolio_without_rotation_unchanged(
        self,
        mock_batch: AsyncMock,
    ):
        from app.mcp_server.tooling.analysis_tool_handlers import (
            analyze_portfolio_impl,
        )

        mock_batch.return_value = {
            "results": {},
            "summary": {"total_symbols": 0, "successful": 0, "failed": 0, "errors": []},
        }

        result = await analyze_portfolio_impl(symbols=[], market="crypto")
        assert "rotation_plan" not in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_portfolio_rotation_service.py::TestAnalyzePortfolioRotation -v`
Expected: FAIL — `analyze_portfolio_impl() got an unexpected keyword argument 'include_rotation_plan'`

- [ ] **Step 3: Modify `analysis_tool_handlers.py`**

In `app/mcp_server/tooling/analysis_tool_handlers.py`, replace lines 495-515:

```python
async def analyze_portfolio_impl(
    symbols: list[str | int],
    market: str | None = None,
    include_peers: bool = False,
    include_rotation_plan: bool = False,
) -> dict[str, Any]:
    """Analyze a portfolio of symbols.

    Args:
        symbols: List of symbol inputs (1-10 entries)
        market: Optional market override
        include_peers: Whether to include peer analysis
        include_rotation_plan: Whether to append rotation plan for crypto

    Returns:
        Dict with 'results' (symbol -> analysis_result) and 'summary' keys
    """
    result = await _run_batch_analysis(
        symbols,
        market=market,
        include_peers=include_peers,
        formatter=lambda _sym, result: result,
    )

    if include_rotation_plan:
        from app.services.portfolio_rotation_service import PortfolioRotationService

        rotation_service = PortfolioRotationService()
        result["rotation_plan"] = await rotation_service.build_rotation_plan(
            market=market or "crypto",
        )

    return result
```

- [ ] **Step 4: Modify `analysis_registration.py`**

In `app/mcp_server/tooling/analysis_registration.py`, replace lines 122-131:

```python
    async def analyze_portfolio(
        symbols: list[str | int],
        market: str | None = None,
        include_peers: bool = False,
        include_rotation_plan: bool = False,
    ) -> dict[str, Any]:
        return await analyze_portfolio_impl(
            symbols=symbols,
            market=market,
            include_peers=include_peers,
            include_rotation_plan=include_rotation_plan,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_portfolio_rotation_service.py -v`
Expected: All 11 tests pass

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/analysis_registration.py app/mcp_server/tooling/analysis_tool_handlers.py tests/test_portfolio_rotation_service.py
git commit -m "feat: extend analyze_portfolio with include_rotation_plan flag"
```

---

### Task 5: Add portfolio rotation API endpoint

**Files:**
- Modify: `app/routers/portfolio.py` (add after line 341)

- [ ] **Step 1: Add the endpoint**

In `app/routers/portfolio.py`, add after the `get_portfolio_cash` endpoint (after line 341):

```python
@router.get("/api/rotation-plan")
async def get_rotation_plan(
    _current_user: User = Depends(get_authenticated_user),
):
    from app.services.portfolio_rotation_service import PortfolioRotationService

    service = PortfolioRotationService()
    return await service.build_rotation_plan(market="crypto")
```

- [ ] **Step 2: Run existing portfolio router tests to check no regression**

Run: `uv run pytest tests/ -k "portfolio" -v --timeout=30 -x`
Expected: All existing portfolio tests pass

- [ ] **Step 3: Commit**

```bash
git add app/routers/portfolio.py
git commit -m "feat: add GET /portfolio/api/rotation-plan endpoint"
```

---

### Task 6: Add rotation panel to portfolio dashboard

**Files:**
- Modify: `app/templates/portfolio_dashboard.html` (lines 679-684 for HTML, lines 689-701 for JS state)

- [ ] **Step 1: Add rotation panel HTML**

In `app/templates/portfolio_dashboard.html`, after the `portfolio-status-panel` closing `</article>` (line 683) and before the closing `</section>` (line 684), insert:

```html
                <article class="panel" id="portfolio-rotation-panel" style="grid-column: 1 / -1; display: none;">
                    <div class="d-flex justify-content-between align-items-center">
                        <h2><i class="bi bi-arrow-repeat"></i> 로테이션 제안</h2>
                        <div>
                            <span class="subtle" id="rotation-summary-badge">-</span>
                            <button class="btn btn-sm btn-outline-secondary ms-2"
                                    id="rotation-refresh-btn" title="새로고침">
                                <i class="bi bi-arrow-clockwise"></i>
                            </button>
                        </div>
                    </div>
                    <div id="rotation-content" class="mt-2">
                        <div class="subtle">로딩 중...</div>
                    </div>
                </article>
```

- [ ] **Step 2: Add rotation JS state property**

In the `<script>` block, add `rotationPlan: null,` to the `state` object (after `sortMode` around line 701):

```javascript
        const state = {
            overview: null,
            cash: null,
            exchangeRate: null,
            selectedAccounts: new Set(),
            charts: {
                allocation: null,
                topPositions: null,
            },
            detailRequestId: 0,
            positionInteractionsBound: false,
            sortMode: "evaluation_desc",
            rotationPlan: null,
        };
```

- [ ] **Step 3: Add fetch and render functions**

Add the following JavaScript functions at the end of the `<script>` block, before the closing `</script>` tag:

```javascript
        /* ── Rotation Plan ─────────────────────────────────────── */
        const rotationPanel = document.getElementById("portfolio-rotation-panel");
        const rotationContent = document.getElementById("rotation-content");
        const rotationSummaryBadge = document.getElementById("rotation-summary-badge");
        const rotationRefreshBtn = document.getElementById("rotation-refresh-btn");

        async function fetchRotationPlan() {
            const market = marketSelect.value;
            if (market !== "ALL" && market !== "CRYPTO") {
                rotationPanel.style.display = "none";
                return;
            }
            rotationPanel.style.display = "";
            rotationContent.innerHTML = '<div class="subtle">로딩 중...</div>';
            try {
                const resp = await fetch("/portfolio/api/rotation-plan");
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                state.rotationPlan = await resp.json();
                renderRotationPlan(state.rotationPlan);
            } catch (err) {
                rotationContent.innerHTML = `<div class="subtle">로테이션 정보를 가져올 수 없습니다: ${escapeHtml(err.message)}</div>`;
            }
        }

        function renderRotationPlan(plan) {
            if (!plan || !plan.supported) {
                rotationContent.innerHTML = '<div class="subtle">지원되지 않는 마켓입니다.</div>';
                rotationPanel.style.display = "none";
                return;
            }
            const s = plan.summary;
            rotationSummaryBadge.textContent =
                `축소 ${s.actionable_positions} · 매수후보 ${s.buy_candidates} · 잠금 ${s.locked_positions}`;

            let html = "";

            // Sell candidates
            if (plan.sell_candidates.length) {
                html += `<h3 class="subtle mt-2">축소/매도 후보</h3><div class="summary-list">`;
                for (const c of plan.sell_candidates) {
                    const profitClass = (c.profit_rate || 0) < 0 ? "profit-negative" : "profit-positive";
                    const badge = c.action === "reduce_full"
                        ? '<span class="badge bg-danger ms-1">전량 매도</span>'
                        : `<span class="badge bg-warning text-dark ms-1">부분 축소 ${c.reduce_pct}%</span>`;
                    html += `<div class="summary-row">
                        <span><strong>${escapeHtml(c.name)}</strong> <span class="subtle">${escapeHtml(c.symbol)}</span>${badge}</span>
                        <span class="${profitClass}">${(c.profit_rate || 0).toFixed(1)}%</span>
                    </div>`;
                    if (c.reason && c.reason.length) {
                        html += `<div class="subtle" style="padding-left:1rem;font-size:0.82rem;">${c.reason.map(r => escapeHtml(r)).join(" · ")}</div>`;
                    }
                }
                html += `</div>`;
            }

            // Buy candidates
            if (plan.buy_candidates.length) {
                html += `<h3 class="subtle mt-3">매수 후보 (oversold)</h3><div class="summary-list">`;
                for (const c of plan.buy_candidates) {
                    html += `<div class="summary-row">
                        <span><strong>${escapeHtml(c.name)}</strong> <span class="subtle">${escapeHtml(c.symbol)}</span></span>
                        <span class="subtle">${c.trade_amount_24h ? formatNumber(c.trade_amount_24h, 0) + '원' : '-'}</span>
                    </div>`;
                }
                html += `</div>`;
            }

            // Locked
            if (plan.locked_positions.length) {
                html += `<h3 class="subtle mt-3"><i class="bi bi-lock"></i> 잠금</h3><div class="summary-list">`;
                for (const c of plan.locked_positions) {
                    html += `<div class="summary-row">
                        <span>${escapeHtml(c.name)} <span class="subtle">${escapeHtml(c.symbol)}</span></span>
                        <span class="subtle">${escapeHtml(c.lock_reason || c.journal_strategy || '')}</span>
                    </div>`;
                }
                html += `</div>`;
            }

            // Ignored (collapsed)
            if (plan.ignored_positions.length) {
                html += `<details class="mt-3"><summary class="subtle" style="cursor:pointer;">무시됨 (${plan.ignored_positions.length})</summary><div class="summary-list">`;
                for (const c of plan.ignored_positions) {
                    html += `<div class="summary-row">
                        <span class="subtle">${escapeHtml(c.name)} ${escapeHtml(c.symbol)}</span>
                        <span class="subtle">${formatNumber(c.evaluation_amount || 0, 0)}원</span>
                    </div>`;
                }
                html += `</div></details>`;
            }

            // Warnings
            if (plan.warnings && plan.warnings.length) {
                html += `<div class="subtle mt-2" style="color:var(--bs-warning);">${plan.warnings.map(w => escapeHtml(w)).join("<br>")}</div>`;
            }

            if (!html) {
                html = '<div class="subtle">로테이션 제안 없음</div>';
            }

            rotationContent.innerHTML = html;
        }

        if (rotationRefreshBtn) {
            rotationRefreshBtn.addEventListener("click", () => fetchRotationPlan());
        }
```

- [ ] **Step 4: Wire fetch into the overview load flow**

Three insertion points:

**4a.** In `fetchOverview()` (line 1583), after `await fetchOverviewEnrich(...)` (line 1610), add inside the try block:

```javascript
                fetchRotationPlan();
```

**4b.** In the `DOMContentLoaded` handler (line 1690), after `fetchCashSummary()` (line 1692), add:

```javascript
            fetchRotationPlan();
```

**4c.** On the `marketSelect` change listener (line 1677), change:

```javascript
        marketSelect.addEventListener("change", fetchOverview);
```

to:

```javascript
        marketSelect.addEventListener("change", () => { fetchOverview(); fetchRotationPlan(); });
```

- [ ] **Step 5: Commit**

```bash
git add app/templates/portfolio_dashboard.html
git commit -m "feat: add rotation plan panel to portfolio dashboard"
```

---

### Task 7: Integration smoke test and final cleanup

**Files:**
- Modify: `tests/test_portfolio_rotation_service.py` (if any fixes needed)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/test_portfolio_rotation_service.py -v`
Expected: All tests pass

- [ ] **Step 2: Run lint and type checks**

Run: `make lint` (or `uv run ruff check app/services/portfolio_rotation_service.py app/mcp_server/tooling/analysis_registration.py app/mcp_server/tooling/analysis_tool_handlers.py app/routers/portfolio.py`)
Expected: No errors

Fix any lint issues.

- [ ] **Step 3: Run broader test suite for regressions**

Run: `uv run pytest tests/ -k "portfolio or analysis" --timeout=30 -x -q`
Expected: No regressions

- [ ] **Step 4: Final commit if any lint fixes were needed**

```bash
git add -u
git commit -m "fix: lint cleanup for rotation plan feature"
```

---

## Summary of All Files

| File | Action | Purpose |
|------|--------|---------|
| `app/services/portfolio_rotation_service.py` | **NEW** | Core rotation logic: classify positions, fetch candidates |
| `tests/test_portfolio_rotation_service.py` | **NEW** | 11 unit tests covering all buckets and MCP integration |
| `app/mcp_server/tooling/analysis_registration.py` | **MODIFY** | Add `include_rotation_plan` param (additive) |
| `app/mcp_server/tooling/analysis_tool_handlers.py` | **MODIFY** | Wire rotation into `analyze_portfolio_impl` |
| `app/routers/portfolio.py` | **MODIFY** | Add `GET /portfolio/api/rotation-plan` |
| `app/templates/portfolio_dashboard.html` | **MODIFY** | Rotation panel HTML + JS (fetch, render, refresh) |
