# KR Analyze Stock Dedup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove exact duplicate KR `analyze_stock` Naver and KIS fetches while keeping the MCP request/response contract unchanged.

**Architecture:** Rework `app/services/naver_finance.py` around shared internal fetch/parser helpers so one per-run `httpx.AsyncClient` can preload the KR Naver snapshot and reuse the already-fetched main-page soup/current price in the opinions flow. Then add an internal bundled wrapper in `app/mcp_server/tooling/fundamentals_sources_naver.py` and update the KR branch of `app/mcp_server/tooling/analysis_screening.py` to consume the bundled Naver payload plus the already-fetched KIS OHLCV frame instead of starting duplicate quote and Naver tasks.

**Tech Stack:** Python 3.13, asyncio, httpx, BeautifulSoup, pandas, pytest, FastMCP tooling helpers.

---

### Task 1: Add failing Naver service regression tests

**Files:**
- Modify: `tests/test_naver_finance.py`
- Inspect while implementing: `app/services/naver_finance.py`

**Step 1: Write the failing tests**

Add tests that prove:
- opinion consensus can use a preloaded main-page current price without re-fetching `main.naver`
- duplicate `nid` rows in `company_list.naver` collapse before detail fetch fan-out
- distinct `nid` rows still fetch one detail page each

Use the existing `TestFetchInvestmentOpinions` mocking pattern and count URLs/`nid` values inside the fake `_fetch_html` implementation.

**Step 2: Run the service tests to verify RED**

Run: `uv run pytest tests/test_naver_finance.py -k investment_opinions -v`

Expected: the newly added tests fail because the current implementation always fetches `main.naver` separately and does not deduplicate duplicate `nid` rows.

### Task 2: Add failing KR analyze_stock regression test

**Files:**
- Modify: `tests/test_mcp_fundamentals_tools.py`
- Inspect while implementing: `app/mcp_server/tooling/analysis_screening.py`, `app/mcp_server/tooling/fundamentals_sources_naver.py`, `tests/_mcp_tooling_support.py`

**Step 1: Write the failing test**

Add a KR `analyze_stock` test near `test_analyze_stock_us_reuses_yfinance_info` that:
- patches `_fetch_ohlcv_for_indicators` to return a one-row KR frame
- patches the new bundled Naver helper to return decorated `valuation`, `news`, and `opinions`
- patches `_fetch_quote_equity_kr` to track unexpected calls
- asserts `analyze_stock("005930", market="kr")` reuses the preloaded OHLCV data for `quote`, calls the Naver bundle once, never calls the standalone KR quote helper, and keeps the existing top-level keys/shape

**Step 2: Run the MCP test to verify RED**

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py -k "analyze_stock and kr" -v`

Expected: the new test fails because the current KR branch still calls `_fetch_quote_equity_kr` and separate Naver helpers.

### Task 3: Implement shared Naver snapshot helpers

**Files:**
- Modify: `app/services/naver_finance.py`

**Step 1: Extract shared helper layer**

Introduce internal helpers for:
- shared HTML fetches with an injected `httpx.AsyncClient`
- parsing `main.naver`, `sise.naver`, `news_news.naver`, and `company_list.naver`
- fetching one report detail page with an injected client
- computing current price from a preloaded main-page soup

Keep public functions (`fetch_news`, `fetch_valuation`, `fetch_investment_opinions`) intact by delegating to the new internal parsers/fetch helpers.

**Step 2: Implement bundled snapshot flow**

Add an internal async helper that fetches `main.naver`, `sise.naver`, `news_news.naver`, and `company_list.naver` once, then returns a bundle containing:
- valuation payload built from the preloaded `main` + `sise` soups
- news items built from the preloaded news soup
- investment opinions built from the preloaded company-list soup, deduplicated by `nid`, with one detail fetch per unique `nid`, and consensus computed from the already-parsed main-page current price

**Step 3: Run service tests to verify GREEN**

Run: `uv run pytest tests/test_naver_finance.py -k investment_opinions -v`

Expected: all investment-opinion tests, including the new dedupe/current-price tests, pass.

### Task 4: Wire the bundled KR snapshot into MCP analyze_stock

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals_sources_naver.py`
- Modify: `app/mcp_server/tooling/analysis_screening.py`

**Step 1: Add an internal KR snapshot wrapper**

In `fundamentals_sources_naver.py`, add one internal helper that calls the new Naver bundled snapshot service helper and returns:
- `valuation` decorated with `instrument_type="equity_kr"` and `source="naver"`
- `news` decorated with the current KR wrapper shape
- `opinions` decorated with the current KR wrapper shape

This helper must stay internal only; do not change public MCP tool signatures.

**Step 2: Rework KR analyze_stock task fan-out**

In `analysis_screening.py`:
- stop launching standalone KR valuation/news/opinions tasks
- launch the new bundled KR Naver task once
- derive the KR `quote` payload from the last row of the already-fetched `ohlcv_df`
- fall back to `_fetch_quote_equity_kr` only when the preloaded frame is empty
- leave US and crypto paths unchanged

**Step 3: Run KR analyze_stock tests to verify GREEN**

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py -k "analyze_stock and (kr or yfinance_info)" -v`

Expected: the new KR regression test passes and the existing US dedupe test still passes.

### Task 5: Full verification on touched scope

**Files:**
- Verify: `app/services/naver_finance.py`
- Verify: `app/mcp_server/tooling/fundamentals_sources_naver.py`
- Verify: `app/mcp_server/tooling/analysis_screening.py`
- Verify: `tests/test_naver_finance.py`
- Verify: `tests/test_mcp_fundamentals_tools.py`

**Step 1: Run diagnostics**

Check LSP diagnostics on every modified Python file and resolve all errors/warnings that stem from the change.

**Step 2: Run targeted pytest**

Run:
- `uv run pytest tests/test_naver_finance.py -k investment_opinions -v`
- `uv run pytest tests/test_mcp_fundamentals_tools.py -k "analyze_stock or yfinance_info" -v`

**Step 3: Run one broader safety check**

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py -v`

If this broader file is too expensive or exposes unrelated pre-existing failures, document the exact failing tests and confirm the new KR dedupe coverage still passes.
