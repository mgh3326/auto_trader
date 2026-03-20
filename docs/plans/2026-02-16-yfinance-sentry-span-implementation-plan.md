# yfinance Sentry Span Instrumentation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** MCP/API 트랜잭션 안에서 yfinance가 발생시키는 Yahoo HTTP 요청을 `METHOD + path` child span으로 Sentry Trace에 표시한다.

**Architecture:** `curl_cffi.requests.Session` 래퍼를 공통 모듈로 추가하고, yfinance 호출부에 `session=`으로 주입한다. 래퍼는 `request()` 단위로 `sentry_sdk.start_span(op="http.client")`를 생성해 `url`, method, status를 span data에 기록한다. 상위 비즈니스 로직은 변경하지 않고 관측성만 확장한다.

**Tech Stack:** Python 3.11+, sentry-sdk, yfinance 1.1.x, curl_cffi, FastAPI, FastMCP, pytest.

---

### Task 1: Build shared yfinance tracing session module

**Files:**
- Create: `app/monitoring/yfinance_sentry.py`
- Modify: `app/monitoring/__init__.py`
- Create: `tests/test_yfinance_sentry.py`

**Step 1: Write the failing test**

Add tests for:
- span name is `METHOD /path`
- query string is excluded from span name
- status code data is recorded
- invalid URL falls back to `"/unknown"`
- wrapped request exceptions are re-raised
- arbitrary kwargs (for example `timeout`, `headers`, `params`) are forwarded unchanged to `super().request(...)`

```python
def test_tracing_session_uses_method_and_path(monkeypatch):
    session = SentryTracingCurlSession()
    ...
    response = session.request("GET", "https://query1.finance.yahoo.com/v1/finance/screener?x=1")
    assert started_span_name == "GET /v1/finance/screener"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_yfinance_sentry.py -v`  
Expected: FAIL (`ModuleNotFoundError` or missing class/functions)

**Step 3: Write minimal implementation**

In `app/monitoring/yfinance_sentry.py`:
- implement `SentryTracingCurlSession(Session)`
- override `request(self, method, url, **kwargs)`
- derive path via `urllib.parse.urlsplit(url).path or "/unknown"`
- create span:

```python
with sentry_sdk.start_span(op="http.client", name=f"{method_up} {path}") as span:
    span.set_data("url", url)
    span.set_data("http.request.method", method_up)
    response = super().request(method, url, **kwargs)
    span.set_data("http.response.status_code", response.status_code)
    return response
```

- add helper `build_yfinance_tracing_session()`
- export in `app/monitoring/__init__.py`

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_yfinance_sentry.py -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/monitoring/yfinance_sentry.py app/monitoring/__init__.py tests/test_yfinance_sentry.py
git commit -m "Add yfinance tracing session for Sentry spans"
```

### Task 2: Inject tracing session into shared Yahoo service layer

**Files:**
- Modify: `app/services/yahoo.py`
- Modify: `tests/test_services.py`

**Step 1: Write the failing test**

Add tests for:
- `fetch_ohlcv()` passes `session=` to `yf.download(...)`
- `fetch_price()` and `fetch_fundamental_info()` instantiate `yf.Ticker(..., session=...)`
- existing payload/shape unchanged

```python
@pytest.mark.asyncio
async def test_fetch_ohlcv_passes_tracing_session(monkeypatch):
    captured = {}
    def fake_download(*args, **kwargs):
        captured["session"] = kwargs.get("session")
        return sample_df
    ...
    await fetch_ohlcv("AAPL", days=5)
    assert captured["session"] is not None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services.py -k "fetch_ohlcv or fetch_price or fundamental" -v`  
Expected: FAIL (missing `session` injection assertions)

**Step 3: Write minimal implementation**

In `app/services/yahoo.py`:
- create session per top-level call:
  - `session = build_yfinance_tracing_session()`
- pass session into:
  - `yf.download(..., session=session)`
  - `yf.Ticker(yahoo_ticker, session=session)`

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_services.py -k "fetch_ohlcv or fetch_price or fundamental" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/yahoo.py tests/test_services.py
git commit -m "Inject Sentry tracing session into yahoo service calls"
```

### Task 3: Wire session into US screening/ranking yfinance entrypoints

**Files:**
- Modify: `app/mcp_server/tooling/analysis_screen_core.py`
- Modify: `app/mcp_server/tooling/analysis_rankings.py`
- Modify: `tests/test_mcp_screen_stocks.py`
- Modify: `tests/test_mcp_top_stocks.py`

**Step 1: Write the failing test**

Add tests that assert `yf.screen(...)` receives non-None `session` in:
- US `screen_stocks` path
- US ranking/top-stocks path

```python
def mock_yfinance_screen(*args, **kwargs):
    assert kwargs.get("session") is not None
    return {"quotes": []}
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_screen_stocks.py -k "us and screen" -v`  
Run: `uv run pytest tests/test_mcp_top_stocks.py -k "yfinance" -v`  
Expected: FAIL (no session passed currently)

**Step 3: Write minimal implementation**

In `analysis_screen_core.py` and `analysis_rankings.py`:
- instantiate session via `build_yfinance_tracing_session()`
- pass `session=session` to each `yf.screen(...)` call
- preserve existing `asyncio.to_thread(...)` boundaries and return contract

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_screen_stocks.py -k "us and screen" -v`  
Run: `uv run pytest tests/test_mcp_top_stocks.py -k "yfinance" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_screen_core.py app/mcp_server/tooling/analysis_rankings.py tests/test_mcp_screen_stocks.py tests/test_mcp_top_stocks.py
git commit -m "Add tracing session to yfinance screen and ranking flows"
```

### Task 4: Wire session into quote/dividend tool handlers

**Files:**
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py`
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Modify: `tests/test_mcp_server_tools.py`

**Step 1: Write the failing test**

Add tests for US quote/dividend paths asserting:
- `yf.Ticker(..., session=...)` is used
- response payload contract is unchanged

```python
def ticker_factory(symbol, session=None):
    assert session is not None
    return MockTicker()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server_tools.py -k "get_quote_us or dividends" -v`  
Expected: FAIL (session not injected yet)

**Step 3: Write minimal implementation**

In both files:
- create session with `build_yfinance_tracing_session()`
- pass session to `yf.Ticker(...)` constructors in US branches

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server_tools.py -k "get_quote_us or dividends" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_tool_handlers.py app/mcp_server/tooling/market_data_quotes.py tests/test_mcp_server_tools.py
git commit -m "Inject tracing session into quote and dividend yfinance handlers"
```

### Task 5: Wire session into fundamentals/index yfinance sources

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals_sources_naver.py`
- Modify: `app/mcp_server/tooling/fundamentals_sources_indices.py`
- Modify: `tests/test_mcp_server_tools.py`

**Step 1: Write the failing test**

Add tests for fundamentals/index helpers asserting:
- `yf.Ticker(..., session=...)`
- `yf.download(..., session=...)`
- outputs and source tags (`yfinance`, `finnhub+yfinance`) unchanged

```python
def ticker_factory(symbol, session=None):
    assert session is not None
    return StubTicker(...)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server_tools.py -k "fundamentals and yfinance or indices" -v`  
Expected: FAIL

**Step 3: Write minimal implementation**

In both fundamentals source files:
- build and pass tracing session to all yfinance entrypoints
- explicitly handle `fundamentals_sources_naver.py` line-635 lambda pattern (`lambda t=ticker: yf.Ticker(t).info`):
  - replace with a helper/closure that captures tracing session
  - call `yf.Ticker(t, session=session).info` inside that helper/closure
  - keep existing behavior and threading model unchanged (`asyncio.to_thread(...)`)
- avoid widening function signatures; keep behavior local and non-breaking

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server_tools.py -k "fundamentals and yfinance or indices" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/fundamentals_sources_naver.py app/mcp_server/tooling/fundamentals_sources_indices.py tests/test_mcp_server_tools.py
git commit -m "Apply yfinance tracing session to fundamentals and indices sources"
```

### Task 6: End-to-end verification and documentation update

**Files:**
- Modify: `app/mcp_server/README.md`
- Modify: `docs/plans/2026-02-16-yfinance-sentry-span-design.md` (if any drift updates)

**Step 1: Add/adjust docs and trace query guidance**

Document:
- yfinance HTTP spans are custom-instrumented through tracing session
- span name format: `METHOD /path`
- example Sentry filters

**Step 2: Run focused validation tests**

Run:
- `uv run pytest tests/test_yfinance_sentry.py -v`
- `uv run pytest tests/test_services.py -k "yahoo" -v`
- `uv run pytest tests/test_mcp_screen_stocks.py -k "us and screen" -v`
- `uv run pytest tests/test_mcp_server_tools.py -k "get_quote_us or dividends or fundamentals or indices" -v`

Expected: PASS

**Step 3: Run lint/type checks for touched files**

Run:
- `make lint`
- `make typecheck`

Expected: PASS (or known pre-existing failures documented)

**Step 4: Commit**

```bash
git add app/mcp_server/README.md docs/plans/2026-02-16-yfinance-sentry-span-design.md
git commit -m "Document yfinance METHOD-path span instrumentation"
```
