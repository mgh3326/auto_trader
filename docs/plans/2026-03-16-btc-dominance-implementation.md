# Plan A: BTC Dominance Implementation

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement BTC dominance fetch from CoinGecko `/api/v3/global` endpoint in the n8n market-context API

**Architecture:** Create a new `fetch_btc_dominance()` service in `app/services/external/btc_dominance.py` following the existing Fear & Greed pattern (httpx + async cache), then wire it into `_fetch_crypto_market_overview()` in `n8n_market_context_service.py`.

**Tech Stack:** Python 3.13+, httpx, pytest

---

## Pre-Implementation Context

**Existing Patterns to Follow:**
- `app/services/external/fear_greed.py` - Same caching pattern, httpx usage, error handling
- `app/mcp_server/tooling/fundamentals_sources_coingecko.py` - Existing CoinGecko integration reference
- `tests/test_n8n_market_context.py` - Test patterns using `unittest.mock.patch`

**CoinGecko API Details:**
- Endpoint: `GET https://api.coingecko.com/api/v3/global`
- Response structure: `{"data": {"market_cap_percentage": {"btc": 61.2, ...}, "market_cap_change_percentage_24h_usd": 2.3}}`
- No API key required for public endpoints
- Rate limit: ~10-30 calls/minute on free tier

---

## Task 1: Create BTC Dominance Service

**Files:**
- Create: `app/services/external/btc_dominance.py`

**Step 1: Write the failing test**

```python
# tests/test_n8n_market_context.py - Add new test class
@pytest.mark.unit
class TestBtcDominanceService:
    """Tests for BTC dominance service."""

    @pytest.mark.asyncio
    async def test_fetch_btc_dominance_success(self) -> None:
        """Test successful BTC dominance fetch."""
        from app.services.external.btc_dominance import fetch_btc_dominance

        mock_response = {
            "data": {
                "market_cap_percentage": {"btc": 61.2, "eth": 12.5},
                "market_cap_change_percentage_24h_usd": 2.3
            }
        }

        with patch(
            "app.services.external.btc_dominance.httpx.AsyncClient.get",
            return_value=MagicMock(
                raise_for_status=lambda: None,
                json=lambda: mock_response
            )
        ):
            result = await fetch_btc_dominance()
            assert result is not None
            assert result["btc_dominance"] == 61.2
            assert result["total_market_cap_change_24h"] == 2.3

    @pytest.mark.asyncio
    async def test_fetch_btc_dominance_handles_error(self) -> None:
        """Test BTC dominance fetch handles API errors."""
        from app.services.external.btc_dominance import fetch_btc_dominance

        with patch(
            "app.services.external.btc_dominance.httpx.AsyncClient.get",
            side_effect=Exception("Network error")
        ):
            result = await fetch_btc_dominance()
            assert result is None
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_n8n_market_context.py::TestBtcDominanceService -v
```

Expected: `ImportError: cannot import name 'fetch_btc_dominance'`

**Step 3: Create the service module**

```python
# app/services/external/btc_dominance.py
"""BTC dominance service using CoinGecko API."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.timezone import now_kst

logger = logging.getLogger(__name__)

_btc_dominance_cache: dict[str, Any] = {}
_btc_dominance_cache_expires: datetime | None = None
_cache_lock = asyncio.Lock()

COIN_GECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"


async def fetch_btc_dominance() -> dict[str, Any] | None:
    """
    Fetch BTC dominance and global market data from CoinGecko.

    Returns:
        {
            "btc_dominance": float,  # BTC market cap percentage
            "total_market_cap_change_24h": float  # 24h change %
        }
        or None if fetch fails
    """
    global _btc_dominance_cache, _btc_dominance_cache_expires

    async with _cache_lock:
        if _btc_dominance_cache_expires and now_kst() < _btc_dominance_cache_expires:
            logger.debug("Returning cached BTC dominance data")
            return _btc_dominance_cache.copy()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(COIN_GECKO_GLOBAL_URL)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning(f"Failed to fetch BTC dominance: {exc}")
        return None

    try:
        market_data = data.get("data", {})
        market_cap_pct = market_data.get("market_cap_percentage", {})
        btc_dominance = market_cap_pct.get("btc")
        market_cap_change = market_data.get("market_cap_change_percentage_24h_usd")

        if btc_dominance is None:
            logger.warning("BTC dominance not found in CoinGecko response")
            return None

        result = {
            "btc_dominance": round(float(btc_dominance), 2),
            "total_market_cap_change_24h": round(float(market_cap_change), 2) if market_cap_change is not None else None,
        }

        async with _cache_lock:
            _btc_dominance_cache = result.copy()
            _btc_dominance_cache_expires = now_kst() + timedelta(minutes=30)

        return result

    except Exception as exc:
        logger.warning(f"Failed to parse BTC dominance response: {exc}")
        return None
```

**Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_n8n_market_context.py::TestBtcDominanceService -v
```

Expected: All tests pass

**Step 5: Commit**

```bash
git add app/services/external/btc_dominance.py tests/test_n8n_market_context.py
git commit -m "feat(n8n): add BTC dominance service with CoinGecko integration"
```

---

## Task 2: Integrate BTC Dominance into Market Context Service

**Files:**
- Modify: `app/services/n8n_market_context_service.py` (lines 137-154)

**Step 1: Update imports**

```python
# Add to existing imports at top of file
from app.services.external.btc_dominance import fetch_btc_dominance
```

**Step 2: Replace stub implementation**

```python
# OLD CODE (lines 137-154):
async def _fetch_crypto_market_overview() -> dict[str, Any]:
    """Fetch crypto market overview data (BTC dominance, market cap change)."""
    try:
        await fetch_multiple_tickers(["KRW-BTC", "KRW-ETH"])

        btc_dominance = None
        total_market_cap_change = None

        return {
            "btc_dominance": btc_dominance,
            "total_market_cap_change_24h": total_market_cap_change,
        }
    except Exception as exc:
        logger.warning(f"Failed to fetch market overview: {exc}")
        return {
            "btc_dominance": None,
            "total_market_cap_change_24h": None,
        }

# NEW CODE:
async def _fetch_crypto_market_overview() -> dict[str, Any]:
    """Fetch crypto market overview data (BTC dominance, market cap change)."""
    try:
        dominance_data = await fetch_btc_dominance()

        if dominance_data:
            return {
                "btc_dominance": dominance_data.get("btc_dominance"),
                "total_market_cap_change_24h": dominance_data.get("total_market_cap_change_24h"),
            }
        else:
            return {
                "btc_dominance": None,
                "total_market_cap_change_24h": None,
            }
    except Exception as exc:
        logger.warning(f"Failed to fetch market overview: {exc}")
        return {
            "btc_dominance": None,
            "total_market_cap_change_24h": None,
        }
```

**Step 3: Run integration tests**

```bash
uv run pytest tests/test_n8n_market_context.py::TestMarketContextEndpoint -v
```

Expected: All tests pass (mocked)

**Step 4: Commit**

```bash
git add app/services/n8n_market_context_service.py
git commit -m "feat(n8n): integrate BTC dominance into market context API"
```

---

## Task 3: Add Integration Test for Market Context with BTC Dominance

**Files:**
- Modify: `tests/test_n8n_market_context.py`

**Step 1: Add integration test**

```python
# Add to TestMarketContextEndpoint class
@pytest.mark.integration
@pytest.mark.asyncio
async def test_market_context_with_btc_dominance(self, client: TestClient) -> None:
    """Test that market context endpoint returns BTC dominance data."""
    with patch("app.services.n8n_market_context_service.fetch_btc_dominance") as mock_btc, \
         patch("app.services.n8n_market_context_service.fetch_fear_greed") as mock_fg, \
         patch("app.services.n8n_market_context_service.fetch_economic_events_today") as mock_econ:
        
        mock_btc.return_value = {
            "btc_dominance": 61.5,
            "total_market_cap_change_24h": 2.3
        }
        mock_fg.return_value = {
            "value": 45,
            "label": "Neutral",
            "previous": 42,
            "trend": "improving"
        }
        mock_econ.return_value = []

        response = client.get("/api/n8n/market-context")
        assert response.status_code == 200
        data = response.json()
        
        assert data["market_overview"]["btc_dominance"] == 61.5
        assert data["market_overview"]["total_market_cap_change_24h"] == 2.3
```

**Step 2: Run the new test**

```bash
uv run pytest tests/test_n8n_market_context.py::TestMarketContextEndpoint::test_market_context_with_btc_dominance -v -m "not live"
```

Expected: Test passes

**Step 3: Commit**

```bash
git add tests/test_n8n_market_context.py
git commit -m "test(n8n): add BTC dominance integration test"
```

---

## Task 4: Verification & Final Checks

**Step 1: Run all related tests**

```bash
uv run pytest tests/test_n8n_market_context.py -v -m "not live"
```

Expected: All tests pass

**Step 2: Check lint/type compliance**

```bash
make lint
```

Expected: No errors in changed files

**Step 3: Manual verification (optional)**

```bash
# Start dev server in one terminal
make dev

# In another terminal, test the endpoint
curl "http://localhost:8000/api/n8n/market-context" | jq '.market_overview'
```

Expected: Response includes `btc_dominance` and `total_market_cap_change_24h` values

**Step 4: Final commit if any fixes needed**

```bash
# Only if lint/type fixes were needed
git add -A
git commit -m "fix(n8n): address lint/type issues in BTC dominance implementation"
```

---

## Summary

**Files Created:**
- `app/services/external/btc_dominance.py` - New service for CoinGecko global API

**Files Modified:**
- `app/services/n8n_market_context_service.py` - Integrated BTC dominance fetch
- `tests/test_n8n_market_context.py` - Added unit and integration tests

**API Response Change:**
```json
{
  "market_overview": {
    "btc_dominance": 61.5,           // NEW: Now populated
    "total_market_cap_change_24h": 2.3,  // NEW: Now populated
    "fear_greed": {...},
    "economic_events_today": []
  }
}
```

---

## Execution Options

**Plan saved to:** `docs/plans/YYYY-MM-DD-btc-dominance-implementation.md`

**Two execution options:**

1. **Subagent-Driven (this session)** - I dispatch fresh subagent per task, review between tasks, fast iteration
2. **Parallel Session (separate)** - Open new session with executing-plans, batch execution with checkpoints

Which approach would you prefer?
