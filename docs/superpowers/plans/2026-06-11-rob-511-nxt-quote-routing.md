# ROB-511 NXT Quote Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MCP `get_quote` return a live NXT-derived KR equity price during KR NXT pre-market/after-hours sessions instead of stopping at a stale KRX prior-close payload.

**Architecture:** Keep the change inside the MCP market-data tool layer because ROB-511 is about public MCP `get_quote` behavior, not shared service quote semantics used by other services. Fetch the existing KRX daily quote first to preserve `previous_close`, then overlay only `price` and NXT diagnostics when an NXT orderbook produces a usable expected price or best-bid/ask price. If NXT orderbook data is missing, empty, or errors, fall back to the current ROB-464 honest stale behavior.

**Tech Stack:** Python 3.13, FastMCP tool handlers, pandas-based test fixtures, pytest/pytest-asyncio, `uv`.

---

## File Structure

- Modify `tests/test_mcp_quotes_tools.py`
  - Add failing MCP contract tests for KR pre-market NXT expected price, NXT mid price, empty-book fallback, NXT after-hours routing, and regular-session skip.
- Modify `app/mcp_server/tooling/market_data_quotes.py`
  - Add small helper functions for NXT session detection and orderbook-derived quote overlay.
  - Update `_get_quote_impl()` so KR equity `get_quote` overlays NXT price only when the current session can use NXT evidence.
  - Update the `get_quote` tool description.
- Modify `app/mcp_server/README.md`
  - Document the KR quote NXT-session overlay, response fields, and fallback behavior.

## Public Contract

Successful NXT overlay response shape:

```json
{
  "symbol": "005930",
  "instrument_type": "equity_kr",
  "price": 114300.0,
  "previous_close": 100.0,
  "open": 100.0,
  "high": 110.0,
  "low": 99.0,
  "volume": 1000,
  "value": 105000.0,
  "source": "kis",
  "data_state": "fresh",
  "regular_session_data_state": "premarket_unavailable",
  "session": "nxt_premarket",
  "venue": "nxt",
  "venue_label": "NXT",
  "kis_market_code": "NX",
  "source_endpoint": "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
  "source_tr_id": "FHKST01010200",
  "price_source": "nxt_expected_price"
}
```

Rules:

- `previous_close` remains the KRX previous close from the existing daily quote path.
- `price` changes to NXT orderbook-derived price only when NXT evidence is usable.
- `open`, `high`, `low`, `volume`, and `value` remain the existing daily KIS fields; do not invent NXT OHLCV from an orderbook.
- `data_state` is `"fresh"` when the returned `price` is NXT-derived and usable for the active NXT session.
- `regular_session_data_state` preserves the ROB-464 KRX regular-session classifier value so callers can tell why the overlay was used.
- `session` is `"nxt_premarket"` when `kr_market_data_state()` returns `"premarket_unavailable"`.
- `session` is `"nxt_after"` when the KST clock is on a KR trading session day and `16:00 <= time < 20:00`.
- If NXT overlay fails, the payload is the current behavior: KRX daily `price`, `data_state` from `kr_market_data_state()`, and no NXT diagnostic fields.

---

### Task 1: Add Failing MCP Quote Tests

**Files:**
- Modify: `tests/test_mcp_quotes_tools.py`
- Test: `tests/test_mcp_quotes_tools.py`

- [ ] **Step 1: Add a local two-row KR daily quote fixture and NXT book helper**

Insert these helpers after `test_get_quote_korean_equity_previous_close` or directly before the new ROB-511 tests:

```python
def _two_row_kr_quote_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2024-01-01",
                "open": 98.0,
                "high": 102.0,
                "low": 97.0,
                "close": 100.0,
                "volume": 900,
                "value": 90000.0,
            },
            {
                "date": "2024-01-02",
                "open": 100.0,
                "high": 110.0,
                "low": 99.0,
                "close": 105.0,
                "volume": 1000,
                "value": 105000.0,
            },
        ]
    )


def _nxt_quote_book(
    *,
    expected_price: int | None = None,
    asks: list[tuple[float, float]] | None = None,
    bids: list[tuple[float, float]] | None = None,
    empty: bool = False,
):
    import app.services.market_data as market_data_service

    return market_data_service.OrderbookSnapshot(
        symbol="005930",
        instrument_type="equity_kr",
        source="kis",
        asks=[
            market_data_service.OrderbookLevel(price=price, quantity=qty)
            for price, qty in (asks or [])
        ],
        bids=[
            market_data_service.OrderbookLevel(price=price, quantity=qty)
            for price, qty in (bids or [])
        ],
        total_ask_qty=0.0,
        total_bid_qty=0.0,
        bid_ask_ratio=None,
        expected_price=expected_price,
        expected_qty=None,
        venue="nxt",
        venue_label="NXT",
        kis_market_code="NX",
        source_endpoint="/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        source_tr_id="FHKST01010200",
        is_empty_book=empty,
        requires_final_recheck=empty,
        empty_reason="empty_kis_orderbook" if empty else None,
    )
```

- [ ] **Step 2: Add the expected-price pre-market test**

Insert this test below the ROB-464 premarket data-state test:

```python
@pytest.mark.asyncio
async def test_get_quote_korean_equity_premarket_routes_to_nxt_expected_price(
    monkeypatch,
):
    """ROB-511: pre-market KR quote uses NXT expected price when available."""
    from app.mcp_server.tooling import market_data_quotes
    from app.mcp_server.tooling.market_session import (
        DATA_STATE_PREMARKET_UNAVAILABLE,
    )

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            assert code == "005930"
            assert market == "J"
            assert n == 2
            return df

    get_orderbook_mock = AsyncMock(
        return_value=_nxt_quote_book(expected_price=114300)
    )

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: DATA_STATE_PREMARKET_UNAVAILABLE,
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )

    result = await tools["get_quote"]("005930")

    get_orderbook_mock.assert_awaited_once_with("005930", "kr", venue="nxt")
    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["price"] == pytest.approx(114300.0)
    assert result["previous_close"] == pytest.approx(100.0)
    assert result["data_state"] == "fresh"
    assert result["regular_session_data_state"] == "premarket_unavailable"
    assert result["session"] == "nxt_premarket"
    assert result["venue"] == "nxt"
    assert result["venue_label"] == "NXT"
    assert result["kis_market_code"] == "NX"
    assert result["price_source"] == "nxt_expected_price"
```

- [ ] **Step 3: Run the new expected-price test and confirm it fails**

Run:

```bash
uv run pytest tests/test_mcp_quotes_tools.py::test_get_quote_korean_equity_premarket_routes_to_nxt_expected_price -q
```

Expected result before implementation:

```text
FAILED ... AssertionError: Expected get_orderbook to have been awaited once. Awaited 0 times.
```

- [ ] **Step 4: Add the NXT mid-price test**

Add:

```python
@pytest.mark.asyncio
async def test_get_quote_korean_equity_premarket_routes_to_nxt_mid(
    monkeypatch,
):
    """ROB-511: use NXT best bid/ask mid when expected_price is absent."""
    from app.mcp_server.tooling import market_data_quotes
    from app.mcp_server.tooling.market_session import (
        DATA_STATE_PREMARKET_UNAVAILABLE,
    )

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    get_orderbook_mock = AsyncMock(
        return_value=_nxt_quote_book(
            asks=[(114500, 10)],
            bids=[(114100, 20)],
        )
    )

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: DATA_STATE_PREMARKET_UNAVAILABLE,
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )

    result = await tools["get_quote"]("005930")

    assert result["price"] == pytest.approx(114300.0)
    assert result["price_source"] == "nxt_mid"
    assert result["session"] == "nxt_premarket"
    assert result["data_state"] == "fresh"
```

- [ ] **Step 5: Add the empty-book fallback test**

Add:

```python
@pytest.mark.asyncio
async def test_get_quote_korean_equity_premarket_empty_nxt_book_keeps_stale_flag(
    monkeypatch,
):
    """ROB-511: empty NXT book keeps ROB-464 honest stale quote behavior."""
    from app.mcp_server.tooling import market_data_quotes
    from app.mcp_server.tooling.market_session import (
        DATA_STATE_PREMARKET_UNAVAILABLE,
    )

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: DATA_STATE_PREMARKET_UNAVAILABLE,
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=_nxt_quote_book(empty=True)),
    )

    result = await tools["get_quote"]("005930")

    assert result["price"] == pytest.approx(105.0)
    assert result["previous_close"] == pytest.approx(100.0)
    assert result["data_state"] == "premarket_unavailable"
    assert "regular_session_data_state" not in result
    assert "session" not in result
    assert "venue" not in result
    assert "price_source" not in result
```

- [ ] **Step 6: Add the after-hours NXT routing test**

Add:

```python
@pytest.mark.asyncio
async def test_get_quote_korean_equity_after_hours_routes_to_nxt(monkeypatch):
    """ROB-511: KR trading-day NXT after-hours quote also uses NXT evidence."""
    from app.mcp_server.tooling import market_data_quotes

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    get_orderbook_mock = AsyncMock(
        return_value=_nxt_quote_book(expected_price=113900)
    )

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: "market_closed",
    )
    monkeypatch.setattr(market_data_quotes, "is_kr_session_day", lambda date: True)
    monkeypatch.setattr(
        market_data_quotes,
        "now_kst",
        lambda: pd.Timestamp("2026-06-11 17:00:00", tz="Asia/Seoul").to_pydatetime(),
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )

    result = await tools["get_quote"]("005930")

    get_orderbook_mock.assert_awaited_once_with("005930", "kr", venue="nxt")
    assert result["price"] == pytest.approx(113900.0)
    assert result["data_state"] == "fresh"
    assert result["regular_session_data_state"] == "market_closed"
    assert result["session"] == "nxt_after"
    assert result["venue"] == "nxt"
    assert result["price_source"] == "nxt_expected_price"
```

- [ ] **Step 7: Add the regular-session skip test**

Add:

```python
@pytest.mark.asyncio
async def test_get_quote_korean_equity_regular_session_skips_nxt_orderbook(
    monkeypatch,
):
    """ROB-511: regular KRX session keeps the existing daily quote path."""
    from app.mcp_server.tooling import market_data_quotes
    from app.mcp_server.tooling.market_session import DATA_STATE_FRESH

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    get_orderbook_mock = AsyncMock()

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: DATA_STATE_FRESH,
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )

    result = await tools["get_quote"]("005930")

    get_orderbook_mock.assert_not_awaited()
    assert result["price"] == pytest.approx(105.0)
    assert result["previous_close"] == pytest.approx(100.0)
    assert result["data_state"] == "fresh"
    assert "regular_session_data_state" not in result
    assert "session" not in result
```

- [ ] **Step 8: Run all new ROB-511 quote tests and confirm they fail before implementation**

Run:

```bash
uv run pytest \
  tests/test_mcp_quotes_tools.py::test_get_quote_korean_equity_premarket_routes_to_nxt_expected_price \
  tests/test_mcp_quotes_tools.py::test_get_quote_korean_equity_premarket_routes_to_nxt_mid \
  tests/test_mcp_quotes_tools.py::test_get_quote_korean_equity_premarket_empty_nxt_book_keeps_stale_flag \
  tests/test_mcp_quotes_tools.py::test_get_quote_korean_equity_after_hours_routes_to_nxt \
  tests/test_mcp_quotes_tools.py::test_get_quote_korean_equity_regular_session_skips_nxt_orderbook \
  -q
```

Expected result before implementation:

```text
FAILED ... test_get_quote_korean_equity_premarket_routes_to_nxt_expected_price
FAILED ... test_get_quote_korean_equity_premarket_routes_to_nxt_mid
FAILED ... test_get_quote_korean_equity_after_hours_routes_to_nxt
```

The empty-book fallback and regular-session skip tests may pass before implementation if the implementation still never calls NXT. Keep them because they protect the fallback and non-NXT paths after the overlay is added.

- [ ] **Step 9: Commit the failing tests**

Run:

```bash
git add tests/test_mcp_quotes_tools.py
git commit -m "test(ROB-511): cover NXT quote routing"
```

---

### Task 2: Implement NXT Price Overlay in `get_quote`

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Test: `tests/test_mcp_quotes_tools.py`

- [ ] **Step 1: Import NXT session-day helper**

Change the existing `market_session` import from:

```python
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    kr_market_data_state,
)
```

to:

```python
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    DATA_STATE_PREMARKET_UNAVAILABLE,
    is_kr_session_day,
    kr_market_data_state,
)
```

- [ ] **Step 2: Add NXT quote helper functions**

Add these helpers after `_validate_crypto_orderbook_symbol_input()` and before `_build_orderbook_walls_for_side()`:

```python
_NXT_AFTER_OPEN = datetime.time(16, 0)
_NXT_AFTER_CLOSE = datetime.time(20, 0)
_KST = ZoneInfo("Asia/Seoul")


def _current_kst_datetime(now: datetime.datetime | None = None) -> datetime.datetime:
    current = now or now_kst()
    if current.tzinfo is None:
        return current.replace(tzinfo=_KST)
    return current.astimezone(_KST)


def _nxt_quote_session(
    data_state: str,
    *,
    now: datetime.datetime | None = None,
) -> str | None:
    if data_state == DATA_STATE_PREMARKET_UNAVAILABLE:
        return "nxt_premarket"

    current = _current_kst_datetime(now)
    if not is_kr_session_day(current.date()):
        return None

    current_time = current.timetz().replace(tzinfo=None)
    if _NXT_AFTER_OPEN <= current_time < _NXT_AFTER_CLOSE:
        return "nxt_after"
    return None


def _positive_price(value: float | int | None) -> float | None:
    try:
        if value is None:
            return None
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _nxt_price_from_orderbook(
    snapshot: market_data_service.OrderbookSnapshot,
) -> tuple[float | None, str | None]:
    if snapshot.is_empty_book:
        return None, None

    expected_price = _positive_price(snapshot.expected_price)
    if expected_price is not None:
        return expected_price, "nxt_expected_price"

    best_ask = _positive_price(snapshot.asks[0].price if snapshot.asks else None)
    best_bid = _positive_price(snapshot.bids[0].price if snapshot.bids else None)
    if best_ask is not None and best_bid is not None:
        return (best_ask + best_bid) / 2.0, "nxt_mid"
    if best_ask is not None:
        return best_ask, "nxt_best_ask"
    if best_bid is not None:
        return best_bid, "nxt_best_bid"
    return None, None


async def _fetch_nxt_quote_overlay(
    symbol: str,
    *,
    session: str,
) -> dict[str, Any] | None:
    try:
        snapshot = await market_data_service.get_orderbook(symbol, "kr", venue="nxt")
    except Exception as exc:
        logger.warning("NXT quote overlay failed for %s: %s", symbol, exc)
        return None

    price, price_source = _nxt_price_from_orderbook(snapshot)
    if price is None or price_source is None:
        return None

    overlay: dict[str, Any] = {
        "price": price,
        "session": session,
        "venue": snapshot.venue or "nxt",
        "price_source": price_source,
    }
    if snapshot.venue_label is not None:
        overlay["venue_label"] = snapshot.venue_label
    if snapshot.kis_market_code is not None:
        overlay["kis_market_code"] = snapshot.kis_market_code
    if snapshot.source_endpoint is not None:
        overlay["source_endpoint"] = snapshot.source_endpoint
    if snapshot.source_tr_id is not None:
        overlay["source_tr_id"] = snapshot.source_tr_id
    return overlay
```

- [ ] **Step 3: Update the KR branch of `_get_quote_impl()`**

Replace the current KR branch:

```python
        # ROB-464: tag the KRX session so a pre-market / closed-session prior
        # close is not mistaken for a live price. The shared fetcher (used by
        # orders/portfolio) is left untouched; only the get_quote tool adds this.
        quote = await _fetch_quote_equity_kr(symbol)
        quote["data_state"] = kr_market_data_state()
        return quote
```

with:

```python
        # ROB-464: tag stale KRX regular-session data honestly. ROB-511 overlays
        # an NXT-derived price during NXT sessions while preserving the KRX
        # previous_close used for gap calculations.
        data_state = kr_market_data_state()
        quote = await _fetch_quote_equity_kr(symbol)
        session = _nxt_quote_session(data_state)
        if session is not None:
            overlay = await _fetch_nxt_quote_overlay(symbol, session=session)
            if overlay is not None:
                quote.update(overlay)
                quote["regular_session_data_state"] = data_state
                quote["data_state"] = DATA_STATE_FRESH
                return quote

        quote["data_state"] = data_state
        return quote
```

- [ ] **Step 4: Run the focused ROB-511 tests**

Run:

```bash
uv run pytest \
  tests/test_mcp_quotes_tools.py::test_get_quote_korean_equity_premarket_routes_to_nxt_expected_price \
  tests/test_mcp_quotes_tools.py::test_get_quote_korean_equity_premarket_routes_to_nxt_mid \
  tests/test_mcp_quotes_tools.py::test_get_quote_korean_equity_premarket_empty_nxt_book_keeps_stale_flag \
  tests/test_mcp_quotes_tools.py::test_get_quote_korean_equity_after_hours_routes_to_nxt \
  tests/test_mcp_quotes_tools.py::test_get_quote_korean_equity_regular_session_skips_nxt_orderbook \
  -q
```

Expected result:

```text
5 passed
```

- [ ] **Step 5: Run the full quote and orderbook MCP test files**

Run:

```bash
uv run pytest tests/test_mcp_quotes_tools.py tests/test_mcp_orderbook_tools.py -q
```

Expected result:

```text
passed
```

- [ ] **Step 6: Run the existing order-validation NXT regression tests**

Run:

```bash
uv run pytest tests/test_mcp_place_order.py::TestPremarketNxtPricing -q
```

Expected result:

```text
4 passed
```

- [ ] **Step 7: Commit the implementation**

Run:

```bash
git add app/mcp_server/tooling/market_data_quotes.py
git commit -m "feat(ROB-511): route KR NXT quotes through orderbook"
```

---

### Task 3: Document the MCP Contract

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Modify: `app/mcp_server/README.md`
- Test: `tests/test_mcp_quotes_tools.py`

- [ ] **Step 1: Update the `get_quote` tool description**

In `_register_market_data_tools_impl()`, replace:

```python
        description="Get latest quote/last price for a symbol (KR equity / US equity / crypto).",
```

with:

```python
        description=(
            "Get latest quote/last price for a symbol (KR equity / US equity / crypto). "
            "For KR equities during NXT pre-market/after-hours sessions, price falls "
            "back to the NXT orderbook expected price or best bid/ask mid while "
            "preserving KRX previous_close for gap calculations."
        ),
```

- [ ] **Step 2: Add README bullets under Market Data Tools**

In `app/mcp_server/README.md`, under the existing `get_quote` bullet near the Market Data Tools section, add:

```markdown
- KR equity `get_quote` uses KRX daily quote data for the regular-session baseline and includes `previous_close` when at least two daily rows are available.
  - During KR NXT pre-market (`session: "nxt_premarket"`) and trading-day NXT after-hours (`session: "nxt_after"`), KR `get_quote` overlays `price` from `get_orderbook(symbol, market="kr", venue="nxt")`.
  - NXT price selection order is `expected_price` (`price_source: "nxt_expected_price"`), then best bid/ask mid (`"nxt_mid"`), then a single available best ask or bid (`"nxt_best_ask"` / `"nxt_best_bid"`).
  - A successful NXT overlay returns `data_state: "fresh"`, `regular_session_data_state` with the KRX classifier value, and venue diagnostics (`venue`, `venue_label`, `kis_market_code`, `source_endpoint`, `source_tr_id`) when KIS supplies them.
  - If the NXT orderbook is empty or unavailable, `get_quote` keeps the ROB-464 stale-session behavior: KRX daily `price`, `data_state` from `kr_market_data_state()`, and no NXT diagnostic fields.
```

- [ ] **Step 3: Run quote tests after docs/description edit**

Run:

```bash
uv run pytest tests/test_mcp_quotes_tools.py -q
```

Expected result:

```text
passed
```

- [ ] **Step 4: Commit docs and description**

Run:

```bash
git add app/mcp_server/tooling/market_data_quotes.py app/mcp_server/README.md
git commit -m "docs(ROB-511): describe KR NXT quote overlay"
```

---

### Task 4: Final Verification and Linear/PR Notes

**Files:**
- Verify: `tests/test_mcp_quotes_tools.py`
- Verify: `tests/test_mcp_orderbook_tools.py`
- Verify: `tests/test_mcp_place_order.py`
- Verify: `app/mcp_server/README.md`

- [ ] **Step 1: Run the focused regression suite**

Run:

```bash
uv run pytest \
  tests/test_mcp_quotes_tools.py \
  tests/test_mcp_orderbook_tools.py \
  tests/test_mcp_place_order.py::TestPremarketNxtPricing \
  -q
```

Expected result:

```text
passed
```

- [ ] **Step 2: Run lint on touched Python files**

Run:

```bash
uv run ruff check app/mcp_server/tooling/market_data_quotes.py tests/test_mcp_quotes_tools.py
```

Expected result:

```text
All checks passed!
```

- [ ] **Step 3: Inspect the final diff**

Run:

```bash
git diff --stat HEAD~3..HEAD
git diff HEAD~3..HEAD -- app/mcp_server/tooling/market_data_quotes.py tests/test_mcp_quotes_tools.py app/mcp_server/README.md
```

Expected review points:

```text
tests/test_mcp_quotes_tools.py includes ROB-511 NXT overlay tests.
market_data_quotes.py overlays only price and diagnostics during NXT sessions.
market_data_quotes.py preserves KRX fallback behavior when NXT evidence is unusable.
README documents response fields and fallback behavior.
```

- [ ] **Step 4: Prepare the Linear/PR risk note**

Use this exact note in the PR description or Linear comment:

```markdown
ROB-511 implements KR `get_quote` NXT session price overlay using the already-supported KIS NX orderbook path. Scope is read-only MCP quote behavior: no order placement, DB schema, auth, or deployment automation changes.

Model-lane tags: `candidate_for_sonnet`.
Reason: trading-decision quote semantics change during NXT sessions, but the implementation is read-only and locally scoped.

Verification:
- `uv run pytest tests/test_mcp_quotes_tools.py tests/test_mcp_orderbook_tools.py tests/test_mcp_place_order.py::TestPremarketNxtPricing -q`
- `uv run ruff check app/mcp_server/tooling/market_data_quotes.py tests/test_mcp_quotes_tools.py`
```

- [ ] **Step 5: Final commit or squash**

If the repository convention for this branch prefers one commit per Linear issue, squash the three task commits into one non-interactive commit:

```bash
git reset --soft HEAD~3
git commit -m "feat(ROB-511): route KR NXT quotes through orderbook"
```

If preserving TDD commits is preferred for review, keep the three commits from Tasks 1-3.

---

## Self-Review

Spec coverage:

- ROB-511 `get_quote` pre-market no longer stops at `premarket_unavailable`: covered by Task 1 expected-price and mid-price tests, implemented in Task 2.
- NXT real price uses existing KIS NX path from `get_orderbook(venue="nxt")`: covered by `get_orderbook_mock.assert_awaited_once_with("005930", "kr", venue="nxt")` and Task 2 `_fetch_nxt_quote_overlay`.
- `previous_close` remains KRX close for gap calculations: covered by tests asserting `previous_close == 100.0`.
- `session: "nxt_premarket"` / `"nxt_after"` is explicit: covered by Task 1 tests and Task 2 `_nxt_quote_session`.
- Empty/unavailable NXT evidence falls back to ROB-464 honest stale behavior: covered by empty-book test and exception fallback in `_fetch_nxt_quote_overlay`.
- Tool description and README include the operational route: covered by Task 3.

Placeholder scan:

- The plan contains no unfinished placeholder markers, incomplete code names, or unbound helper references.

Type consistency:

- `OrderbookSnapshot`, `OrderbookLevel`, and `market_data_service.get_orderbook(symbol, "kr", venue="nxt")` match the current service contracts.
- `DATA_STATE_FRESH`, `DATA_STATE_PREMARKET_UNAVAILABLE`, `is_kr_session_day`, and `kr_market_data_state` are imported from `app.mcp_server.tooling.market_session`.
- New helper names are local to `market_data_quotes.py` and are not public API.
