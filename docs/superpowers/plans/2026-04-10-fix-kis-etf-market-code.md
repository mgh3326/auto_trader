# Fix KIS ETF Market Code Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix KIS ETF symbol price lookup failure by changing the default domestic market code from `"UN"` to `"J"` (issue #487).

**Architecture:** All KIS domestic market API calls use `FID_COND_MRKT_DIV_CODE` parameter. The default `"UN"` excludes ETF/ETN. Changing to `"J"` (KRX unified) covers stocks + ETF/ETN. This is a mechanical substitution across 7 production files, 6 test files, plus 1 new regression test.

**Tech Stack:** Python, pytest, KIS Open API, pandas

**Spec:** `docs/superpowers/specs/2026-04-10-fix-kis-etf-market-code-design.md`

---

### Task 1: Update KIS constants documentation

**Files:**
- Modify: `app/services/brokers/kis/constants.py:176-181`

- [ ] **Step 1: Update DOMESTIC_MARKET_CODES dict**

Change the dict to clarify the meaning of each code:

```python
# Before
DOMESTIC_MARKET_CODES = {
    "K": "코스피",
    "Q": "코스닥",
    "UN": "통합",
    "J": "통합(랭킹 호환)",
}

# After
DOMESTIC_MARKET_CODES = {
    "K": "코스피",
    "Q": "코스닥",
    "J": "통합(주식+ETF/ETN)",   # 기본값
    "UN": "통합(주식만, ETF/ETN 제외)",
}
```

- [ ] **Step 2: Commit**

```bash
git add app/services/brokers/kis/constants.py
git commit -m "docs: clarify DOMESTIC_MARKET_CODES — J covers ETF/ETN, UN excludes them"
```

---

### Task 2: Change default market code in `domestic_market_data.py`

**Files:**
- Modify: `app/services/brokers/kis/domestic_market_data.py`

All 9 methods with `market: str = "UN"` default → `market: str = "J"`.

- [ ] **Step 1: Replace all `market: str = "UN"` defaults**

The 9 methods to change (line numbers approximate — use `replace_all` for the pattern):

| Line | Method |
|------|--------|
| 175 | `inquire_price` |
| 217 | `_request_orderbook_snapshot` |
| 228 | `inquire_orderbook` |
| 246 | `inquire_orderbook_snapshot` |
| 318 | `fetch_fundamental_info` |
| 399 | `inquire_daily_itemchartprice` |
| 456 | `inquire_time_dailychartprice` |
| 515 | `inquire_minute_chart` |
| 589 | `fetch_minute_candles` |

Apply with a single replace-all on the file:

```
old: market: str = "UN"
new: market: str = "J"
```

- [ ] **Step 2: Verify no remaining "UN" defaults in the file**

Run: `grep -n '"UN"' app/services/brokers/kis/domestic_market_data.py`

Expected: no output (zero matches).

- [ ] **Step 3: Commit**

```bash
git add app/services/brokers/kis/domestic_market_data.py
git commit -m "fix(kis): change default market code UN→J to include ETF/ETN (#487)"
```

---

### Task 3: Change default market code in `client.py` (proxy layer)

**Files:**
- Modify: `app/services/brokers/kis/client.py`

All 8 proxy methods with `market: str = "UN"` → `market: str = "J"`.

- [ ] **Step 1: Replace all `market: str = "UN"` defaults**

| Line | Method |
|------|--------|
| 95 | `inquire_price` |
| 98 | `inquire_orderbook` |
| 104 | `inquire_orderbook_snapshot` |
| 109 | `fetch_fundamental_info` |
| 116 | `inquire_daily_itemchartprice` |
| 144 | `inquire_time_dailychartprice` |
| 156 | `inquire_minute_chart` |
| 168 | `fetch_minute_candles` |

Apply with a single replace-all:

```
old: market: str = "UN"
new: market: str = "J"
```

- [ ] **Step 2: Verify no remaining "UN" defaults**

Run: `grep -n '"UN"' app/services/brokers/kis/client.py`

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add app/services/brokers/kis/client.py
git commit -m "fix(kis): change default market code UN→J in client proxy layer (#487)"
```

---

### Task 4: Change hardcoded `market="UN"` in callers

**Files:**
- Modify: `app/services/market_data/service.py` (4 locations)
- Modify: `app/mcp_server/tooling/market_data_quotes.py` (3 locations)
- Modify: `app/mcp_server/tooling/market_data_indicators.py` (2 locations)
- Modify: `app/routers/trading.py` (1 Protocol signature + 1 hardcoded)

- [ ] **Step 1: Update `app/services/market_data/service.py`**

Replace all `market="UN"` with `market="J"`:

```
Line 355: market="UN" → market="J"
Line 423: market="UN" → market="J"
Line 580: market="UN" → market="J"
Line 603: market="UN" → market="J"
```

Use replace-all on file:
```
old: market="UN"
new: market="J"
```

- [ ] **Step 2: Update `app/mcp_server/tooling/market_data_quotes.py`**

Replace all `market="UN"` with `market="J"`:

```
Line 405: market="UN" → market="J"
Line 653: market="UN" → market="J"
Line 686: market="UN" → market="J"
```

Use replace-all on file:
```
old: market="UN"
new: market="J"
```

- [ ] **Step 3: Update `app/mcp_server/tooling/market_data_indicators.py`**

Replace all `market="UN"` with `market="J"`:

```
Line 98:  market="UN" → market="J"
Line 125: market="UN" → market="J"
```

Use replace-all on file:
```
old: market="UN"
new: market="J"
```

- [ ] **Step 4: Update `app/routers/trading.py`**

Two changes:

Line 216 — Protocol signature:
```python
# Before
async def inquire_price(self, code: str, market: str = "UN") -> DataFrame: ...

# After
async def inquire_price(self, code: str, market: str = "J") -> DataFrame: ...
```

Line 554 — hardcoded call:
```python
# Before
code=ticker, market="UN", n=normalized_days, period="D"

# After
code=ticker, market="J", n=normalized_days, period="D"
```

- [ ] **Step 5: Verify zero remaining "UN" in production code**

Run: `grep -rn '"UN"' app/services/brokers/kis/domestic_market_data.py app/services/brokers/kis/client.py app/services/market_data/service.py app/mcp_server/tooling/market_data_quotes.py app/mcp_server/tooling/market_data_indicators.py app/routers/trading.py`

Expected: only the `constants.py` dict entry `"UN": "통합(주식만, ETF/ETN 제외)"` remains.

- [ ] **Step 6: Commit**

```bash
git add app/services/market_data/service.py app/mcp_server/tooling/market_data_quotes.py app/mcp_server/tooling/market_data_indicators.py app/routers/trading.py
git commit -m "fix(kis): replace hardcoded market='UN' with 'J' in callers (#487)"
```

---

### Task 5: Update test files — mechanical `"UN"` → `"J"` substitution

**Files:**
- Modify: `tests/test_services_kis_market_data.py` (6 locations)
- Modify: `tests/test_mcp_ohlcv_tools.py` (1 location)
- Modify: `tests/test_mcp_quotes_tools.py` (1 location)
- Modify: `tests/test_mcp_indicator_tools.py` (2 locations + 1 function rename)
- Modify: `tests/test_market_data_service.py` (many locations)
- Modify: `tests/test_trading_orderbook_router.py` (4+ locations)

- [ ] **Step 1: Update `tests/test_services_kis_market_data.py`**

Replace all occurrences of `market="UN"` with `market="J"`:

```
Line 488: market="UN" → market="J"
Line 549: market="UN" → market="J"
Line 573: market="UN" → market="J"
Line 601: market="UN" → market="J"
Line 633: market="UN" → market="J"
Line 648: market="UN" → market="J"
```

Use replace-all on file:
```
old: market="UN"
new: market="J"
```

- [ ] **Step 2: Update `tests/test_mcp_ohlcv_tools.py`**

Line 598:
```python
# Before
assert called["market"] == "UN"

# After
assert called["market"] == "J"
```

- [ ] **Step 3: Update `tests/test_mcp_quotes_tools.py`**

Line 198:
```python
# Before
assert called["market"] == "UN"

# After
assert called["market"] == "J"
```

- [ ] **Step 4: Update `tests/test_mcp_indicator_tools.py`**

Line 376 and line 415 — replace assert values:
```python
# Before
assert called["market"] == "UN"

# After
assert called["market"] == "J"
```

Also rename the test function at line 382:
```python
# Before
async def test_fetch_ohlcv_for_volume_profile_kr_uses_un_market(monkeypatch):

# After
async def test_fetch_ohlcv_for_volume_profile_kr_uses_j_market(monkeypatch):
```

- [ ] **Step 5: Update `tests/test_market_data_service.py`**

This file has many `DummyKIS` classes with `market: str = "UN"` in signatures and `assert market == "UN"` checks. Apply two replace-all passes on the file:

Pass 1 — signatures:
```
old: market: str = "UN"
new: market: str = "J"
```

Pass 2 — assertions:
```
old: assert market == "UN"
new: assert market == "J"
```

Affected locations (approximate lines): 277, 279, 322, 364, 390, 417, 460, 490, 492, 520, 522, 548, 550, 785, 854.

- [ ] **Step 6: Update `tests/test_trading_orderbook_router.py`**

Replace all `market: str = "UN"` with `market: str = "J"` in mock signatures:

```
Line 90:  market: str = "UN" → market: str = "J"
Line 115: market: str = "UN" → market: str = "J"
Line 159: market: str = "UN" → market: str = "J"
Line 187: market: str = "UN" → market: str = "J"
```

Use replace-all:
```
old: market: str = "UN"
new: market: str = "J"
```

- [ ] **Step 7: Verify zero remaining "UN" in test files**

Run: `grep -rn '"UN"' tests/test_services_kis_market_data.py tests/test_mcp_ohlcv_tools.py tests/test_mcp_quotes_tools.py tests/test_mcp_indicator_tools.py tests/test_market_data_service.py tests/test_trading_orderbook_router.py`

Expected: no output (zero matches).

- [ ] **Step 8: Run full test suite**

Run: `make test`

Expected: all tests pass. If any tests fail, the failure is due to a missed `"UN"` → `"J"` substitution — find and fix it.

- [ ] **Step 9: Commit**

```bash
git add tests/test_services_kis_market_data.py tests/test_mcp_ohlcv_tools.py tests/test_mcp_quotes_tools.py tests/test_mcp_indicator_tools.py tests/test_market_data_service.py tests/test_trading_orderbook_router.py
git commit -m "test: update all market code assertions UN→J to match new default (#487)"
```

---

### Task 6: Add ETF + stock regression test

**Files:**
- Modify: `tests/test_mcp_quotes_tools.py`

- [ ] **Step 1: Write the parametrized test**

Add the following test after the existing `test_get_quote_korean_etf_with_explicit_market` test (around line 257):

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol,label",
    [
        ("005930", "stock"),  # 삼성전자 — 일반 주식
        ("133690", "etf"),  # TIGER 은행TOP10 — ETF
    ],
)
async def test_fetch_quote_equity_kr_passes_market_j(monkeypatch, symbol, label):
    """Regression: market='J' is passed to KIS API for both stocks and ETFs (#487)."""
    tools = build_tools()
    df = _single_row_df()
    called: dict[str, object] = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            called["code"] = code
            called["market"] = market
            called["n"] = n
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"](symbol)

    assert called["market"] == "J", f"Expected market='J' for {label} symbol {symbol}"
    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["price"] == 105.0
    assert result["symbol"] == symbol
```

- [ ] **Step 2: Run the new test to verify it passes**

Run: `uv run pytest tests/test_mcp_quotes_tools.py::test_fetch_quote_equity_kr_passes_market_j -v`

Expected:
```
test_fetch_quote_equity_kr_passes_market_j[005930-stock] PASSED
test_fetch_quote_equity_kr_passes_market_j[133690-etf] PASSED
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_mcp_quotes_tools.py
git commit -m "test: add ETF+stock regression test for market=J (#487)"
```

---

### Task 7: Final verification

- [ ] **Step 1: Run full test suite**

Run: `make test`

Expected: all tests pass.

- [ ] **Step 2: Run lint and type check**

Run: `make lint`

Expected: no errors.

- [ ] **Step 3: Verify production code has no stale "UN" defaults**

Run: `grep -rn 'market.*=.*"UN"\|market="UN"' app/ --include="*.py" | grep -v constants.py | grep -v __pycache__`

Expected: no output. The only remaining `"UN"` should be in `constants.py` as a documented dict entry.

- [ ] **Step 4: Verify test code has no stale "UN" references (in changed files)**

Run: `grep -rn '"UN"' tests/test_services_kis_market_data.py tests/test_mcp_ohlcv_tools.py tests/test_mcp_quotes_tools.py tests/test_mcp_indicator_tools.py tests/test_market_data_service.py tests/test_trading_orderbook_router.py`

Expected: no output.
