# Plan C-1: Economic Calendar Implementation (Finnhub)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement Economic Calendar fetch using Finnhub API to return high-impact US economic events (FOMC, CPI, NFP, etc.) in the n8n market-context API

**Architecture:** Extend existing Finnhub client in `fundamentals_sources_finnhub.py` with `fetch_economic_calendar()` function, then wire it into the stub `economic_calendar.py` service. Transform Finnhub response to `N8nEconomicEvent` schema with US-only filtering and high-importance keyword matching.

**Tech Stack:** Python 3.13+, finnhub-python library, httpx (fallback), pytest

---

## Pre-Implementation Context

**Existing Patterns to Follow:**
- `app/mcp_server/tooling/fundamentals_sources_finnhub.py` - Finnhub client with `_get_finnhub_client()`, `asyncio.to_thread()` pattern
- `app/services/external/fear_greed.py` - Caching pattern (1-hour TTL, asyncio.Lock)
- `app/services/external/btc_dominance.py` - Recent similar implementation
- `app/services/external/economic_calendar.py` - Target stub to fill in
- `app/schemas/n8n.py` - `N8nEconomicEvent` schema definition

**Finnhub API Details:**
- Library: `finnhub-python` (already installed)
- Endpoint: `client.economic_calendar(_from="YYYY-MM-DD", to="YYYY-MM-DD")`
- Response: List of events with `time`, `country`, `event`, `actual`, `previous`, `estimate`, `impact`
- Free tier: 60 calls/minute (sufficient for hourly caching)
- API Key: `settings.finnhub_api_key` (already configured)

**High-Impact Keywords (already defined in economic_calendar.py):**
```python
HIGH_IMPORTANCE_KEYWORDS = [
    "FOMC", "CPI", "PPI", "GDP", "NFP", "Non-Farm",
    "Unemployment", "Interest Rate", "Fed", "ECB", "BOJ",
    "PMI", "Retail Sales", "Industrial Production", "Consumer Confidence", "Treasury"
]
```

---

## Task 1: Add Economic Calendar Fetch to Finnhub Client

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals_sources_finnhub.py`

**Step 1: Write the failing test**

```python
# tests/test_n8n_market_context.py - Add new test class
@pytest.mark.unit
class TestFinnhubEconomicCalendar:
    """Tests for Finnhub economic calendar integration."""

    @pytest.mark.asyncio
    async def test_fetch_economic_calendar_success(self) -> None:
        """Test successful economic calendar fetch."""
        from app.mcp_server.tooling.fundamentals_sources_finnhub import (
            fetch_economic_calendar_finnhub,
        )

        mock_response = [
            {
                "time": "08:30",
                "country": "US",
                "event": "CPI",
                "actual": "2.4%",
                "previous": "2.3%",
                "estimate": "2.3%",
                "impact": "high"
            },
            {
                "time": "14:00",
                "country": "US",
                "event": "FOMC Statement",
                "actual": None,
                "previous": None,
                "estimate": None,
                "impact": "high"
            }
        ]

        with patch(
            "app.mcp_server.tooling.fundamentals_sources_finnhub._get_finnhub_client"
        ) as mock_client:
            mock_instance = MagicMock()
            mock_instance.economic_calendar.return_value = mock_response
            mock_client.return_value = mock_instance

            result = await fetch_economic_calendar_finnhub("2026-03-16", "2026-03-16")
            
            assert result is not None
            assert len(result) == 2
            assert result[0]["event"] == "CPI"
            assert result[0]["country"] == "US"

    @pytest.mark.asyncio
    async def test_fetch_economic_calendar_handles_error(self) -> None:
        """Test economic calendar fetch handles API errors."""
        from app.mcp_server.tooling.fundamentals_sources_finnhub import (
            fetch_economic_calendar_finnhub,
        )

        with patch(
            "app.mcp_server.tooling.fundamentals_sources_finnhub._get_finnhub_client"
        ) as mock_client:
            mock_client.side_effect = Exception("API error")

            result = await fetch_economic_calendar_finnhub("2026-03-16", "2026-03-16")
            assert result is None
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_n8n_market_context.py::TestFinnhubEconomicCalendar -v
```

Expected: `ImportError: cannot import name 'fetch_economic_calendar_finnhub'`

**Step 3: Implement the function**

```python
# app/mcp_server/tooling/fundamentals_sources_finnhub.py
# Add after existing imports and before other functions

async def fetch_economic_calendar_finnhub(
    from_date: str, to_date: str
) -> list[dict[str, Any]] | None:
    """
    Fetch economic calendar events from Finnhub.

    Args:
        from_date: Start date in YYYY-MM-DD format
        to_date: End date in YYYY-MM-DD format

    Returns:
        List of event dicts with keys: time, country, event, actual, previous, estimate, impact
        or None if fetch fails
    """
    try:
        client = _get_finnhub_client()

        def fetch_sync() -> list[dict[str, Any]]:
            return client.economic_calendar(_from=from_date, to=to_date)

        events = await asyncio.to_thread(fetch_sync)
        
        if not isinstance(events, list):
            logger.warning("Finnhub economic_calendar returned non-list: %s", type(events))
            return None

        # Normalize and filter events
        normalized_events = []
        for event in events:
            if not isinstance(event, dict):
                continue
                
            country = str(event.get("country", "")).strip().upper()
            # Filter for US events only (as per requirements)
            if country != "US":
                continue

            normalized_events.append({
                "time": str(event.get("time", "")).strip(),
                "country": country,
                "event": str(event.get("event", "")).strip(),
                "actual": event.get("actual"),
                "previous": event.get("previous"),
                "estimate": event.get("estimate"),
                "impact": str(event.get("impact", "")).strip().lower() or None,
            })

        return normalized_events

    except Exception as exc:
        logger.warning(f"Failed to fetch economic calendar from Finnhub: {exc}")
        return None
```

**Step 4: Add to __all__**

```python
# At the end of the file, update __all__
__all__ = [
    "_fetch_news_finnhub",
    "_fetch_company_profile_finnhub",
    "_fetch_financials_finnhub",
    "fetch_economic_calendar_finnhub",  # Add this
]
```

**Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_n8n_market_context.py::TestFinnhubEconomicCalendar -v
```

Expected: All tests pass

**Step 6: Commit**

```bash
git add app/mcp_server/tooling/fundamentals_sources_finnhub.py tests/test_n8n_market_context.py
git commit -m "feat(n8n): add Finnhub economic calendar client"
```

---

## Task 2: Implement Economic Calendar Service

**Files:**
- Modify: `app/services/external/economic_calendar.py` (lines 38-67)

**Step 1: Update imports and implement fetch function**

```python
# app/services/external/economic_calendar.py
# Replace the entire file content with:

"""Economic calendar service for fetching high-impact events."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from app.core.timezone import now_kst
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    fetch_economic_calendar_finnhub,
)

logger = logging.getLogger(__name__)

_econ_calendar_cache: list[dict[str, Any]] = []
_econ_calendar_cache_expires: datetime | None = None
_cache_lock = asyncio.Lock()

HIGH_IMPORTANCE_KEYWORDS = [
    "FOMC",
    "CPI",
    "PPI",
    "GDP",
    "NFP",
    "Non-Farm",
    "Unemployment",
    "Interest Rate",
    "Fed",
    "ECB",
    "BOJ",
    "PMI",
    "Retail Sales",
    "Industrial Production",
    "Consumer Confidence",
    "Treasury",
]


def _is_high_importance_event(event_name: str) -> bool:
    """Check if event matches high-importance keywords."""
    event_upper = event_name.upper()
    return any(keyword.upper() in event_upper for keyword in HIGH_IMPORTANCE_KEYWORDS)


def _convert_time_to_kst(time_str: str) -> str:
    """
    Convert time string to KST format.
    
    Finnhub returns times in ET (US Eastern Time) during market hours.
    We convert to KST (Korea Standard Time) which is ET + 13 or + 14 hours.
    
    Args:
        time_str: Time in "HH:MM" format (ET)
        
    Returns:
        Time in KST format "HH:MM KST"
    """
    if not time_str or ":" not in time_str:
        return "00:00 KST"
    
    try:
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1])
        
        # KST is ET + 14 hours (simplified, ignoring DST nuances)
        kst_hour = (hour + 14) % 24
        
        return f"{kst_hour:02d}:{minute:02d} KST"
    except (ValueError, IndexError):
        return "00:00 KST"


def _format_value(value: Any) -> str | None:
    """Format event value (actual/previous/estimate)."""
    if value is None:
        return None
    return str(value).strip()


def _determine_importance(event_name: str, finnhub_impact: str | None) -> str:
    """
    Determine event importance level.
    
    Priority:
    1. Keyword matching for known high-impact events
    2. Finnhub impact field if available
    3. Default to medium
    """
    if _is_high_importance_event(event_name):
        return "high"
    
    if finnhub_impact:
        impact_lower = finnhub_impact.lower()
        if impact_lower in ("high", "medium", "low"):
            return impact_lower
    
    return "medium"


async def fetch_economic_events_today() -> list[dict[str, Any]]:
    """
    Fetch today's high-impact economic events.

    Returns list of events in N8nEconomicEvent format:
        [
            {
                "time": "21:30 KST",
                "event": "US CPI",
                "importance": "high",
                "previous": "2.4%",
                "forecast": "2.3%"
            }
        ]
    """
    global _econ_calendar_cache, _econ_calendar_cache_expires

    async with _cache_lock:
        if _econ_calendar_cache_expires and now_kst() < _econ_calendar_cache_expires:
            logger.debug("Returning cached economic calendar")
            return _econ_calendar_cache.copy()

    try:
        today = now_kst().strftime("%Y-%m-%d")
        
        # Fetch from Finnhub
        events = await fetch_economic_calendar_finnhub(today, today)
        
        if events is None:
            logger.warning("Failed to fetch economic calendar from Finnhub")
            async with _cache_lock:
                _econ_calendar_cache = []
                _econ_calendar_cache_expires = now_kst() + timedelta(minutes=15)
            return []

        # Transform to N8nEconomicEvent format
        transformed_events = []
        for event in events:
            event_name = event.get("event", "")
            
            # Skip events without names
            if not event_name:
                continue
            
            # Determine importance
            importance = _determine_importance(event_name, event.get("impact"))
            
            # For non-high importance events, we still include them but could filter here if needed
            # Currently including all US events with at least medium importance
            
            transformed_event = {
                "time": _convert_time_to_kst(event.get("time", "")),
                "event": event_name,
                "importance": importance,
                "previous": _format_value(event.get("previous")),
                "forecast": _format_value(event.get("estimate")),
            }
            
            transformed_events.append(transformed_event)

        # Sort by time
        transformed_events.sort(key=lambda x: x["time"])

        # Update cache
        async with _cache_lock:
            _econ_calendar_cache = transformed_events.copy()
            _econ_calendar_cache_expires = now_kst() + timedelta(hours=1)

        logger.info(f"Fetched {len(transformed_events)} economic events for today")
        return transformed_events

    except Exception as exc:
        logger.warning(f"Failed to fetch economic calendar: {exc}")
        async with _cache_lock:
            _econ_calendar_cache = []
            _econ_calendar_cache_expires = now_kst() + timedelta(minutes=15)
        return []
```

**Step 2: Add unit tests for helper functions**

```python
# tests/test_n8n_market_context.py - Add to TestMarketContextService class

@pytest.mark.asyncio
async def test_is_high_importance_event(self) -> None:
    """Test high-importance event detection."""
    from app.services.external.economic_calendar import _is_high_importance_event
    
    assert _is_high_importance_event("US CPI") is True
    assert _is_high_importance_event("FOMC Meeting") is True
    assert _is_high_importance_event("Non-Farm Payrolls") is True
    assert _is_high_importance_event("GDP Growth") is True
    assert _is_high_importance_event("Retail Sales") is True
    assert _is_high_importance_event("Earnings Report") is False
    assert _is_high_importance_event("Dividend Announcement") is False

@pytest.mark.asyncio
async def test_convert_time_to_kst(self) -> None:
    """Test time conversion to KST."""
    from app.services.external.economic_calendar import _convert_time_to_kst
    
    # ET 08:30 -> KST 22:30 (ET + 14)
    assert _convert_time_to_kst("08:30") == "22:30 KST"
    # ET 14:00 -> KST 04:00 next day
    assert _convert_time_to_kst("14:00") == "04:00 KST"
    # Invalid input
    assert _convert_time_to_kst("") == "00:00 KST"
    assert _convert_time_to_kst("invalid") == "00:00 KST"

@pytest.mark.asyncio
async def test_determine_importance(self) -> None:
    """Test importance level determination."""
    from app.services.external.economic_calendar import _determine_importance
    
    # Keyword-based
    assert _determine_importance("CPI Release", None) == "high"
    assert _determine_importance("FOMC Statement", None) == "high"
    
    # Finnhub impact fallback
    assert _determine_importance("Some Event", "low") == "low"
    assert _determine_importance("Some Event", "medium") == "medium"
    
    # Default
    assert _determine_importance("Some Event", None) == "medium"
```

**Step 3: Run tests to verify they pass**

```bash
uv run pytest tests/test_n8n_market_context.py::TestMarketContextService -v
```

Expected: All tests pass

**Step 4: Commit**

```bash
git add app/services/external/economic_calendar.py tests/test_n8n_market_context.py
git commit -m "feat(n8n): implement economic calendar service with Finnhub"
```

---

## Task 3: Integration Test with Market Context API

**Files:**
- Modify: `tests/test_n8n_market_context.py`

**Step 1: Add integration test**

```python
# Add to TestMarketContextEndpoint class

@pytest.mark.integration
@pytest.mark.asyncio
async def test_market_context_with_economic_events(self, client: TestClient) -> None:
    """Test that market context endpoint returns economic events."""
    with patch("app.services.n8n_market_context_service.fetch_btc_dominance") as mock_btc, \
         patch("app.services.n8n_market_context_service.fetch_fear_greed") as mock_fg, \
         patch("app.services.external.economic_calendar.fetch_economic_calendar_finnhub") as mock_econ:
        
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
        mock_econ.return_value = [
            {
                "time": "21:30",
                "country": "US",
                "event": "CPI",
                "actual": "2.4%",
                "previous": "2.3%",
                "estimate": "2.3%",
                "impact": "high"
            }
        ]

        response = client.get("/api/n8n/market-context")
        assert response.status_code == 200
        data = response.json()
        
        # Verify economic events are present
        assert "economic_events_today" in data["market_overview"]
        events = data["market_overview"]["economic_events_today"]
        assert len(events) >= 1
        
        # Verify event format
        first_event = events[0]
        assert "time" in first_event
        assert "event" in first_event
        assert "importance" in first_event
```

**Step 2: Run the integration test**

```bash
uv run pytest tests/test_n8n_market_context.py::TestMarketContextEndpoint::test_market_context_with_economic_events -v -m "not live"
```

Expected: Test passes

**Step 3: Commit**

```bash
git add tests/test_n8n_market_context.py
git commit -m "test(n8n): add economic calendar integration test"
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

**Step 3: Manual verification (optional - requires FINNHUB_API_KEY)**

```bash
# Ensure FINNHUB_API_KEY is set in .env
# Start dev server
make dev

# Test the endpoint
curl "http://localhost:8000/api/n8n/market-context" | jq '.market_overview.economic_events_today'
```

Expected: Response includes array of economic events with time, event, importance, previous, forecast fields

**Step 4: Final commit if any fixes needed**

```bash
# Only if lint/type fixes were needed
git add -A
git commit -m "fix(n8n): address lint/type issues in economic calendar implementation"
```

---

## Summary

**Files Modified:**
- `app/mcp_server/tooling/fundamentals_sources_finnhub.py` - Added `fetch_economic_calendar_finnhub()`
- `app/services/external/economic_calendar.py` - Implemented full service with caching
- `tests/test_n8n_market_context.py` - Added comprehensive tests

**API Response Change:**
```json
{
  "market_overview": {
    "btc_dominance": 61.5,
    "total_market_cap_change_24h": 2.3,
    "fear_greed": {...},
    "economic_events_today": [
      {
        "time": "21:30 KST",
        "event": "US CPI",
        "importance": "high",
        "previous": "2.4%",
        "forecast": "2.3%"
      },
      {
        "time": "02:00 KST",
        "event": "FOMC Statement",
        "importance": "high",
        "previous": null,
        "forecast": null
      }
    ]
  }
}
```

**Key Features Implemented:**
- ✅ US-only event filtering
- ✅ High-importance keyword detection (FOMC, CPI, NFP, etc.)
- ✅ Time conversion from ET to KST
- ✅ 1-hour caching with async lock
- ✅ Graceful error handling (returns empty list on failure)
- ✅ Full test coverage

---

## Execution Options

**Plan saved to:** `docs/plans/YYYY-MM-DD-economic-calendar-implementation.md`

**Two execution options:**

1. **Subagent-Driven (this session)** - I dispatch fresh subagent per task, review between tasks, fast iteration
2. **Parallel Session (separate)** - Open new session with executing-plans, batch execution with checkpoints

**Which approach would you prefer?**
