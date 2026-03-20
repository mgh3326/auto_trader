# n8n Crypto Scan API Endpoint Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `GET /api/n8n/crypto-scan` endpoint that exposes DailyScanner's technical indicator data as raw JSON for n8n/Robin AI consumption.

**Architecture:** New service (`n8n_crypto_scan_service.py`) reuses existing Upbit client functions and DailyScanner static helpers to collect OHLCV/RSI/SMA/crash/F&G data. Router endpoint in existing `app/routers/n8n.py` follows established n8n patterns. No DailyScanner modifications — import and reuse only.

**Tech Stack:** FastAPI, Pydantic v2, asyncio (Semaphore for concurrent OHLCV), existing Upbit broker client, existing indicator functions.

---

## Task 1: Add Pydantic Schemas

**Files:**
- Modify: `app/schemas/n8n.py` (append after line 739)

**Step 1: Add crypto scan schema classes**

Append these classes to the end of `app/schemas/n8n.py`:

```python
# -----------------------------------------------------------------------------
# Crypto Scan (GET /api/n8n/crypto-scan)
# -----------------------------------------------------------------------------
class N8nCryptoScanParams(BaseModel):
    """Echo of the scan parameters used for this response."""

    top_n: int = Field(..., description="Top N coins by 24h trade amount")
    include_holdings: bool = Field(...)
    include_crash: bool = Field(...)
    include_sma_cross: bool = Field(...)
    include_fear_greed: bool = Field(...)
    ohlcv_days: int = Field(...)


class N8nBtcContext(BaseModel):
    """BTC technical context for market backdrop."""

    rsi14: float | None = Field(None, description="BTC RSI 14-period")
    sma20: float | None = Field(None, description="BTC SMA 20")
    sma60: float | None = Field(None, description="BTC SMA 60")
    sma200: float | None = Field(None, description="BTC SMA 200")
    current_price: float | None = Field(None, description="BTC current price in KRW")
    change_rate_24h: float | None = Field(None, description="BTC 24h change rate")


class N8nCryptoScanIndicators(BaseModel):
    """Per-coin technical indicators."""

    rsi14: float | None = Field(None, description="RSI 14-period")
    sma20: float | None = Field(None, description="SMA 20")
    sma60: float | None = Field(None, description="SMA 60")
    sma200: float | None = Field(None, description="SMA 200")


class N8nSmaCross(BaseModel):
    """SMA20 crossing event data."""

    type: str = Field(..., description="golden or dead")
    prev_close: float = Field(...)
    curr_close: float = Field(...)
    prev_sma20: float = Field(...)
    curr_sma20: float = Field(...)


class N8nCrashData(BaseModel):
    """Crash detection data per coin."""

    change_rate_24h: float = Field(..., description="Actual 24h change rate")
    threshold: float = Field(..., description="Crash threshold for this coin's rank")
    triggered: bool = Field(..., description="Whether abs(change) >= threshold")


class N8nCryptoScanCoin(BaseModel):
    """Single coin in the crypto scan response."""

    symbol: str = Field(..., description="Upbit market code, e.g. KRW-BTC")
    currency: str = Field(..., description="Currency code, e.g. BTC")
    name: str = Field(..., description="Korean name, e.g. 비트코인")
    rank: int | None = Field(None, description="Trade amount rank (1-based), null if holding-only")
    is_holding: bool = Field(..., description="Whether user currently holds this coin")
    current_price: float | None = Field(None, description="Current trade price in KRW")
    change_rate_24h: float | None = Field(None, description="24h signed change rate")
    trade_amount_24h: float | None = Field(None, description="24h accumulated trade amount in KRW")
    indicators: N8nCryptoScanIndicators = Field(...)
    sma_cross: N8nSmaCross | None = Field(None)
    crash: N8nCrashData | None = Field(None)


class N8nCryptoScanSummary(BaseModel):
    """Aggregate summary of the scan results."""

    total_scanned: int = Field(..., description="Total coins scanned")
    top_n_count: int = Field(..., description="Coins from top N by trade amount")
    holdings_added: int = Field(..., description="Extra coins added because they are held")
    oversold_count: int = Field(0, description="Coins with RSI < 35 (reference only)")
    overbought_count: int = Field(0, description="Coins with RSI > 70 (reference only)")
    crash_triggered_count: int = Field(0, description="Coins that triggered crash threshold")
    sma_golden_cross_count: int = Field(0, description="SMA20 golden cross coins")
    sma_dead_cross_count: int = Field(0, description="SMA20 dead cross coins")


class N8nCryptoScanResponse(BaseModel):
    """Top-level response for GET /api/n8n/crypto-scan."""

    success: bool = Field(...)
    as_of: str = Field(..., description="Response timestamp in KST ISO8601")
    scan_params: N8nCryptoScanParams = Field(...)
    btc_context: N8nBtcContext = Field(...)
    fear_greed: N8nFearGreedData | None = Field(None)
    coins: list[N8nCryptoScanCoin] = Field(default_factory=list)
    summary: N8nCryptoScanSummary = Field(...)
    errors: list[dict[str, object]] = Field(default_factory=list)
```

**Step 2: Verify schema imports compile**

Run: `uv run python -c "from app.schemas.n8n import N8nCryptoScanResponse; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add app/schemas/n8n.py
git commit -m "feat(n8n): add crypto scan Pydantic schemas"
```

---

## Task 2: Write Failing Service Tests

**Files:**
- Create: `tests/test_n8n_crypto_scan_service.py`

**Step 1: Write service unit tests**

Create `tests/test_n8n_crypto_scan_service.py`:

```python
"""Unit tests for n8n crypto scan service."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest


@pytest.fixture
def mock_top_coins() -> list[dict]:
    """Top traded coins fixture — 3 coins sorted by trade amount."""
    return [
        {"market": "KRW-BTC", "acc_trade_price_24h": 150_000_000_000},
        {"market": "KRW-ETH", "acc_trade_price_24h": 80_000_000_000},
        {"market": "KRW-XRP", "acc_trade_price_24h": 45_000_000_000},
    ]


@pytest.fixture
def mock_my_coins() -> list[dict]:
    """Holdings fixture — user holds BTC and SOL (SOL not in top 3)."""
    return [
        {"currency": "BTC", "balance": "0.5", "locked": "0"},
        {"currency": "SOL", "balance": "10", "locked": "0"},
    ]


@pytest.fixture
def mock_ohlcv_df() -> pd.DataFrame:
    """OHLCV dataframe with enough rows for RSI/SMA calculation."""
    import numpy as np

    np.random.seed(42)
    n = 50
    close = pd.Series(np.cumsum(np.random.randn(n)) + 100)
    return pd.DataFrame(
        {
            "open": close - 1,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": [1000] * n,
        }
    )


@pytest.fixture
def mock_tickers() -> list[dict]:
    """Ticker data for BTC, ETH, XRP, SOL."""
    return [
        {
            "market": "KRW-BTC",
            "trade_price": 110_000_000,
            "signed_change_rate": -0.0018,
            "acc_trade_price_24h": 150_000_000_000,
        },
        {
            "market": "KRW-ETH",
            "trade_price": 2_900_000,
            "signed_change_rate": 0.02,
            "acc_trade_price_24h": 80_000_000_000,
        },
        {
            "market": "KRW-XRP",
            "trade_price": 800,
            "signed_change_rate": -0.05,
            "acc_trade_price_24h": 45_000_000_000,
        },
        {
            "market": "KRW-SOL",
            "trade_price": 180_000,
            "signed_change_rate": 0.03,
            "acc_trade_price_24h": 20_000_000_000,
        },
    ]


def _patch_all():
    """Return a dict of patches for all external dependencies."""
    return {
        "top_coins": patch(
            "app.services.n8n_crypto_scan_service.fetch_top_traded_coins",
            new_callable=AsyncMock,
        ),
        "my_coins": patch(
            "app.services.n8n_crypto_scan_service.fetch_my_coins",
            new_callable=AsyncMock,
        ),
        "ohlcv": patch(
            "app.services.n8n_crypto_scan_service.fetch_ohlcv",
            new_callable=AsyncMock,
        ),
        "tickers": patch(
            "app.services.n8n_crypto_scan_service.fetch_multiple_tickers",
            new_callable=AsyncMock,
        ),
        "fear_greed": patch(
            "app.services.n8n_crypto_scan_service.fetch_fear_greed",
            new_callable=AsyncMock,
        ),
        "korean_name": patch(
            "app.services.n8n_crypto_scan_service.get_upbit_korean_name_by_coin",
            new_callable=AsyncMock,
        ),
    }


@pytest.mark.unit
class TestFetchCryptoScan:
    """Tests for fetch_crypto_scan service function."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_all_fields(
        self,
        mock_top_coins: list[dict],
        mock_my_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
        mock_tickers: list[dict],
    ) -> None:
        """Scan returns coins with indicators, BTC context, F&G, and summary."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins
            m_my.return_value = mock_my_coins
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = mock_tickers
            m_fg.return_value = {
                "value": 34,
                "label": "Fear",
                "previous": 28,
                "trend": "improving",
            }
            m_name.return_value = "테스트코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3, ohlcv_days=50)

        assert result["success"] is True
        assert "btc_context" in result
        assert "fear_greed" in result
        assert "coins" in result
        assert "summary" in result
        assert "errors" in result
        # Should have 4 coins: BTC, ETH, XRP (top 3) + SOL (holding)
        assert len(result["coins"]) == 4
        # Summary should reflect correct counts
        assert result["summary"]["top_n_count"] == 3
        assert result["summary"]["holdings_added"] == 1
        assert result["summary"]["total_scanned"] == 4

    @pytest.mark.asyncio
    async def test_coins_sorted_by_rsi_ascending(
        self,
        mock_top_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
        mock_tickers: list[dict],
    ) -> None:
        """Coins should be sorted by RSI ascending (most oversold first)."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins
            m_my.return_value = []  # no holdings
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = mock_tickers[:3]
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3)

        coins = result["coins"]
        rsi_values = [
            c["indicators"]["rsi14"]
            for c in coins
            if c["indicators"]["rsi14"] is not None
        ]
        assert rsi_values == sorted(rsi_values), "Coins must be sorted by RSI ascending"

    @pytest.mark.asyncio
    async def test_holdings_added_outside_top_n(
        self,
        mock_top_coins: list[dict],
        mock_my_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
        mock_tickers: list[dict],
    ) -> None:
        """Holdings not in top_n should still appear in coins."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins
            m_my.return_value = mock_my_coins  # holds BTC + SOL
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = mock_tickers
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3, include_holdings=True)

        symbols = [c["symbol"] for c in result["coins"]]
        assert "KRW-SOL" in symbols, "SOL (holding but not top 3) should be included"
        sol_coin = next(c for c in result["coins"] if c["symbol"] == "KRW-SOL")
        assert sol_coin["is_holding"] is True
        assert sol_coin["rank"] is None  # not in top N

    @pytest.mark.asyncio
    async def test_include_holdings_false_excludes_extra(
        self,
        mock_top_coins: list[dict],
        mock_my_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
        mock_tickers: list[dict],
    ) -> None:
        """When include_holdings=False, only top_n coins appear."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins
            m_my.return_value = mock_my_coins
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = mock_tickers[:3]
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3, include_holdings=False)

        symbols = [c["symbol"] for c in result["coins"]]
        assert "KRW-SOL" not in symbols
        assert len(result["coins"]) == 3

    @pytest.mark.asyncio
    async def test_include_fear_greed_false_returns_none(
        self,
        mock_top_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
        mock_tickers: list[dict],
    ) -> None:
        """When include_fear_greed=False, fear_greed field is None."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins[:1]
            m_my.return_value = []
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = mock_tickers[:1]
            m_fg.return_value = {"value": 34, "label": "Fear", "previous": 28, "trend": "improving"}
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=1, include_fear_greed=False)

        assert result["fear_greed"] is None
        m_fg.assert_not_called()

    @pytest.mark.asyncio
    async def test_ohlcv_failure_produces_null_indicators(
        self,
        mock_top_coins: list[dict],
        mock_tickers: list[dict],
    ) -> None:
        """When OHLCV fetch fails for a coin, indicators should be null."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins[:1]
            m_my.return_value = []
            m_ohlcv.side_effect = Exception("Upbit API error")
            m_tickers.return_value = mock_tickers[:1]
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=1)

        assert result["success"] is True  # partial failure is not fatal
        assert len(result["coins"]) == 1
        coin = result["coins"][0]
        assert coin["indicators"]["rsi14"] is None
        assert coin["indicators"]["sma20"] is None
        assert len(result["errors"]) >= 1

    @pytest.mark.asyncio
    async def test_crash_threshold_rank_based(
        self,
        mock_top_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
    ) -> None:
        """Crash data should use rank-based thresholds from DailyScanner."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            # BTC is rank 1 (top 10 threshold = 0.06)
            m_top.return_value = mock_top_coins
            m_my.return_value = []
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = [
                {
                    "market": "KRW-BTC",
                    "trade_price": 110_000_000,
                    "signed_change_rate": -0.07,  # exceeds 0.06 threshold
                    "acc_trade_price_24h": 150_000_000_000,
                },
                {
                    "market": "KRW-ETH",
                    "trade_price": 2_900_000,
                    "signed_change_rate": -0.02,  # below threshold
                    "acc_trade_price_24h": 80_000_000_000,
                },
                {
                    "market": "KRW-XRP",
                    "trade_price": 800,
                    "signed_change_rate": -0.03,
                    "acc_trade_price_24h": 45_000_000_000,
                },
            ]
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3, include_crash=True)

        btc = next(c for c in result["coins"] if c["symbol"] == "KRW-BTC")
        assert btc["crash"] is not None
        assert btc["crash"]["triggered"] is True
        assert btc["crash"]["threshold"] == 0.06  # top 10 threshold

    @pytest.mark.asyncio
    async def test_sma_cross_detection(self) -> None:
        """SMA20 golden cross should be detected correctly."""
        patches = _patch_all()
        # Build OHLCV where last candle crosses above SMA20
        # prev_close < prev_sma20 AND curr_close > curr_sma20
        import numpy as np

        n = 25
        # Price starts below SMA20 then jumps above
        close_values = [100.0] * 20 + [95.0, 94.0, 93.0, 92.0, 105.0]
        close = pd.Series(close_values)
        df = pd.DataFrame(
            {
                "open": close - 1,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "volume": [1000] * n,
            }
        )

        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = [
                {"market": "KRW-TEST", "acc_trade_price_24h": 1_000_000},
            ]
            m_my.return_value = []
            m_ohlcv.return_value = df
            m_tickers.return_value = [
                {
                    "market": "KRW-TEST",
                    "trade_price": 105,
                    "signed_change_rate": 0.14,
                    "acc_trade_price_24h": 1_000_000,
                },
            ]
            m_fg.return_value = None
            m_name.return_value = "테스트"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=1, include_sma_cross=True)

        coin = result["coins"][0]
        assert coin["sma_cross"] is not None
        assert coin["sma_cross"]["type"] == "golden"

    @pytest.mark.asyncio
    async def test_rsi_null_sorted_last(
        self,
        mock_top_coins: list[dict],
        mock_tickers: list[dict],
    ) -> None:
        """Coins with null RSI should appear at end of sorted list."""
        patches = _patch_all()
        ohlcv_ok = pd.DataFrame(
            {
                "close": pd.Series([100.0 + i for i in range(50)]),
                "open": pd.Series([99.0 + i for i in range(50)]),
                "high": pd.Series([102.0 + i for i in range(50)]),
                "low": pd.Series([98.0 + i for i in range(50)]),
                "volume": [1000] * 50,
            }
        )
        ohlcv_empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        call_count = 0

        async def alternating_ohlcv(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # BTC gets empty (null RSI), ETH and XRP get real data
            market = args[0] if args else kwargs.get("market", "")
            if market == "KRW-BTC":
                return ohlcv_empty
            return ohlcv_ok

        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins
            m_my.return_value = []
            m_ohlcv.side_effect = alternating_ohlcv
            m_tickers.return_value = mock_tickers[:3]
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3)

        coins = result["coins"]
        # BTC (null RSI) should be last
        last_coin = coins[-1]
        assert last_coin["symbol"] == "KRW-BTC"
        assert last_coin["indicators"]["rsi14"] is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_n8n_crypto_scan_service.py -v --no-header 2>&1 | head -30`
Expected: `ModuleNotFoundError: No module named 'app.services.n8n_crypto_scan_service'`

**Step 3: Commit**

```bash
git add tests/test_n8n_crypto_scan_service.py
git commit -m "test(n8n): add crypto scan service unit tests (red)"
```

---

## Task 3: Implement Crypto Scan Service

**Files:**
- Create: `app/services/n8n_crypto_scan_service.py`

**Step 1: Create the service implementation**

Create `app/services/n8n_crypto_scan_service.py`:

```python
"""Crypto scan service for n8n integration.

Reuses DailyScanner data-collection logic but returns raw indicator data
without signal judgement, message assembly, or alert delivery.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pandas as pd

from app.core.config import settings
from app.core.timezone import now_kst
from app.jobs.daily_scan import DailyScanner
from app.mcp_server.tooling.market_data_indicators import _calculate_rsi, _calculate_sma
from app.services.brokers.upbit.client import (
    fetch_multiple_tickers,
    fetch_my_coins,
    fetch_ohlcv,
    fetch_top_traded_coins,
)
from app.services.external.fear_greed import fetch_fear_greed
from app.services.upbit_symbol_universe_service import get_upbit_korean_name_by_coin

logger = logging.getLogger(__name__)

# Concurrent OHLCV fetch limit (Upbit rate limit aware)
_OHLCV_SEMAPHORE_LIMIT = 5


def _to_float(value: object) -> float | None:
    """Safe float conversion."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _indicator_value(data: dict[str, float | None], key: str) -> float | None:
    return _to_float(data.get(key))


def _currency_from_market(market: str) -> str:
    if "-" in market:
        return market.split("-")[-1].upper()
    return market.upper()


async def _fetch_coin_name(currency: str) -> str:
    """Get Korean name, returning currency code on failure."""
    try:
        return await get_upbit_korean_name_by_coin(currency, quote_currency="KRW")
    except Exception:
        return currency


def _detect_sma_cross(
    close: pd.Series,
) -> dict[str, Any] | None:
    """Detect SMA20 golden/dead cross from close series.

    Returns dict with type/prev_close/curr_close/prev_sma20/curr_sma20,
    or None if no crossing or insufficient data.
    """
    if len(close) < 21:
        return None

    prev_close = _to_float(close.iloc[-2])
    curr_close = _to_float(close.iloc[-1])
    prev_sma20 = _indicator_value(
        _calculate_sma(close.iloc[:-1], periods=[20]), "20"
    )
    curr_sma20 = _indicator_value(
        _calculate_sma(close, periods=[20]), "20"
    )

    if any(v is None for v in (prev_close, curr_close, prev_sma20, curr_sma20)):
        return None

    if prev_close < prev_sma20 and curr_close > curr_sma20:
        return {
            "type": "golden",
            "prev_close": prev_close,
            "curr_close": curr_close,
            "prev_sma20": prev_sma20,
            "curr_sma20": curr_sma20,
        }
    elif prev_close > prev_sma20 and curr_close < curr_sma20:
        return {
            "type": "dead",
            "prev_close": prev_close,
            "curr_close": curr_close,
            "prev_sma20": prev_sma20,
            "curr_sma20": curr_sma20,
        }

    return None


async def _build_btc_context(ohlcv_days: int) -> tuple[dict[str, Any], list[dict]]:
    """Build BTC technical context. Returns (btc_context_dict, errors)."""
    errors: list[dict] = []
    ctx: dict[str, Any] = {
        "rsi14": None,
        "sma20": None,
        "sma60": None,
        "sma200": None,
        "current_price": None,
        "change_rate_24h": None,
    }

    try:
        btc_df = await fetch_ohlcv("KRW-BTC", days=ohlcv_days)
        if not btc_df.empty and "close" in btc_df.columns:
            close = btc_df["close"]
            ctx["rsi14"] = _indicator_value(_calculate_rsi(close), "14")
            sma = _calculate_sma(close, periods=[20, 60, 200])
            ctx["sma20"] = _indicator_value(sma, "20")
            ctx["sma60"] = _indicator_value(sma, "60")
            ctx["sma200"] = _indicator_value(sma, "200")
    except Exception as exc:
        logger.warning("Failed to fetch BTC OHLCV: %s", exc)
        errors.append({"source": "btc_ohlcv", "error": str(exc)})

    try:
        tickers = await fetch_multiple_tickers(["KRW-BTC"])
        if tickers:
            ctx["current_price"] = _to_float(tickers[0].get("trade_price"))
            ctx["change_rate_24h"] = _to_float(tickers[0].get("signed_change_rate"))
    except Exception as exc:
        logger.warning("Failed to fetch BTC ticker: %s", exc)
        errors.append({"source": "btc_ticker", "error": str(exc)})

    return ctx, errors


async def _build_coin_data(
    *,
    market: str,
    rank: int | None,
    is_holding: bool,
    ticker_map: dict[str, dict],
    ohlcv_days: int,
    include_crash: bool,
    include_sma_cross: bool,
    semaphore: asyncio.Semaphore,
) -> tuple[dict[str, Any] | None, list[dict]]:
    """Build scan data for a single coin. Returns (coin_dict, errors)."""
    errors: list[dict] = []
    currency = _currency_from_market(market)
    name = await _fetch_coin_name(currency)

    ticker = ticker_map.get(market, {})
    current_price = _to_float(ticker.get("trade_price"))
    change_rate_24h = _to_float(ticker.get("signed_change_rate"))
    trade_amount_24h = _to_float(ticker.get("acc_trade_price_24h"))

    indicators: dict[str, float | None] = {
        "rsi14": None,
        "sma20": None,
        "sma60": None,
        "sma200": None,
    }
    sma_cross = None
    crash = None

    # Fetch OHLCV with concurrency limit
    try:
        async with semaphore:
            df = await fetch_ohlcv(market, days=ohlcv_days)

        if not df.empty and "close" in df.columns:
            close = df["close"]
            indicators["rsi14"] = _indicator_value(_calculate_rsi(close), "14")
            sma = _calculate_sma(close, periods=[20, 60, 200])
            indicators["sma20"] = _indicator_value(sma, "20")
            indicators["sma60"] = _indicator_value(sma, "60")
            indicators["sma200"] = _indicator_value(sma, "200")

            if include_sma_cross:
                sma_cross = _detect_sma_cross(close)
    except Exception as exc:
        logger.warning("Failed to fetch OHLCV for %s: %s", market, exc)
        errors.append({"source": f"ohlcv:{market}", "error": str(exc)})

    # Crash detection
    if include_crash and change_rate_24h is not None:
        threshold = DailyScanner._crash_threshold_for_candidate(rank, is_holding)
        crash = {
            "change_rate_24h": change_rate_24h,
            "threshold": threshold,
            "triggered": abs(change_rate_24h) >= threshold,
        }

    coin = {
        "symbol": market,
        "currency": currency,
        "name": name,
        "rank": rank,
        "is_holding": is_holding,
        "current_price": current_price,
        "change_rate_24h": change_rate_24h,
        "trade_amount_24h": trade_amount_24h,
        "indicators": indicators,
        "sma_cross": sma_cross,
        "crash": crash,
    }

    return coin, errors


async def fetch_crypto_scan(
    *,
    top_n: int = 30,
    include_holdings: bool = True,
    include_crash: bool = True,
    include_sma_cross: bool = True,
    include_fear_greed: bool = True,
    ohlcv_days: int = 50,
) -> dict[str, Any]:
    """Collect crypto scan data: indicators, crash, SMA cross, F&G.

    Returns a dict matching N8nCryptoScanResponse schema.
    Does NOT make signal judgements or send alerts.
    """
    errors: list[dict] = []

    # 1. Fetch top traded coins and holdings in parallel
    top_coins_task = fetch_top_traded_coins("KRW")
    my_coins_task = fetch_my_coins() if include_holdings else asyncio.coroutine(lambda: [])()

    try:
        top_coins, my_coins = await asyncio.gather(
            top_coins_task, my_coins_task, return_exceptions=False
        )
    except Exception as exc:
        logger.error("Failed to fetch coin universe: %s", exc)
        return {
            "success": False,
            "btc_context": {},
            "fear_greed": None,
            "coins": [],
            "summary": {
                "total_scanned": 0,
                "top_n_count": 0,
                "holdings_added": 0,
            },
            "errors": [{"source": "universe", "error": str(exc)}],
        }

    # 2. Build rank map and determine scan universe
    rank_by_market = DailyScanner._build_rank_by_market(top_coins)
    tradable_markets = set(rank_by_market.keys())

    # Top N markets
    top_n_markets: set[str] = set()
    for market, rank in rank_by_market.items():
        if rank <= top_n:
            top_n_markets.add(market)

    # Holdings markets (outside top N)
    holding_markets: set[str] = set()
    if include_holdings:
        for coin in my_coins:
            currency = str(coin.get("currency") or "").upper()
            if not currency or currency == "KRW":
                continue
            market = f"KRW-{currency}"
            if market in tradable_markets:
                holding_markets.add(market)

    holdings_added = len(holding_markets - top_n_markets)
    all_markets = sorted(top_n_markets | holding_markets)

    # 3. Fetch tickers, BTC context, and F&G in parallel
    fg_task = fetch_fear_greed() if include_fear_greed else None

    try:
        ticker_result = await fetch_multiple_tickers(all_markets)
    except Exception as exc:
        logger.warning("Failed to fetch tickers: %s", exc)
        ticker_result = []
        errors.append({"source": "tickers", "error": str(exc)})

    ticker_map = {t["market"]: t for t in ticker_result if "market" in t}
    btc_context, btc_errors = await _build_btc_context(ohlcv_days)
    errors.extend(btc_errors)

    fear_greed_data = None
    if fg_task is not None:
        try:
            fear_greed_data = await fg_task
        except Exception as exc:
            logger.warning("Failed to fetch Fear & Greed: %s", exc)
            errors.append({"source": "fear_greed", "error": str(exc)})

    # 4. Build coin data in parallel with semaphore
    semaphore = asyncio.Semaphore(_OHLCV_SEMAPHORE_LIMIT)
    coin_tasks = []
    for market in all_markets:
        rank = rank_by_market.get(market)
        is_holding = market in holding_markets
        # rank is None for holding-only coins not in top N
        display_rank = rank if market in top_n_markets else None

        coin_tasks.append(
            _build_coin_data(
                market=market,
                rank=display_rank,
                is_holding=is_holding,
                ticker_map=ticker_map,
                ohlcv_days=ohlcv_days,
                include_crash=include_crash,
                include_sma_cross=include_sma_cross,
                semaphore=semaphore,
            )
        )

    coin_results = await asyncio.gather(*coin_tasks, return_exceptions=True)

    # 5. Collect results
    coins: list[dict] = []
    for result in coin_results:
        if isinstance(result, Exception):
            errors.append({"source": "coin_build", "error": str(result)})
            continue
        coin_data, coin_errors = result
        if coin_data is not None:
            coins.append(coin_data)
        errors.extend(coin_errors)

    # 6. Sort by RSI ascending (null RSI last)
    def rsi_sort_key(coin: dict) -> tuple[int, float]:
        rsi = coin["indicators"]["rsi14"]
        if rsi is None:
            return (1, 0.0)  # null RSI goes last
        return (0, rsi)

    coins.sort(key=rsi_sort_key)

    # 7. Build summary
    oversold_count = sum(
        1
        for c in coins
        if c["indicators"]["rsi14"] is not None
        and c["indicators"]["rsi14"] < settings.DAILY_SCAN_RSI_OVERSOLD
    )
    overbought_count = sum(
        1
        for c in coins
        if c["indicators"]["rsi14"] is not None
        and c["indicators"]["rsi14"] > settings.DAILY_SCAN_RSI_OVERBOUGHT
    )
    crash_triggered_count = sum(
        1 for c in coins if c.get("crash") and c["crash"]["triggered"]
    )
    sma_golden = sum(
        1 for c in coins if c.get("sma_cross") and c["sma_cross"]["type"] == "golden"
    )
    sma_dead = sum(
        1 for c in coins if c.get("sma_cross") and c["sma_cross"]["type"] == "dead"
    )

    return {
        "success": True,
        "btc_context": btc_context,
        "fear_greed": fear_greed_data,
        "coins": coins,
        "summary": {
            "total_scanned": len(coins),
            "top_n_count": len(top_n_markets),
            "holdings_added": holdings_added,
            "oversold_count": oversold_count,
            "overbought_count": overbought_count,
            "crash_triggered_count": crash_triggered_count,
            "sma_golden_cross_count": sma_golden,
            "sma_dead_cross_count": sma_dead,
        },
        "errors": errors,
    }
```

**Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_n8n_crypto_scan_service.py -v --no-header`
Expected: All tests PASS

**Step 3: Run lint**

Run: `uv run ruff check app/services/n8n_crypto_scan_service.py && uv run ruff format --check app/services/n8n_crypto_scan_service.py`
Expected: No errors

**Step 4: Commit**

```bash
git add app/services/n8n_crypto_scan_service.py
git commit -m "feat(n8n): implement crypto scan service with parallel OHLCV fetch"
```

---

## Task 4: Write Failing Router Tests

**Files:**
- Create: `tests/test_n8n_crypto_scan_api.py`

**Step 1: Write router endpoint tests**

Create `tests/test_n8n_crypto_scan_api.py`:

```python
"""Tests for GET /api/n8n/crypto-scan endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.n8n import router as n8n_router


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(n8n_router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _mock_scan_result(**overrides) -> dict:
    """Build a valid scan result dict."""
    base = {
        "success": True,
        "btc_context": {
            "rsi14": 63.5,
            "sma20": 101_000_000.0,
            "sma60": 109_000_000.0,
            "sma200": 137_000_000.0,
            "current_price": 110_000_000,
            "change_rate_24h": -0.0018,
        },
        "fear_greed": {
            "value": 34,
            "label": "Fear",
            "previous": 28,
            "trend": "improving",
        },
        "coins": [
            {
                "symbol": "KRW-BTC",
                "currency": "BTC",
                "name": "비트코인",
                "rank": 1,
                "is_holding": True,
                "current_price": 110_000_000,
                "change_rate_24h": -0.0018,
                "trade_amount_24h": 150_000_000_000,
                "indicators": {
                    "rsi14": 63.5,
                    "sma20": 101_000_000.0,
                    "sma60": 109_000_000.0,
                    "sma200": 137_000_000.0,
                },
                "sma_cross": None,
                "crash": None,
            }
        ],
        "summary": {
            "total_scanned": 30,
            "top_n_count": 30,
            "holdings_added": 0,
            "oversold_count": 2,
            "overbought_count": 0,
            "crash_triggered_count": 0,
            "sma_golden_cross_count": 1,
            "sma_dead_cross_count": 0,
        },
        "errors": [],
    }
    base.update(overrides)
    return base


@pytest.mark.unit
class TestCryptoScanEndpoint:
    """Tests for GET /api/n8n/crypto-scan."""

    def test_success_response(self, client: TestClient) -> None:
        with patch(
            "app.routers.n8n.fetch_crypto_scan", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _mock_scan_result()
            response = client.get("/api/n8n/crypto-scan")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "as_of" in data
        assert "scan_params" in data
        assert data["scan_params"]["top_n"] == 30
        assert len(data["coins"]) == 1
        assert data["coins"][0]["symbol"] == "KRW-BTC"

    def test_query_params_forwarded(self, client: TestClient) -> None:
        with patch(
            "app.routers.n8n.fetch_crypto_scan", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _mock_scan_result()
            response = client.get(
                "/api/n8n/crypto-scan",
                params={
                    "top_n": 10,
                    "include_holdings": False,
                    "include_crash": False,
                    "include_sma_cross": False,
                    "include_fear_greed": False,
                    "ohlcv_days": 100,
                },
            )

        assert response.status_code == 200
        mock_fetch.assert_called_once_with(
            top_n=10,
            include_holdings=False,
            include_crash=False,
            include_sma_cross=False,
            include_fear_greed=False,
            ohlcv_days=100,
        )
        data = response.json()
        assert data["scan_params"]["top_n"] == 10
        assert data["scan_params"]["include_holdings"] is False
        assert data["scan_params"]["ohlcv_days"] == 100

    def test_service_exception_returns_500(self, client: TestClient) -> None:
        with patch(
            "app.routers.n8n.fetch_crypto_scan", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = RuntimeError("Upbit down")
            response = client.get("/api/n8n/crypto-scan")

        assert response.status_code == 500
        data = response.json()
        assert data["success"] is False
        assert len(data["errors"]) >= 1

    def test_top_n_validation_min(self, client: TestClient) -> None:
        response = client.get("/api/n8n/crypto-scan", params={"top_n": 0})
        assert response.status_code == 422

    def test_top_n_validation_max(self, client: TestClient) -> None:
        response = client.get("/api/n8n/crypto-scan", params={"top_n": 200})
        assert response.status_code == 422

    def test_ohlcv_days_validation(self, client: TestClient) -> None:
        response = client.get("/api/n8n/crypto-scan", params={"ohlcv_days": 5})
        assert response.status_code == 422

    def test_default_params(self, client: TestClient) -> None:
        with patch(
            "app.routers.n8n.fetch_crypto_scan", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _mock_scan_result()
            response = client.get("/api/n8n/crypto-scan")

        assert response.status_code == 200
        data = response.json()
        assert data["scan_params"]["top_n"] == 30
        assert data["scan_params"]["include_holdings"] is True
        assert data["scan_params"]["include_crash"] is True
        assert data["scan_params"]["include_sma_cross"] is True
        assert data["scan_params"]["include_fear_greed"] is True
        assert data["scan_params"]["ohlcv_days"] == 50
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_n8n_crypto_scan_api.py -v --no-header 2>&1 | head -20`
Expected: `ImportError` or `AttributeError` because `fetch_crypto_scan` is not yet imported in the router

**Step 3: Commit**

```bash
git add tests/test_n8n_crypto_scan_api.py
git commit -m "test(n8n): add crypto scan endpoint tests (red)"
```

---

## Task 5: Add Router Endpoint

**Files:**
- Modify: `app/routers/n8n.py` (add import + endpoint)

**Step 1: Add import for the new service and schemas**

Add to the imports section of `app/routers/n8n.py`:

```python
# Add to the schema imports block (line 12-29):
from app.schemas.n8n import (
    # ... existing imports ...
    N8nBtcContext,
    N8nCryptoScanParams,
    N8nCryptoScanResponse,
    N8nCryptoScanSummary,
)

# Add to the service imports block (line 30-42):
from app.services.n8n_crypto_scan_service import fetch_crypto_scan
```

**Step 2: Add the endpoint**

Add after the last existing endpoint (after `patch_pending_resolve`, around line 393):

```python
@router.get("/crypto-scan", response_model=N8nCryptoScanResponse)
async def get_crypto_scan(
    top_n: int = Query(30, ge=1, le=100, description="Top N by 24h trade amount"),
    include_holdings: bool = Query(True, description="Include holding coins outside top N"),
    include_crash: bool = Query(True, description="Include crash detection data"),
    include_sma_cross: bool = Query(True, description="Include SMA20 cross detection"),
    include_fear_greed: bool = Query(True, description="Include Fear & Greed Index"),
    ohlcv_days: int = Query(50, ge=20, le=200, description="OHLCV lookback days"),
) -> N8nCryptoScanResponse | JSONResponse:
    as_of = now_kst().replace(microsecond=0).isoformat()
    scan_params = N8nCryptoScanParams(
        top_n=top_n,
        include_holdings=include_holdings,
        include_crash=include_crash,
        include_sma_cross=include_sma_cross,
        include_fear_greed=include_fear_greed,
        ohlcv_days=ohlcv_days,
    )

    try:
        result = await fetch_crypto_scan(
            top_n=top_n,
            include_holdings=include_holdings,
            include_crash=include_crash,
            include_sma_cross=include_sma_cross,
            include_fear_greed=include_fear_greed,
            ohlcv_days=ohlcv_days,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build n8n crypto scan response")
        payload = N8nCryptoScanResponse(
            success=False,
            as_of=as_of,
            scan_params=scan_params,
            btc_context=N8nBtcContext(),
            fear_greed=None,
            coins=[],
            summary=N8nCryptoScanSummary(
                total_scanned=0,
                top_n_count=0,
                holdings_added=0,
            ),
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nCryptoScanResponse(
        success=result.get("success", True),
        as_of=as_of,
        scan_params=scan_params,
        btc_context=N8nBtcContext(**(result.get("btc_context") or {})),
        fear_greed=result.get("fear_greed"),
        coins=result.get("coins", []),
        summary=N8nCryptoScanSummary(**(result.get("summary", {}))),
        errors=result.get("errors", []),
    )
```

**Step 3: Run router tests to verify they pass**

Run: `uv run pytest tests/test_n8n_crypto_scan_api.py -v --no-header`
Expected: All PASS

**Step 4: Run service tests to confirm no regressions**

Run: `uv run pytest tests/test_n8n_crypto_scan_service.py -v --no-header`
Expected: All PASS

**Step 5: Run lint on changed files**

Run: `uv run ruff check app/routers/n8n.py app/schemas/n8n.py && uv run ruff format --check app/routers/n8n.py app/schemas/n8n.py`
Expected: No errors

**Step 6: Commit**

```bash
git add app/routers/n8n.py
git commit -m "feat(n8n): add GET /api/n8n/crypto-scan endpoint"
```

---

## Task 6: Full Verification

**Step 1: Run all n8n-related tests**

Run: `uv run pytest tests/test_n8n_crypto_scan_service.py tests/test_n8n_crypto_scan_api.py -v --no-header`
Expected: All PASS

**Step 2: Run full test suite to check for regressions**

Run: `uv run pytest tests/ -m "not live" -v --no-header --tb=short 2>&1 | tail -30`
Expected: No new failures

**Step 3: Run LSP diagnostics on all changed files**

Check: `app/schemas/n8n.py`, `app/services/n8n_crypto_scan_service.py`, `app/routers/n8n.py`
Expected: No errors

**Step 4: Run lint on full project**

Run: `make lint`
Expected: PASS

**Step 5: Final commit if any fixups needed**

```bash
git add -A
git commit -m "chore: lint and formatting fixes for crypto scan endpoint"
```

---

## Reference: Key Import Paths

| Function | Import Path | Notes |
|----------|-------------|-------|
| `fetch_top_traded_coins` | `app.services.brokers.upbit.client` | Returns all KRW coins sorted by trade amount |
| `fetch_my_coins` | `app.services.brokers.upbit.client` | Returns user's Upbit account holdings |
| `fetch_ohlcv` | `app.services.brokers.upbit.client` | Returns pd.DataFrame with OHLCV data |
| `fetch_multiple_tickers` | `app.services.brokers.upbit.client` | Batch ticker fetch (50 per request) |
| `_calculate_rsi` | `app.mcp_server.tooling.market_data_indicators` | Returns `{"14": float\|None}` |
| `_calculate_sma` | `app.mcp_server.tooling.market_data_indicators` | Returns `{"20": float\|None, ...}` |
| `fetch_fear_greed` | `app.services.external.fear_greed` | Returns `{value, label, previous, trend}` or None |
| `get_upbit_korean_name_by_coin` | `app.services.upbit_symbol_universe_service` | Returns Korean name string |
| `DailyScanner._crash_threshold_for_candidate` | `app.jobs.daily_scan` | Static method, rank-based threshold |
| `DailyScanner._build_rank_by_market` | `app.jobs.daily_scan` | Static method, builds rank dict |

## Reference: Config Settings Used

| Setting | Default | Description |
|---------|---------|-------------|
| `DAILY_SCAN_RSI_OVERSOLD` | 35.0 | RSI threshold for oversold classification |
| `DAILY_SCAN_RSI_OVERBOUGHT` | 70.0 | RSI threshold for overbought classification |
| `DAILY_SCAN_CRASH_TOP10_THRESHOLD` | 0.06 | Crash threshold for rank 1-10 |
| `DAILY_SCAN_CRASH_TOP30_THRESHOLD` | 0.08 | Crash threshold for rank 11-30 |
| `DAILY_SCAN_CRASH_TOP50_THRESHOLD` | 0.10 | Crash threshold for rank 31-50 |
| `DAILY_SCAN_CRASH_TOP100_THRESHOLD` | 0.20 | Crash threshold for rank 51+ |
| `DAILY_SCAN_CRASH_HOLDING_THRESHOLD` | 0.04 | Crash threshold for holdings (min of rank/holding) |
