# Split Fundamentals MCP Handlers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `fundamentals_handlers.py` (932 lines) into domain-focused modules so each tool's implementation lives in its own file, the registration function becomes a thin dispatcher, and market normalization duplication is eliminated.

**Architecture:** Extract each tool handler's body into a standalone async function in `app/mcp_server/tooling/fundamentals/<domain>.py`. Keep `fundamentals_handlers.py` as the single registration point that imports handlers and wires them to `@mcp.tool`. Common market-normalization logic (repeated 6+ times) goes into `fundamentals/_helpers.py`.

**Tech Stack:** Python 3.13, FastMCP, pytest, existing source modules (naver/finnhub/coingecko/binance/indices)

---

## File Structure

```
app/mcp_server/tooling/
├── fundamentals_handlers.py          # MODIFY: slim to ~120 lines (registration only)
├── fundamentals_registration.py      # NO CHANGE (imports from fundamentals_handlers)
├── fundamentals/                     # CREATE: new package
│   ├── __init__.py                   # Re-exports for backward compat
│   ├── _helpers.py                   # Market normalization, shared constants
│   ├── _news.py                      # handle_get_news
│   ├── _profiles.py                  # handle_get_company_profile, handle_get_crypto_profile
│   ├── _financials.py                # handle_get_financials, handle_get_insider_transactions, handle_get_earnings_calendar
│   ├── _valuation.py                 # handle_get_valuation, handle_get_investment_opinions, handle_get_investor_trends, handle_get_short_interest
│   ├── _crypto.py                    # handle_get_kimchi_premium, handle_get_funding_rate
│   ├── _market_index.py              # handle_get_market_index
│   ├── _support_resistance.py        # get_support_resistance_impl (+ _DEFAULT sentinel)
│   └── _sector_peers.py              # handle_get_sector_peers
tests/
├── _mcp_tooling_support.py           # MODIFY: add fundamentals submodules to _PATCH_MODULES
```

**Unchanged files:** `fundamentals_registration.py`, `shared.py`, `market_data_quotes.py`, order files, `fundamentals_sources_*.py`, `market_data_indicators.py`

**Public contract preserved:**
- `fundamentals_handlers.FUNDAMENTALS_TOOL_NAMES` — stays in `fundamentals_handlers.py`
- `fundamentals_handlers._register_fundamentals_tools_impl` — stays, becomes thin
- `fundamentals_handlers._get_support_resistance_impl` — re-exported from `fundamentals/_support_resistance.py`
- All tool names, parameters, descriptions, return types — identical

---

## Common Patterns

### Market normalization (extracted to `_helpers.py`)

The following block appears 6+ times with slight variations:
```python
normalized_market = market.strip().lower()
if normalized_market in ("kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr", "naver"):
    normalized_market = "kr"
elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
    normalized_market = "us"
else:
    raise ValueError(...)
```

Extracted as two helpers:
- `normalize_equity_market(market: str) -> str` — returns `"kr"` or `"us"`, raises otherwise
- `normalize_market_with_crypto(market: str) -> str` — returns `"kr"`, `"us"`, or `"crypto"`
- `detect_equity_market(symbol: str, market: str | None) -> str` — auto-detects from symbol when market is None

### Handler function naming

All extracted handler functions follow: `handle_<tool_name>(...)` — e.g., `handle_get_news(symbol, market, limit)`.

### Error wrapping

Each handler keeps its existing try/except + `_error_payload()` call. No new abstraction.

---

### Task 1: Verify baseline tests pass

**Files:** (none modified)

- [ ] **Step 1: Run fundamentals tests**

```bash
cd /Users/robin/.superset/worktrees/auto_trader/split-fundamentals-handlers
uv run pytest tests/test_mcp_fundamentals_tools.py -q
```

Expected: All tests pass.

- [ ] **Step 2: Run indicator tests**

```bash
uv run pytest tests/test_mcp_indicator_tools.py -q
```

Expected: All tests pass.

---

### Task 2: Create `fundamentals/__init__.py` and `fundamentals/_helpers.py`

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/__init__.py`
- Create: `app/mcp_server/tooling/fundamentals/_helpers.py`

- [ ] **Step 1: Write tests for market normalization helpers**

Create `tests/test_fundamentals_helpers.py`:

```python
"""Tests for fundamentals helpers (market normalization)."""

import pytest

from app.mcp_server.tooling.fundamentals._helpers import (
    detect_equity_market,
    normalize_equity_market,
    normalize_market_with_crypto,
)


class TestNormalizeEquityMarket:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("kr", "kr"),
            ("KR", "kr"),
            ("krx", "kr"),
            ("korea", "kr"),
            ("kospi", "kr"),
            ("kosdaq", "kr"),
            ("kis", "kr"),
            ("equity_kr", "kr"),
            ("naver", "kr"),
            ("us", "us"),
            ("USA", "us"),
            ("nyse", "us"),
            ("nasdaq", "us"),
            ("yahoo", "us"),
            ("equity_us", "us"),
        ],
    )
    def test_valid_markets(self, raw: str, expected: str) -> None:
        assert normalize_equity_market(raw) == expected

    @pytest.mark.parametrize("raw", ["crypto", "upbit", "invalid", ""])
    def test_invalid_markets(self, raw: str) -> None:
        with pytest.raises(ValueError, match="market must be"):
            normalize_equity_market(raw)


class TestNormalizeMarketWithCrypto:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("kr", "kr"),
            ("us", "us"),
            ("crypto", "crypto"),
            ("upbit", "crypto"),
            ("krw", "crypto"),
            ("usdt", "crypto"),
        ],
    )
    def test_valid_markets(self, raw: str, expected: str) -> None:
        assert normalize_market_with_crypto(raw) == expected

    def test_invalid_market(self) -> None:
        with pytest.raises(ValueError, match="market must be"):
            normalize_market_with_crypto("invalid")


class TestDetectEquityMarket:
    def test_korean_code(self) -> None:
        assert detect_equity_market("005930", None) == "kr"

    def test_us_symbol(self) -> None:
        assert detect_equity_market("AAPL", None) == "us"

    def test_explicit_market_overrides(self) -> None:
        assert detect_equity_market("AAPL", "kr") == "kr"

    def test_crypto_raises(self) -> None:
        with pytest.raises(ValueError, match="not available for crypto"):
            detect_equity_market("KRW-BTC", None)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_fundamentals_helpers.py -q
```

Expected: FAIL — module not found.

- [ ] **Step 3: Create the package and helpers module**

Create `app/mcp_server/tooling/fundamentals/__init__.py`:

```python
"""Fundamentals tool handler sub-package."""
```

Create `app/mcp_server/tooling/fundamentals/_helpers.py`:

```python
"""Shared helpers for fundamentals tool handlers."""

from __future__ import annotations

from app.mcp_server.tooling.shared import (
    is_crypto_market as _is_crypto_market,
    is_korean_equity_code as _is_korean_equity_code,
)

# Alias sets for market string normalization
_KR_ALIASES = frozenset(
    {"kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr", "naver"}
)
_US_ALIASES = frozenset({"us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"})
_CRYPTO_ALIASES = frozenset({"crypto", "upbit", "krw", "usdt"})


def normalize_equity_market(market: str) -> str:
    """Normalize a market string to 'kr' or 'us'. Raises ValueError otherwise."""
    m = market.strip().lower()
    if m in _KR_ALIASES:
        return "kr"
    if m in _US_ALIASES:
        return "us"
    raise ValueError("market must be 'us' or 'kr'")


def normalize_market_with_crypto(market: str) -> str:
    """Normalize a market string to 'kr', 'us', or 'crypto'. Raises ValueError otherwise."""
    m = market.strip().lower()
    if m in _CRYPTO_ALIASES:
        return "crypto"
    if m in _KR_ALIASES:
        return "kr"
    if m in _US_ALIASES:
        return "us"
    raise ValueError("market must be 'us', 'kr', or 'crypto'")


def detect_equity_market(symbol: str, market: str | None) -> str:
    """Auto-detect equity market from symbol, or normalize explicit market.

    Returns 'kr' or 'us'. Raises ValueError for crypto symbols.
    """
    if market is not None:
        return normalize_equity_market(market)
    if _is_crypto_market(symbol):
        raise ValueError(
            "not available for cryptocurrencies"
        )
    if _is_korean_equity_code(symbol):
        return "kr"
    return "us"
```

- [ ] **Step 4: Run helper tests to verify they pass**

```bash
uv run pytest tests/test_fundamentals_helpers.py -q
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/__init__.py app/mcp_server/tooling/fundamentals/_helpers.py tests/test_fundamentals_helpers.py
git commit -m "refactor: add fundamentals helpers package with market normalization"
```

---

### Task 3: Extract news handler to `fundamentals/_news.py`

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/_news.py`
- Reference: `fundamentals_handlers.py:229-283` (get_news body)

- [ ] **Step 1: Create `_news.py` with handler extracted from `fundamentals_handlers.py:229-283`**

```python
"""Handler for get_news tool."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import normalize_market_with_crypto
from app.mcp_server.tooling.fundamentals_sources_finnhub import _fetch_news_finnhub
from app.mcp_server.tooling.fundamentals_sources_naver import _fetch_news_naver
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
    is_crypto_market as _is_crypto_market,
    is_korean_equity_code as _is_korean_equity_code,
    normalize_symbol_input as _normalize_symbol_input,
)


async def handle_get_news(
    symbol: str | int,
    market: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        elif _is_crypto_market(symbol):
            market = "crypto"
        else:
            market = "us"

    normalized_market = normalize_market_with_crypto(market)
    capped_limit = min(max(limit, 1), 50)

    try:
        if normalized_market == "kr":
            return await _fetch_news_naver(symbol, capped_limit)
        return await _fetch_news_finnhub(symbol, normalized_market, capped_limit)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "finnhub"
        instrument_type = {
            "kr": "equity_kr",
            "us": "equity_us",
            "crypto": "crypto",
        }.get(normalized_market, "equity_us")
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )
```

- [ ] **Step 2: Run existing fundamentals tests to verify no breakage**

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py -q
```

Expected: All pass (new file not wired in yet, old code untouched).

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/_news.py
git commit -m "refactor: extract get_news handler to fundamentals/_news.py"
```

---

### Task 4: Extract profile handlers to `fundamentals/_profiles.py`

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/_profiles.py`
- Reference: `fundamentals_handlers.py:292-371` (get_company_profile + get_crypto_profile)

- [ ] **Step 1: Create `_profiles.py`**

```python
"""Handlers for get_company_profile and get_crypto_profile tools."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import (
    detect_equity_market,
    normalize_equity_market,
)
from app.mcp_server.tooling.fundamentals_sources_coingecko import (
    _fetch_coingecko_coin_profile,
    _map_coingecko_profile_to_output,
    _normalize_crypto_base_symbol,
    _resolve_coingecko_coin_id,
)
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_company_profile_finnhub,
)
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_company_profile_naver,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
    is_crypto_market as _is_crypto_market,
    is_korean_equity_code as _is_korean_equity_code,
)


async def handle_get_company_profile(
    symbol: str,
    market: str | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if _is_crypto_market(symbol):
        raise ValueError("Company profile is not available for cryptocurrencies")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        else:
            market = "us"

    normalized_market = normalize_equity_market(market)

    try:
        if normalized_market == "kr":
            return await _fetch_company_profile_naver(symbol)
        return await _fetch_company_profile_finnhub(symbol)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "finnhub"
        instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )


async def handle_get_crypto_profile(symbol: str) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    normalized_symbol = _normalize_crypto_base_symbol(symbol)
    if not normalized_symbol:
        raise ValueError("symbol is required")

    try:
        coin_id = await _resolve_coingecko_coin_id(normalized_symbol)
        profile = await _fetch_coingecko_coin_profile(coin_id)
        result = _map_coingecko_profile_to_output(profile)
        if result.get("symbol") is None:
            result["symbol"] = normalized_symbol
        if result.get("name") is None:
            result["name"] = normalized_symbol
        return result
    except Exception as exc:
        return _error_payload(
            source="coingecko",
            message=str(exc),
            symbol=normalized_symbol,
            instrument_type="crypto",
        )
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py -q
```

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/_profiles.py
git commit -m "refactor: extract profile handlers to fundamentals/_profiles.py"
```

---

### Task 5: Extract financial handlers to `fundamentals/_financials.py`

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/_financials.py`
- Reference: `fundamentals_handlers.py:380-515` (get_financials, get_insider_transactions, get_earnings_calendar)

- [ ] **Step 1: Create `_financials.py`**

```python
"""Handlers for get_financials, get_insider_transactions, get_earnings_calendar tools."""

from __future__ import annotations

import datetime
from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import normalize_equity_market
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_earnings_calendar_finnhub,
    _fetch_financials_finnhub,
    _fetch_insider_transactions_finnhub,
)
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_financials_naver,
    _fetch_financials_yfinance,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
    is_crypto_market as _is_crypto_market,
    is_korean_equity_code as _is_korean_equity_code,
)


async def handle_get_financials(
    symbol: str,
    statement: str = "income",
    freq: str = "annual",
    market: str | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    statement = (statement or "income").strip().lower()
    if statement not in ("income", "balance", "cashflow"):
        raise ValueError("statement must be 'income', 'balance', or 'cashflow'")

    freq = (freq or "annual").strip().lower()
    if freq not in ("annual", "quarterly"):
        raise ValueError("freq must be 'annual' or 'quarterly'")

    if _is_crypto_market(symbol):
        raise ValueError(
            "Financial statements are not available for cryptocurrencies"
        )

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        else:
            market = "us"

    normalized_market = normalize_equity_market(market)

    try:
        if normalized_market == "kr":
            return await _fetch_financials_naver(symbol, statement, freq)
        try:
            return await _fetch_financials_finnhub(symbol, statement, freq)
        except (ValueError, Exception):
            return await _fetch_financials_yfinance(symbol, statement, freq)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "yfinance"
        instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )


async def handle_get_insider_transactions(
    symbol: str,
    limit: int = 20,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    capped_limit = min(max(limit, 1), 100)

    if _is_crypto_market(symbol):
        raise ValueError("Insider transactions are only available for US stocks")
    if _is_korean_equity_code(symbol):
        raise ValueError("Insider transactions are only available for US stocks")

    try:
        return await _fetch_insider_transactions_finnhub(symbol, capped_limit)
    except Exception as exc:
        return _error_payload(
            source="finnhub",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_us",
        )


async def handle_get_earnings_calendar(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip() if symbol else None

    if symbol:
        if _is_crypto_market(symbol):
            raise ValueError("Earnings calendar is only available for US stocks")
        if _is_korean_equity_code(symbol):
            raise ValueError("Earnings calendar is only available for US stocks")

    if from_date:
        try:
            datetime.date.fromisoformat(from_date)
        except ValueError:
            raise ValueError("from_date must be ISO format (e.g., '2024-01-15')")

    if to_date:
        try:
            datetime.date.fromisoformat(to_date)
        except ValueError:
            raise ValueError("to_date must be ISO format (e.g., '2024-01-15')")

    try:
        return await _fetch_earnings_calendar_finnhub(symbol, from_date, to_date)
    except Exception as exc:
        return _error_payload(
            source="finnhub",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_us",
        )
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py -q
```

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/_financials.py
git commit -m "refactor: extract financial handlers to fundamentals/_financials.py"
```

---

### Task 6: Extract valuation-group handlers to `fundamentals/_valuation.py`

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/_valuation.py`
- Reference: `fundamentals_handlers.py:524-701` (get_investor_trends, get_investment_opinions, get_valuation, get_short_interest)

- [ ] **Step 1: Create `_valuation.py`**

```python
"""Handlers for valuation and equity-analysis tools.

Includes: get_valuation, get_investment_opinions, get_investor_trends, get_short_interest.
"""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import normalize_equity_market
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_investment_opinions_naver,
    _fetch_investment_opinions_yfinance,
    _fetch_investor_trends_naver,
    _fetch_valuation_naver,
    _fetch_valuation_yfinance,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
    is_crypto_market as _is_crypto_market,
    is_korean_equity_code as _is_korean_equity_code,
    normalize_symbol_input as _normalize_symbol_input,
)
from app.services import market_data as market_data_service


async def handle_get_valuation(
    symbol: str | int,
    market: str | None = None,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    if _is_crypto_market(symbol):
        raise ValueError("Valuation metrics are not available for cryptocurrencies")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        else:
            market = "us"

    normalized_market = normalize_equity_market(market)

    try:
        if normalized_market == "kr":
            return await _fetch_valuation_naver(symbol)
        return await _fetch_valuation_yfinance(symbol)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "yfinance"
        instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )


async def handle_get_investment_opinions(
    symbol: str | int,
    limit: int = 10,
    market: str | None = None,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    if _is_crypto_market(symbol):
        raise ValueError(
            "Investment opinions are not available for cryptocurrencies"
        )

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        else:
            market = "us"

    if not market:
        raise ValueError("market is required")

    normalized_market = normalize_equity_market(str(market))
    capped_limit = min(max(limit, 1), 30)

    try:
        if normalized_market == "kr":
            return await _fetch_investment_opinions_naver(symbol, capped_limit)
        return await _fetch_investment_opinions_yfinance(symbol, capped_limit)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "yfinance"
        instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )


async def handle_get_investor_trends(
    symbol: str,
    days: int = 20,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if not _is_korean_equity_code(symbol):
        raise ValueError(
            "Investor trends are only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    capped_days = min(max(days, 1), 60)

    try:
        return await _fetch_investor_trends_naver(symbol, capped_days)
    except Exception as exc:
        return _error_payload(
            source="naver",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )


async def handle_get_short_interest(
    symbol: str,
    days: int = 20,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if not _is_korean_equity_code(symbol):
        raise ValueError(
            "Short selling data is only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    capped_days = min(max(days, 1), 60)

    try:
        return await market_data_service.get_short_interest(symbol, capped_days)
    except Exception as exc:
        return _error_payload(
            source="kis",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py -q
```

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/_valuation.py
git commit -m "refactor: extract valuation-group handlers to fundamentals/_valuation.py"
```

---

### Task 7: Extract crypto handlers to `fundamentals/_crypto.py`

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/_crypto.py`
- Reference: `fundamentals_handlers.py:710-775` (get_kimchi_premium, get_funding_rate)

- [ ] **Step 1: Create `_crypto.py`**

```python
"""Handlers for get_kimchi_premium and get_funding_rate tools."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals_sources_binance import (
    _fetch_funding_rate,
    _fetch_funding_rate_batch,
)
from app.mcp_server.tooling.fundamentals_sources_coingecko import (
    _normalize_crypto_base_symbol,
    _resolve_batch_crypto_symbols,
)
from app.mcp_server.tooling.fundamentals_sources_naver import _fetch_kimchi_premium
from app.mcp_server.tooling.shared import error_payload as _error_payload


async def handle_get_kimchi_premium(
    symbol: str | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    try:
        if symbol:
            sym = _normalize_crypto_base_symbol(symbol)
            if not sym:
                raise ValueError("symbol is required")
            symbols = [sym]
            return await _fetch_kimchi_premium(symbols)

        symbols = await _resolve_batch_crypto_symbols()
        payload = await _fetch_kimchi_premium(symbols)
        rows: list[dict[str, Any]] = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "symbol": item.get("symbol"),
                    "upbit_price": item.get("upbit_krw"),
                    "binance_price": item.get("binance_usdt"),
                    "premium_pct": item.get("premium_pct"),
                }
            )
        return rows
    except Exception as exc:
        return _error_payload(
            source="upbit+binance",
            message=str(exc),
            instrument_type="crypto",
        )


async def handle_get_funding_rate(
    symbol: str | None = None,
    limit: int = 10,
) -> dict[str, Any] | list[dict[str, Any]]:
    if symbol is not None and not symbol.strip():
        raise ValueError("symbol is required")

    try:
        if symbol is None:
            symbols = await _resolve_batch_crypto_symbols()
            return await _fetch_funding_rate_batch(symbols)

        normalized_symbol = _normalize_crypto_base_symbol(symbol)
        if not normalized_symbol:
            raise ValueError("symbol is required")

        capped_limit = min(max(limit, 1), 100)
        return await _fetch_funding_rate(normalized_symbol, capped_limit)
    except Exception as exc:
        normalized_symbol = _normalize_crypto_base_symbol(symbol or "")
        return _error_payload(
            source="binance",
            message=str(exc),
            symbol=f"{normalized_symbol}USDT" if normalized_symbol else None,
            instrument_type="crypto",
        )
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py -q
```

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/_crypto.py
git commit -m "refactor: extract crypto handlers to fundamentals/_crypto.py"
```

---

### Task 8: Extract market index handler to `fundamentals/_market_index.py`

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/_market_index.py`
- Reference: `fundamentals_handlers.py:784-845` (get_market_index)

- [ ] **Step 1: Create `_market_index.py`**

```python
"""Handler for get_market_index tool."""

from __future__ import annotations

import asyncio
from typing import Any

from app.mcp_server.tooling.fundamentals_sources_indices import (
    _DEFAULT_INDICES,
    _INDEX_META,
    _fetch_index_kr_current,
    _fetch_index_kr_history,
    _fetch_index_us_current,
    _fetch_index_us_history,
)
from app.mcp_server.tooling.shared import error_payload as _error_payload


async def handle_get_market_index(
    symbol: str | None = None,
    period: str = "day",
    count: int = 20,
) -> dict[str, Any]:
    period = (period or "day").strip().lower()
    if period not in ("day", "week", "month"):
        raise ValueError("period must be 'day', 'week', or 'month'")

    capped_count = min(max(count, 1), 100)

    if symbol:
        sym = symbol.strip().upper()
        meta = _INDEX_META.get(sym)
        if meta is None:
            raise ValueError(
                f"Unknown index symbol '{sym}'. Supported: {', '.join(sorted(_INDEX_META))}"
            )

        try:
            if meta["source"] == "naver":
                current_data, history = await asyncio.gather(
                    _fetch_index_kr_current(meta["naver_code"], meta["name"]),
                    _fetch_index_kr_history(
                        meta["naver_code"], capped_count, period
                    ),
                )
            else:
                current_data, history = await asyncio.gather(
                    _fetch_index_us_current(meta["yf_ticker"], meta["name"], sym),
                    _fetch_index_us_history(
                        meta["yf_ticker"], capped_count, period
                    ),
                )
            return {"indices": [current_data], "history": history}
        except Exception as exc:
            return _error_payload(
                source=meta["source"], message=str(exc), symbol=sym
            )

    tasks = []
    for idx_sym in _DEFAULT_INDICES:
        meta = _INDEX_META[idx_sym]
        if meta["source"] == "naver":
            tasks.append(_fetch_index_kr_current(meta["naver_code"], meta["name"]))
        else:
            tasks.append(
                _fetch_index_us_current(meta["yf_ticker"], meta["name"], idx_sym)
            )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    indices: list[dict[str, Any]] = []
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            indices.append({"symbol": _DEFAULT_INDICES[i], "error": str(r)})
        elif isinstance(r, dict):
            indices.append(r)
        else:
            indices.append({"symbol": _DEFAULT_INDICES[i], "error": str(r)})

    return {"indices": indices}
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py -q
```

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/_market_index.py
git commit -m "refactor: extract market index handler to fundamentals/_market_index.py"
```

---

### Task 9: Extract support/resistance handler to `fundamentals/_support_resistance.py`

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/_support_resistance.py`
- Reference: `fundamentals_handlers.py:105-218` (_get_support_resistance_impl)

- [ ] **Step 1: Create `_support_resistance.py`**

```python
"""Handler for get_support_resistance tool."""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.mcp_server.tooling.market_data_indicators import (
    _calculate_fibonacci,
    _calculate_volume_profile,
    _cluster_price_levels,
    _compute_indicators,
    _fetch_ohlcv_for_indicators,
    _format_fibonacci_source,
    _split_support_resistance_levels,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
    resolve_market_type as _resolve_market_type,
    to_optional_float as _to_optional_float,
)


async def get_support_resistance_impl(
    symbol: str,
    market: str | None = None,
    preloaded_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Get support/resistance zones from multi-indicator clustering."""

    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    market_type, normalized_symbol = _resolve_market_type(symbol, market)
    source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
    source = source_map[market_type]

    try:
        if preloaded_df is not None and not preloaded_df.empty:
            df = preloaded_df
        else:
            df = await _fetch_ohlcv_for_indicators(
                normalized_symbol, market_type, count=60
            )
        if df.empty:
            raise ValueError(f"No data available for symbol '{normalized_symbol}'")

        for col in ("high", "low", "close"):
            if col not in df.columns:
                raise ValueError(f"Missing required column: {col}")

        current_price = round(float(df["close"].iloc[-1]), 2)
        fib_result = _calculate_fibonacci(df, current_price)
        fib_result["symbol"] = normalized_symbol

        volume_result = _calculate_volume_profile(df, bins=20)
        volume_result["symbol"] = normalized_symbol
        volume_result["period_days"] = 60

        indicator_result = _compute_indicators(df, ["bollinger"])

        if not fib_result.get("levels"):
            raise ValueError("Failed to calculate Fibonacci levels")
        if current_price <= 0:
            raise ValueError("failed to resolve current price")

        price_levels: list[tuple[float, str]] = []

        fib_levels = fib_result.get("levels", {})
        if isinstance(fib_levels, dict):
            for level_key, price in fib_levels.items():
                level_price = _to_optional_float(price)
                if level_price is None or level_price <= 0:
                    continue
                price_levels.append(
                    (level_price, _format_fibonacci_source(str(level_key)))
                )

        poc_price = _to_optional_float((volume_result.get("poc") or {}).get("price"))
        if poc_price is not None and poc_price > 0:
            price_levels.append((poc_price, "volume_poc"))

        value_area = volume_result.get("value_area") or {}
        value_area_high = _to_optional_float(value_area.get("high"))
        value_area_low = _to_optional_float(value_area.get("low"))
        if value_area_high is not None and value_area_high > 0:
            price_levels.append((value_area_high, "volume_value_area_high"))
        if value_area_low is not None and value_area_low > 0:
            price_levels.append((value_area_low, "volume_value_area_low"))

        bollinger_raw = indicator_result.get("bollinger")
        if isinstance(bollinger_raw, dict):
            bollinger = bollinger_raw
        else:
            indicators_raw = indicator_result.get("indicators")
            if isinstance(indicators_raw, dict):
                nested_bollinger = indicators_raw.get("bollinger")
                bollinger = (
                    nested_bollinger if isinstance(nested_bollinger, dict) else {}
                )
            else:
                bollinger = {}
        bb_upper = _to_optional_float(bollinger.get("upper"))
        bb_middle = _to_optional_float(bollinger.get("middle"))
        bb_lower = _to_optional_float(bollinger.get("lower"))
        if bb_upper is not None and bb_upper > 0:
            price_levels.append((bb_upper, "bb_upper"))
        if bb_middle is not None and bb_middle > 0:
            price_levels.append((bb_middle, "bb_middle"))
        if bb_lower is not None and bb_lower > 0:
            price_levels.append((bb_lower, "bb_lower"))

        clustered_levels = _cluster_price_levels(price_levels, tolerance_pct=0.02)
        supports, resistances = _split_support_resistance_levels(
            clustered_levels,
            current_price,
        )

        return {
            "symbol": normalized_symbol,
            "current_price": round(current_price, 2),
            "supports": supports,
            "resistances": resistances,
        }
    except Exception as exc:
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=normalized_symbol,
            instrument_type=market_type,
        )


_DEFAULT_GET_SUPPORT_RESISTANCE_IMPL = get_support_resistance_impl
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py -q
```

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/_support_resistance.py
git commit -m "refactor: extract support/resistance handler to fundamentals/_support_resistance.py"
```

---

### Task 10: Extract sector peers handler to `fundamentals/_sector_peers.py`

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/_sector_peers.py`
- Reference: `fundamentals_handlers.py:870-925` (get_sector_peers)

- [ ] **Step 1: Create `_sector_peers.py`**

```python
"""Handler for get_sector_peers tool."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_sector_peers_naver,
    _fetch_sector_peers_us,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
    is_crypto_market as _is_crypto_market,
    is_korean_equity_code as _is_korean_equity_code,
    is_us_equity_symbol as _is_us_equity_symbol,
)

_KR_ALIASES = frozenset({"kr", "krx", "korea", "kospi", "kosdaq", "kis", "naver"})
_US_ALIASES = frozenset({"us", "usa", "nyse", "nasdaq", "yahoo"})


async def handle_get_sector_peers(
    symbol: str,
    market: str = "",
    limit: int = 5,
    manual_peers: list[str] | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if _is_crypto_market(symbol):
        raise ValueError("Sector peers are not available for cryptocurrencies")

    capped_limit = min(max(limit, 1), 20)

    market_str = (market or "").strip().lower()
    if market_str in _KR_ALIASES:
        resolved_market = "kr"
    elif market_str in _US_ALIASES:
        resolved_market = "us"
    elif market_str == "":
        if _is_korean_equity_code(symbol):
            resolved_market = "kr"
        elif _is_us_equity_symbol(symbol):
            resolved_market = "us"
        else:
            raise ValueError(
                f"Cannot auto-detect market for symbol '{symbol}'. "
                "Please specify market='kr' or market='us'."
            )
    else:
        raise ValueError("market must be 'kr' or 'us'")

    try:
        if resolved_market == "kr":
            return await _fetch_sector_peers_naver(
                symbol, capped_limit, manual_peers
            )
        return await _fetch_sector_peers_us(symbol, capped_limit, manual_peers)
    except Exception as exc:
        source = "naver" if resolved_market == "kr" else "finnhub+yfinance"
        instrument_type = "equity_kr" if resolved_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )
```

Note: `get_sector_peers` has a slightly different market alias set (no `"equity_kr"`, `"equity_us"`) and custom auto-detect logic. This is preserved exactly.

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py -q
```

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/_sector_peers.py
git commit -m "refactor: extract sector peers handler to fundamentals/_sector_peers.py"
```

---

### Task 11: Rewrite `fundamentals_handlers.py` as thin registration

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py` (complete rewrite from ~932 → ~150 lines)
- Modify: `app/mcp_server/tooling/fundamentals/__init__.py` (re-exports)

This is the core task. Replace the 700-line `_register_fundamentals_tools_impl` with thin delegation to extracted handlers.

- [ ] **Step 1: Update `fundamentals/__init__.py` with re-exports**

```python
"""Fundamentals tool handler sub-package."""

from app.mcp_server.tooling.fundamentals._support_resistance import (
    get_support_resistance_impl as _get_support_resistance_impl,
)

__all__ = ["_get_support_resistance_impl"]
```

- [ ] **Step 2: Rewrite `fundamentals_handlers.py`**

Replace the entire file content with:

```python
"""Fundamentals tool handlers and registration implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.fundamentals._crypto import (
    handle_get_funding_rate,
    handle_get_kimchi_premium,
)
from app.mcp_server.tooling.fundamentals._financials import (
    handle_get_earnings_calendar,
    handle_get_financials,
    handle_get_insider_transactions,
)
from app.mcp_server.tooling.fundamentals._market_index import (
    handle_get_market_index,
)
from app.mcp_server.tooling.fundamentals._news import handle_get_news
from app.mcp_server.tooling.fundamentals._profiles import (
    handle_get_company_profile,
    handle_get_crypto_profile,
)
from app.mcp_server.tooling.fundamentals._sector_peers import (
    handle_get_sector_peers,
)
from app.mcp_server.tooling.fundamentals._support_resistance import (
    _DEFAULT_GET_SUPPORT_RESISTANCE_IMPL,
    get_support_resistance_impl as _get_support_resistance_impl,
)
from app.mcp_server.tooling.fundamentals._valuation import (
    handle_get_investment_opinions,
    handle_get_investor_trends,
    handle_get_short_interest,
    handle_get_valuation,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

FUNDAMENTALS_TOOL_NAMES: set[str] = {
    "get_news",
    "get_company_profile",
    "get_crypto_profile",
    "get_financials",
    "get_insider_transactions",
    "get_earnings_calendar",
    "get_investor_trends",
    "get_investment_opinions",
    "get_valuation",
    "get_short_interest",
    "get_kimchi_premium",
    "get_funding_rate",
    "get_market_index",
    "get_support_resistance",
    "get_sector_peers",
}


def _register_fundamentals_tools_impl(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_news",
        description=(
            "Get recent news for a stock or cryptocurrency. Supports US stocks "
            "(Finnhub), Korean stocks (Naver Finance), and crypto (Finnhub)."
        ),
    )
    async def get_news(
        symbol: str | int,
        market: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return await handle_get_news(symbol, market, limit)

    @mcp.tool(
        name="get_company_profile",
        description=(
            "Get company profile for a US or Korean stock. Crypto symbols like "
            "KRW-BTC are not supported; use get_crypto_profile for cryptocurrencies."
        ),
    )
    async def get_company_profile(
        symbol: str,
        market: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_company_profile(symbol, market)

    @mcp.tool(
        name="get_crypto_profile",
        description=(
            "Get cryptocurrency profile data from CoinGecko. Accepts Upbit market "
            "code (e.g. KRW-BTC) or plain symbol (e.g. BTC)."
        ),
    )
    async def get_crypto_profile(symbol: str) -> dict[str, Any]:
        return await handle_get_crypto_profile(symbol)

    @mcp.tool(
        name="get_financials",
        description=(
            "Get financial statements for a US or Korean stock. Supports income "
            "statement, balance sheet, and cash flow."
        ),
    )
    async def get_financials(
        symbol: str,
        statement: str = "income",
        freq: str = "annual",
        market: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_financials(symbol, statement, freq, market)

    @mcp.tool(
        name="get_insider_transactions",
        description=(
            "Get insider transactions for a US stock. Returns name, transaction "
            "type, shares, price, date. US stocks only."
        ),
    )
    async def get_insider_transactions(
        symbol: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await handle_get_insider_transactions(symbol, limit)

    @mcp.tool(
        name="get_earnings_calendar",
        description=(
            "Get earnings calendar for a US stock or date range. Returns earnings "
            "dates, EPS estimates and actuals. US stocks only."
        ),
    )
    async def get_earnings_calendar(
        symbol: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_earnings_calendar(symbol, from_date, to_date)

    @mcp.tool(
        name="get_investor_trends",
        description=(
            "Get foreign and institutional investor trading trends for a Korean "
            "stock. Returns daily net buy/sell data. Korean stocks only."
        ),
    )
    async def get_investor_trends(
        symbol: str,
        days: int = 20,
    ) -> dict[str, Any]:
        return await handle_get_investor_trends(symbol, days)

    @mcp.tool(
        name="get_investment_opinions",
        description=(
            "Get securities firm investment opinions and target prices for a US or "
            "Korean stock. Returns analyst ratings, price targets, and upside potential."
        ),
    )
    async def get_investment_opinions(
        symbol: str | int,
        limit: int = 10,
        market: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_investment_opinions(symbol, limit, market)

    @mcp.tool(
        name="get_valuation",
        description=(
            "Get valuation metrics for a US or Korean stock. Crypto symbols are not "
            "supported. Returns PER, PBR, ROE, dividend yield, 52-week high/low, "
            "current price, and position within 52-week range."
        ),
    )
    async def get_valuation(
        symbol: str | int,
        market: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_valuation(symbol, market)

    @mcp.tool(
        name="get_short_interest",
        description=(
            "Get short selling data for a Korean stock. Accepts only 6-digit "
            "Korean equity codes like '005930'. US tickers and crypto symbols "
            "are not supported."
        ),
    )
    async def get_short_interest(
        symbol: str,
        days: int = 20,
    ) -> dict[str, Any]:
        return await handle_get_short_interest(symbol, days)

    @mcp.tool(
        name="get_kimchi_premium",
        description=(
            "Get kimchi premium (김치 프리미엄) for cryptocurrencies. Compares Upbit "
            "KRW prices with Binance USDT prices to calculate premium percentage."
        ),
    )
    async def get_kimchi_premium(
        symbol: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        return await handle_get_kimchi_premium(symbol)

    @mcp.tool(
        name="get_funding_rate",
        description=(
            "Get futures funding rate for a cryptocurrency from Binance. Positive = "
            "longs pay shorts, negative = shorts pay longs."
        ),
    )
    async def get_funding_rate(
        symbol: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        return await handle_get_funding_rate(symbol, limit)

    @mcp.tool(
        name="get_market_index",
        description=(
            "Get market index data. Supports KOSPI/KOSDAQ and major US indices. "
            "Without symbol returns current major indices, with symbol adds OHLCV history."
        ),
    )
    async def get_market_index(
        symbol: str | None = None,
        period: str = "day",
        count: int = 20,
    ) -> dict[str, Any]:
        return await handle_get_market_index(symbol, period, count)

    @mcp.tool(
        name="get_support_resistance",
        description=(
            "Extract key support/resistance zones by combining Fibonacci levels, "
            "volume profile (POC/value area), and Bollinger Bands."
        ),
    )
    async def get_support_resistance(
        symbol: str,
        market: str | None = None,
    ) -> dict[str, Any]:
        impl = _get_support_resistance_impl
        if not callable(impl):
            impl = _DEFAULT_GET_SUPPORT_RESISTANCE_IMPL
        return await impl(symbol, market)

    @mcp.tool(
        name="get_sector_peers",
        description=(
            "Get sector peer stocks for comparison. Supports Korean and US stocks. "
            "Not available for cryptocurrencies."
        ),
    )
    async def get_sector_peers(
        symbol: str,
        market: str = "",
        limit: int = 5,
        manual_peers: list[str] | None = None,
    ) -> dict[str, Any]:
        return await handle_get_sector_peers(symbol, market, limit, manual_peers)


__all__ = [
    "FUNDAMENTALS_TOOL_NAMES",
    "_register_fundamentals_tools_impl",
    "_get_support_resistance_impl",
]
```

- [ ] **Step 3: Run fundamentals tests**

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py -q
```

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/tooling/fundamentals/__init__.py
git commit -m "refactor: slim fundamentals_handlers.py to thin registration layer"
```

---

### Task 12: Update test support for monkeypatching

**Files:**
- Modify: `tests/_mcp_tooling_support.py` (add new modules to `_PATCH_MODULES`)

The `_patch_runtime_attr` function patches attributes across `_PATCH_MODULES`. The `_get_support_resistance_impl` attribute must be patchable on the new module too.

- [ ] **Step 1: Read `_mcp_tooling_support.py` import section and `_PATCH_MODULES`**

Read lines 1-160 of `tests/_mcp_tooling_support.py` to get the current imports and module list.

- [ ] **Step 2: Add fundamentals submodule imports to `_PATCH_MODULES`**

Add these imports at the top (alongside existing `fundamentals_handlers` import):

```python
from app.mcp_server.tooling.fundamentals import (
    _support_resistance as fundamentals_support_resistance,
)
```

Add to the `_PATCH_MODULES` tuple (after `fundamentals_handlers`):

```python
    fundamentals_support_resistance,
```

- [ ] **Step 3: Run full test suites**

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py tests/test_mcp_indicator_tools.py -q
```

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add tests/_mcp_tooling_support.py
git commit -m "refactor: add fundamentals submodules to test patch modules"
```

---

### Task 13: Final verification and cleanup

**Files:** (none modified unless issues found)

- [ ] **Step 1: Run all related tests**

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py tests/test_mcp_indicator_tools.py tests/test_fundamentals_helpers.py tests/test_market_data_service.py -q
```

Expected: All pass.

- [ ] **Step 2: Run linter**

```bash
uv run ruff check app/mcp_server/tooling/fundamentals/ app/mcp_server/tooling/fundamentals_handlers.py
```

Expected: No errors.

- [ ] **Step 3: Verify file sizes**

```bash
wc -l app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/tooling/fundamentals/*.py
```

Expected: `fundamentals_handlers.py` is ~230 lines (registration only). Each handler module is 30-130 lines.

- [ ] **Step 4: Verify no import cycles**

```bash
python -c "from app.mcp_server.tooling.fundamentals_handlers import _register_fundamentals_tools_impl; print('OK')"
```

Expected: `OK` — no circular import errors.

- [ ] **Step 5: Squash or amend commits if requested, or leave as-is**

Suggested final squash message:

```
refactor: split fundamentals MCP handlers by domain

Split fundamentals_handlers.py (~930 lines) into focused handler modules
under app/mcp_server/tooling/fundamentals/:
- _helpers.py: market normalization (eliminates 6x duplication)
- _news.py, _profiles.py, _financials.py, _valuation.py
- _crypto.py, _market_index.py, _support_resistance.py, _sector_peers.py

Registration function is now a thin dispatcher (~230 lines).
All public tool names, parameters, and return contracts preserved.
```
