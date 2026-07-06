# ROB-748 US Quote Session Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add honest US quote session/freshness labels to `get_quote` and `analyze_stock_batch`, so premarket/regular/after-hours/closed US prices are no longer surfaced with null provenance.

**Architecture:** Reuse the existing session-calendar pattern already used for KR freshness and XNYS scheduling. Add a US session classifier in `app/mcp_server/tooling/market_session.py`, preserve optional KIS overseas quote timestamp fields from `HHDFS00000300`, then tag the US quote dict in `market_data_quotes.py` before it reaches both `get_quote` and `analysis_analyze._get_quote_impl`. Compact `analyze_stock_batch` only needs an additive pass-through update because its US quote comes from the same `_fetch_quote_equity_us` helper.

**Tech Stack:** Python 3.13, pytest/pytest-asyncio, pandas, `exchange_calendars`, existing MCP tooling, `uv run`.

## Global Constraints

- No DB schema or Alembic migration.
- Read-only market-data paths only; do not touch order, ledger, or broker mutation code.
- Preserve existing US KIS-primary then Yahoo fallback behavior and `delayed: true`.
- Response changes must be additive: existing keys keep their meaning and existing error behavior stays unchanged.
- `session` vocabulary for US quotes is exactly `premarket`, `regular`, `afterhours`, or `closed`.
- `data_state` for US quotes is `fresh` while the US extended-hours envelope is open and `stale` when classified as `closed`.
- `quote_asof` is included only when the provider payload gives enough date/time fields to parse it; do not fabricate a timestamp from wall clock.
- Keep MCP behavior docs in `app/mcp_server/README.md` synchronized with code.

---

## File Structure

- Modify `app/mcp_server/tooling/market_session.py`: add US session constants and `us_market_session(now=None)`.
- Modify `tests/test_market_session.py`: add deterministic tests for US premarket, regular, afterhours, closed, holiday, and half-day behavior.
- Modify `app/services/brokers/kis/overseas_market_data.py`: preserve `quote_asof` from optional KIS overseas fields such as `xymd`/`xhms`.
- Modify `tests/test_services_kis_market_data.py`: add KIS overseas current-price parsing coverage for `quote_asof` and empty-frame columns.
- Modify `app/mcp_server/tooling/market_data_quotes.py`: tag US quotes with `session`, `data_state`, `price_source`, optional `quote_asof`, `venue`, and `data_state_reason`.
- Modify `tests/test_mcp_quotes_tools.py`: add US `get_quote` contract tests for KIS premarket, Yahoo fallback, and closed-session stale labeling.
- Modify `app/mcp_server/tooling/analysis_tool_handlers.py`: pass through US quote provenance fields in compact `analyze_stock_batch`.
- Create `tests/mcp_server/test_analyze_stock_batch_us_quote_session.py`: verify compact summary keeps US quote labels.
- Modify `app/mcp_server/README.md`: document the US quote and compact analysis labels.

---

### Task 1: Add US Session Classification

**Files:**
- Modify: `app/mcp_server/tooling/market_session.py`
- Modify: `tests/test_market_session.py`

**Interfaces:**
- Produces: `def us_market_session(now: Any = None) -> str`
- Produces constants: `US_SESSION_PREMARKET`, `US_SESSION_REGULAR`, `US_SESSION_AFTERHOURS`, `US_SESSION_CLOSED`
- Consumes: `app.services.market_events.session_calendar.regular_session_bounds`

- [ ] **Step 1: Write failing US session tests**

Append to `tests/test_market_session.py`:

```python
import datetime as dt


def test_us_market_session_returns_premarket_on_xnys_session_day(monkeypatch):
    open_utc = dt.datetime(2026, 7, 6, 13, 30, tzinfo=dt.UTC)
    close_utc = dt.datetime(2026, 7, 6, 20, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: (open_utc, close_utc) if market == "us" else None,
    )

    now = dt.datetime(2026, 7, 6, 8, 0, tzinfo=dt.UTC)  # 04:00 ET

    assert market_session.us_market_session(now) == "premarket"


def test_us_market_session_returns_regular_during_xnys_regular_hours(monkeypatch):
    open_utc = dt.datetime(2026, 7, 6, 13, 30, tzinfo=dt.UTC)
    close_utc = dt.datetime(2026, 7, 6, 20, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: (open_utc, close_utc) if market == "us" else None,
    )

    now = dt.datetime(2026, 7, 6, 15, 0, tzinfo=dt.UTC)  # 11:00 ET

    assert market_session.us_market_session(now) == "regular"


def test_us_market_session_returns_afterhours_after_regular_close(monkeypatch):
    open_utc = dt.datetime(2026, 7, 6, 13, 30, tzinfo=dt.UTC)
    close_utc = dt.datetime(2026, 7, 6, 20, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: (open_utc, close_utc) if market == "us" else None,
    )

    now = dt.datetime(2026, 7, 6, 21, 0, tzinfo=dt.UTC)  # 17:00 ET

    assert market_session.us_market_session(now) == "afterhours"


def test_us_market_session_returns_closed_before_premarket(monkeypatch):
    open_utc = dt.datetime(2026, 7, 6, 13, 30, tzinfo=dt.UTC)
    close_utc = dt.datetime(2026, 7, 6, 20, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: (open_utc, close_utc) if market == "us" else None,
    )

    now = dt.datetime(2026, 7, 6, 7, 59, tzinfo=dt.UTC)  # 03:59 ET

    assert market_session.us_market_session(now) == "closed"


def test_us_market_session_returns_closed_on_xnys_holiday(monkeypatch):
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: None,
    )

    now = dt.datetime(2026, 7, 4, 15, 0, tzinfo=dt.UTC)

    assert market_session.us_market_session(now) == "closed"


def test_us_market_session_honors_half_day_early_close(monkeypatch):
    open_utc = dt.datetime(2025, 11, 28, 14, 30, tzinfo=dt.UTC)
    close_utc = dt.datetime(2025, 11, 28, 18, 0, tzinfo=dt.UTC)  # 13:00 ET
    monkeypatch.setattr(
        market_session,
        "regular_session_bounds",
        lambda market, day: (open_utc, close_utc) if market == "us" else None,
    )

    now = dt.datetime(2025, 11, 28, 19, 0, tzinfo=dt.UTC)  # 14:00 ET

    assert market_session.us_market_session(now) == "afterhours"
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```bash
uv run pytest tests/test_market_session.py -k us_market_session -v
```

Expected: fails because `market_session.us_market_session` does not exist.

- [ ] **Step 3: Implement the US session helper**

In `app/mcp_server/tooling/market_session.py`, add imports near the top:

```python
from datetime import time as _time
from zoneinfo import ZoneInfo

from app.services.market_events.session_calendar import regular_session_bounds
```

Add the constants after the existing `DATA_STATE_*` constants:

```python
US_SESSION_PREMARKET = "premarket"
US_SESSION_REGULAR = "regular"
US_SESSION_AFTERHOURS = "afterhours"
US_SESSION_CLOSED = "closed"

_UTC = ZoneInfo("UTC")
_ET = ZoneInfo("America/New_York")
_US_PRE_OPEN = _time(4, 0)
_US_AFTER_CLOSE = _time(20, 0)
```

Add this function after `kr_market_data_state`:

```python
def us_market_session(now: Any = None) -> str:
    """Classify the current US equity quote session using XNYS regular bounds.

    Returns ``premarket`` for 04:00 ET up to the XNYS open, ``regular`` for
    XNYS regular hours, ``afterhours`` from the XNYS close up to 20:00 ET, and
    ``closed`` outside those windows or on non-session days. Naive timestamps
    are treated as UTC. Early closes are honored by ``regular_session_bounds``.
    """
    current = now if now is not None else _dt.datetime.now(_dt.UTC)
    if not isinstance(current, _dt.datetime):
        current = pd.Timestamp(current).to_pydatetime()
    if current.tzinfo is None:
        current = current.replace(tzinfo=_UTC)

    local = current.astimezone(_ET)
    bounds = regular_session_bounds("us", local.date())
    if bounds is None:
        return US_SESSION_CLOSED

    open_utc, close_utc = bounds
    current_utc = current.astimezone(_UTC)
    if open_utc <= current_utc < close_utc:
        return US_SESSION_REGULAR

    open_local = open_utc.astimezone(_ET)
    close_local = close_utc.astimezone(_ET)
    pre_open = local.replace(
        hour=_US_PRE_OPEN.hour, minute=0, second=0, microsecond=0
    )
    after_close = local.replace(
        hour=_US_AFTER_CLOSE.hour, minute=0, second=0, microsecond=0
    )
    if pre_open <= local < open_local:
        return US_SESSION_PREMARKET
    if close_local <= local < after_close:
        return US_SESSION_AFTERHOURS
    return US_SESSION_CLOSED
```

- [ ] **Step 4: Run the focused tests and confirm pass**

Run:

```bash
uv run pytest tests/test_market_session.py -k us_market_session -v
```

Expected: all `us_market_session` tests pass.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add app/mcp_server/tooling/market_session.py tests/test_market_session.py
git commit -m "feat(ROB-748): add US quote session classifier"
```

---

### Task 2: Preserve KIS Overseas `quote_asof`

**Files:**
- Modify: `app/services/brokers/kis/overseas_market_data.py`
- Modify: `tests/test_services_kis_market_data.py`

**Interfaces:**
- Produces: `KISClient.inquire_overseas_price(...)` DataFrame columns `["close", "previous_close", "volume", "quote_asof"]`
- Preserves: empty-frame zero-price guard and existing close/base/tvol parsing

- [ ] **Step 1: Write failing KIS parser tests**

In `tests/test_services_kis_market_data.py`, update `TestKISOverseasPrice.test_inquire_overseas_price_parses_output` mock output:

```python
mock_response.json.return_value = {
    "rt_cd": "0",
    "output": {
        "last": "150.25",
        "base": "148.00",
        "tvol": "1234567",
        "xymd": "20260706",
        "xhms": "084512",
    },
}
```

Add assertions after the existing volume assertion:

```python
assert result.iloc[0]["quote_asof"] == "2026-07-06T08:45:12-04:00"
```

Update empty-frame assertions in `test_inquire_overseas_price_empty_when_no_last` and `test_inquire_overseas_price_empty_when_last_non_positive`:

```python
assert list(result.columns) == ["close", "previous_close", "volume", "quote_asof"]
```

Add this parser-only test in `TestKISOverseasPrice`:

```python
def test_build_overseas_price_frame_omits_quote_asof_when_time_missing(self):
    from app.services.brokers.kis.overseas_market_data import OverseasMarketDataMixin

    result = OverseasMarketDataMixin._build_overseas_price_frame(
        {"last": "402.10", "base": "400.00", "tvol": "9000"}
    )

    assert not result.empty
    assert result.iloc[0]["quote_asof"] is None
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```bash
uv run pytest tests/test_services_kis_market_data.py -k overseas_price -v
```

Expected: fails because the frame does not include `quote_asof`.

- [ ] **Step 3: Implement `quote_asof` parsing**

In `app/services/brokers/kis/overseas_market_data.py`, add the timezone import near the top:

```python
from zoneinfo import ZoneInfo
```

Add module constant near the existing helpers/constants:

```python
_ET = ZoneInfo("America/New_York")
```

Add this static helper above `_build_overseas_price_frame`:

```python
    @staticmethod
    def _parse_overseas_quote_asof(out: dict[str, Any]) -> str | None:
        date_raw = out.get("xymd") or out.get("date")
        time_raw = out.get("xhms") or out.get("time")
        if date_raw in (None, "") or time_raw in (None, ""):
            return None

        date_text = str(date_raw).strip()
        time_text = str(time_raw).strip().zfill(6)
        if len(date_text) != 8 or len(time_text) != 6:
            return None

        try:
            parsed = datetime.datetime.strptime(
                f"{date_text}{time_text}", "%Y%m%d%H%M%S"
            ).replace(tzinfo=_ET)
        except ValueError:
            return None
        return parsed.isoformat()
```

Change `_build_overseas_price_frame`:

```python
empty_cols = ["close", "previous_close", "volume", "quote_asof"]
```

And add the parsed value to the returned row:

```python
"quote_asof": OverseasMarketDataMixin._parse_overseas_quote_asof(out),
```

- [ ] **Step 4: Run the focused tests and confirm pass**

Run:

```bash
uv run pytest tests/test_services_kis_market_data.py -k overseas_price -v
```

Expected: overseas price tests pass.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add app/services/brokers/kis/overseas_market_data.py tests/test_services_kis_market_data.py
git commit -m "feat(ROB-748): preserve KIS overseas quote as-of"
```

---

### Task 3: Tag US `get_quote` Responses

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Modify: `tests/test_mcp_quotes_tools.py`

**Interfaces:**
- Produces helper: `def _tag_us_quote_session(quote: dict[str, Any], *, now: datetime.datetime | None = None) -> dict[str, Any]`
- Adds US quote keys: `session`, `data_state`, `price_source`, optional `quote_asof`, optional `venue`, optional `data_state_reason`
- Preserves US quote exception behavior: US failures still raise for tool-level errors

- [ ] **Step 1: Write failing US `get_quote` tests**

Append near the existing US equity tests in `tests/test_mcp_quotes_tools.py`:

```python
@pytest.mark.asyncio
async def test_get_quote_us_kis_tags_premarket_session_and_quote_asof(monkeypatch):
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    _patch_runtime_attr(monkeypatch, "us_market_session", lambda *a, **k: "premarket")

    price_df = pd.DataFrame(
        [
            {
                "close": 195.29,
                "previous_close": 194.83,
                "volume": 123456,
                "quote_asof": "2026-07-06T08:45:12-04:00",
            }
        ]
    )

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            return price_df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        yahoo_service,
        "fetch_fast_info",
        AsyncMock(side_effect=AssertionError("Yahoo should not be called")),
    )

    result = await tools["get_quote"]("NVDA", market="us")

    assert result["source"] == "kis_overseas"
    assert result["price"] == pytest.approx(195.29)
    assert result["session"] == "premarket"
    assert result["data_state"] == "fresh"
    assert result["quote_asof"] == "2026-07-06T08:45:12-04:00"
    assert result["price_source"] == "kis_overseas_last"
    assert result["venue"] == "NASD"
    assert result["delayed"] is True


@pytest.mark.asyncio
async def test_get_quote_us_closed_session_is_stale(monkeypatch):
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NYSE")
    )
    _patch_runtime_attr(monkeypatch, "us_market_session", lambda *a, **k: "closed")

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            return pd.DataFrame(
                [{"close": 205.0, "previous_close": 201.5, "volume": 100}]
            )

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("AAPL", market="us")

    assert result["session"] == "closed"
    assert result["data_state"] == "stale"
    assert result["data_state_reason"] == "us_market_closed"
    assert result["price_source"] == "kis_overseas_last"


@pytest.mark.asyncio
async def test_get_quote_us_yahoo_fallback_tags_session_and_price_source(monkeypatch):
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    _patch_runtime_attr(monkeypatch, "us_market_session", lambda *a, **k: "regular")

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            return pd.DataFrame(columns=["close", "previous_close", "volume", "quote_asof"])

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        yahoo_service,
        "fetch_fast_info",
        AsyncMock(
            return_value={
                "close": 205.0,
                "previous_close": 201.5,
                "open": 202.0,
                "high": 206.2,
                "low": 200.8,
                "volume": 123456789,
            }
        ),
    )

    result = await tools["get_quote"]("AAPL", market="us")

    assert result["source"] == "yahoo"
    assert result["session"] == "regular"
    assert result["data_state"] == "fresh"
    assert result["price_source"] == "yahoo_fast_info_close"
    assert "quote_asof" not in result
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```bash
uv run pytest tests/test_mcp_quotes_tools.py -k "get_quote_us and (session or quote_asof or fallback_tags)" -v
```

Expected: new tests fail because US quote responses lack session/freshness labels.

- [ ] **Step 3: Import US session helpers**

In `app/mcp_server/tooling/market_data_quotes.py`, extend the `market_session` import:

```python
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_PREMARKET_UNAVAILABLE,
    DATA_STATE_STALE,
    US_SESSION_CLOSED,
    is_kr_session_day,
    kr_market_data_state,
    us_market_session,
)
```

- [ ] **Step 4: Add US quote tagging helper**

Add below `_fetch_us_quote_from_kis` or above `_fetch_quote_equity_us`:

```python
_US_MARKET_CLOSED_REASON = "us_market_closed"


def _optional_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _tag_us_quote_session(
    quote: dict[str, Any], *, now: datetime.datetime | None = None
) -> dict[str, Any]:
    session = us_market_session(now)
    quote["session"] = session
    if session == US_SESSION_CLOSED:
        quote["data_state"] = DATA_STATE_STALE
        quote["data_state_reason"] = _US_MARKET_CLOSED_REASON
    else:
        quote["data_state"] = DATA_STATE_FRESH
        quote.pop("data_state_reason", None)
    if "price_source" not in quote:
        quote["price_source"] = (
            "kis_overseas_last"
            if quote.get("source") == "kis_overseas"
            else "yahoo_fast_info_close"
        )
    return quote
```

- [ ] **Step 5: Thread KIS venue, price source, and quote timestamp**

In `_fetch_us_quote_from_kis`, add these values to the returned dict:

```python
"quote_asof": _optional_text(row.get("quote_asof")),
"venue": exchange_code,
"price_source": "kis_overseas_last",
```

Then remove null `quote_asof` before returning:

```python
quote = {
    "symbol": normalized_symbol,
    "instrument_type": "equity_us",
    "price": price,
    "previous_close": _to_float_or_none(row.get("previous_close")),
    "open": None,
    "high": None,
    "low": None,
    "volume": _to_int_or_none(row.get("volume")),
    "source": "kis_overseas",
    "delayed": True,
    "venue": exchange_code,
    "price_source": "kis_overseas_last",
}
quote_asof = _optional_text(row.get("quote_asof"))
if quote_asof is not None:
    quote["quote_asof"] = quote_asof
return quote
```

- [ ] **Step 6: Apply the tag in both KIS and Yahoo success paths**

In `_fetch_quote_equity_us`, replace:

```python
if kis_quote is not None:
    return kis_quote
```

with:

```python
if kis_quote is not None:
    return _tag_us_quote_session(kis_quote)
```

Replace the final Yahoo return dict with:

```python
return _tag_us_quote_session(
    {
        "symbol": normalized_symbol,
        "instrument_type": "equity_us",
        "price": price,
        "previous_close": _to_float_or_none(fast_info.get("previous_close")),
        "open": _to_float_or_none(fast_info.get("open")),
        "high": _to_float_or_none(fast_info.get("high")),
        "low": _to_float_or_none(fast_info.get("low")),
        "volume": _to_int_or_none(fast_info.get("volume")),
        "source": "yahoo",
        "delayed": True,
        "price_source": "yahoo_fast_info_close",
    }
)
```

- [ ] **Step 7: Run the US quote tests**

Run:

```bash
uv run pytest tests/test_mcp_quotes_tools.py -k "get_quote_us" -v
```

Expected: all US `get_quote` tests pass.

- [ ] **Step 8: Commit Task 3**

Run:

```bash
git add app/mcp_server/tooling/market_data_quotes.py tests/test_mcp_quotes_tools.py
git commit -m "feat(ROB-748): tag US quote session freshness"
```

---

### Task 4: Pass US Labels Through `analyze_stock_batch`

**Files:**
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py`
- Create: `tests/mcp_server/test_analyze_stock_batch_us_quote_session.py`

**Interfaces:**
- Consumes: `analysis["quote"]` produced by `analysis_analyze._get_quote_impl` for `equity_us`
- Produces compact summary keys: `price_source`, `session`, `data_state`, `data_state_reason`, `venue`, `quote_asof`, `delayed`

- [ ] **Step 1: Write failing compact summary test**

Create `tests/mcp_server/test_analyze_stock_batch_us_quote_session.py`:

```python
from app.mcp_server.tooling.analysis_tool_handlers import _summarize_analysis_result


def test_summarize_analysis_result_passes_us_quote_session_freshness_fields():
    result = _summarize_analysis_result(
        "NVDA",
        {
            "market_type": "equity_us",
            "source": "yahoo",
            "quote": {
                "symbol": "NVDA",
                "instrument_type": "equity_us",
                "price": 195.29,
                "source": "kis_overseas",
                "session": "premarket",
                "data_state": "fresh",
                "price_source": "kis_overseas_last",
                "venue": "NASD",
                "quote_asof": "2026-07-06T08:45:12-04:00",
                "delayed": True,
            },
            "indicators": {"rsi": {"14": 61.2}},
            "support_resistance": {"supports": [], "resistances": []},
            "opinions": {"consensus": {"rating": "buy"}},
            "recommendation": {"action": "hold"},
        },
    )

    assert result["current_price"] == 195.29
    assert result["session"] == "premarket"
    assert result["data_state"] == "fresh"
    assert result["price_source"] == "kis_overseas_last"
    assert result["venue"] == "NASD"
    assert result["quote_asof"] == "2026-07-06T08:45:12-04:00"
    assert result["delayed"] is True
```

- [ ] **Step 2: Run the focused test and confirm failure**

Run:

```bash
uv run pytest tests/mcp_server/test_analyze_stock_batch_us_quote_session.py -v
```

Expected: fails on missing `quote_asof` and `delayed`.

- [ ] **Step 3: Extend compact quote provenance pass-through**

In `app/mcp_server/tooling/analysis_tool_handlers.py`, replace:

```python
for _px_key in ("price_source", "session", "data_state", "venue"):
```

with:

```python
for _px_key in (
    "price_source",
    "session",
    "data_state",
    "data_state_reason",
    "venue",
    "quote_asof",
    "delayed",
):
```

- [ ] **Step 4: Run compact summary tests**

Run:

```bash
uv run pytest tests/mcp_server/test_analyze_stock_batch_us_quote_session.py tests/test_analyze_stock_kr_live_price.py tests/test_analyze_stock_batch_cache.py -v
```

Expected: all pass. The KR cache test protects the ROB-725 compact pass-through that already exists.

- [ ] **Step 5: Commit Task 4**

Run:

```bash
git add app/mcp_server/tooling/analysis_tool_handlers.py tests/mcp_server/test_analyze_stock_batch_us_quote_session.py
git commit -m "feat(ROB-748): pass US quote freshness into batch summaries"
```

---

### Task 5: Documentation And Verification

**Files:**
- Modify: `app/mcp_server/README.md`

**Interfaces:**
- Documents additive US fields for `get_quote` and compact `analyze_stock_batch`

- [ ] **Step 1: Update `get_quote` documentation**

In `app/mcp_server/README.md`, replace the US quote bullets around the current lines 74-76 with:

```markdown
- US equity quote price resolution uses KIS overseas current price first when `settings.us_quote_kis_primary` is enabled, then falls back to Yahoo `fast_info`.
  - US quote response keeps `source: "kis_overseas"` or `source: "yahoo"` and includes `previous_close/open/high/low/volume` when the provider supplies them.
  - US quote response includes `session` (`premarket`, `regular`, `afterhours`, `closed`), `data_state` (`fresh` during the extended-hours envelope, `stale` when closed), `price_source` (`kis_overseas_last` or `yahoo_fast_info_close`), `delayed: true`, and optional `quote_asof` when KIS supplies parseable quote date/time fields.
  - KIS-backed US quote response includes `venue` with the DB-resolved KIS exchange code (`NASD`, `NYSE`, `AMEX`) used for the upstream request.
  - US quote failures are propagated as tool-level errors (exceptions), not returned as in-band error payload dicts.
```

- [ ] **Step 2: Update `analyze_stock_batch` documentation**

Under `- analyze_stock_batch(...)`, add:

```markdown
  - Compact US rows carry the same quote provenance fields as `get_quote` when present: `session`, `data_state`, `data_state_reason`, `price_source`, `venue`, `quote_asof`, and `delayed`.
```

- [ ] **Step 3: Run focused verification**

Run:

```bash
uv run pytest tests/test_market_session.py -k "market_session" -v
uv run pytest tests/test_services_kis_market_data.py -k "overseas_price" -v
uv run pytest tests/test_mcp_quotes_tools.py -k "get_quote_us" -v
uv run pytest tests/mcp_server/test_analyze_stock_batch_us_quote_session.py -v
```

Expected: all pass.

- [ ] **Step 4: Run broader MCP smoke verification**

Run:

```bash
uv run pytest tests/test_mcp_quotes_tools.py tests/test_analyze_stock_kr_live_price.py tests/test_analyze_stock_batch_cache.py tests/mcp_server/test_analyze_stock_batch_us_quote_session.py -v
```

Expected: all pass.

- [ ] **Step 5: Run lint**

Run:

```bash
make lint
```

Expected: Ruff and ty checks pass.

- [ ] **Step 6: Commit Task 5**

Run:

```bash
git add app/mcp_server/README.md
git commit -m "docs(ROB-748): document US quote freshness labels"
```

---

## Self-Review

- Spec coverage: `get_quote` US receives `session`, `quote_asof` when provider supplies date/time, `data_state`, `price_source`, and `venue`; `analyze_stock_batch` compact summary passes those labels through; KIS overseas timestamp investigation is captured by preserving `xymd`/`xhms` from the raw current-price payload.
- Placeholder scan: no step uses placeholder implementation language; every code-changing step includes concrete code.
- Type consistency: `session` vocabulary is consistent across helper/tests/docs; `data_state` reuses existing `DATA_STATE_FRESH` and `DATA_STATE_STALE`; `quote_asof` is a string ISO timestamp or absent from public quote dicts.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-06-rob-748-us-quote-session-freshness.md`. Two execution options:

**1. Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
