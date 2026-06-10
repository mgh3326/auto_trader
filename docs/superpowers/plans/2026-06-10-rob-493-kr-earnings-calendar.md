# ROB-493 KR Earnings Calendar MCP Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `get_earnings_calendar` so Korean equities can read KR earnings events from the existing `market_events` read model without adding a new MCP tool.

**Status (2026-06-10):** Implemented in this branch. Post-review follow-up tightened auto-routing so only 6-digit or `A` + 6-digit KR equity codes route to KR, and normalized the default US date window to `from_date + 30 days`.

**Architecture:** Keep `get_earnings_calendar` as the single public surface. US requests continue to call Finnhub exactly as today; KR requests route to `MarketEventsQueryService` and read existing `(category="earnings", market="kr")` rows from `market_events`. This is read-only: no DDL, no ingestion activation, no broker/order/strategy side effects.

**Tech Stack:** Python 3.13, FastMCP, SQLAlchemy async sessions, Pydantic schemas, pytest, Ruff.

---

## Decisions

- Do not create `get_kr_events` in this slice. ROB-468 prefers tool surface cleanup/consolidation; extending the existing tool is the smallest compatible change.
- Do not add new tables or migrations. `market_events` already stores WiseFn KR earnings schedules and DART rows classified as earnings.
- Do not enable production WiseFn scheduling or flags here. This PR only exposes what is already ingested.
- Do not implement shareholder meetings, ex-dividend dates, IR, or conferences here. Those require source-contract decisions and should be separate ingestion follow-ups.
- Return an explicit KR warning so callers understand that KR coverage is limited to `market_events` earnings rows and does not imply full corporate event coverage.

## File Structure

- Modify `app/services/market_events/query_service.py`
  - Add optional `symbol` filtering to `list_for_date`, `list_for_range`, and `_query`.
  - Normalize A-prefixed KR codes like `A005930` to `005930`.
- Modify `app/mcp_server/tooling/fundamentals/_financials.py`
  - Add `market` support to `handle_get_earnings_calendar`.
  - Add a KR read-only path backed by `MarketEventsQueryService`.
  - Preserve the existing US Finnhub path and response shape.
- Modify `app/mcp_server/tooling/fundamentals_handlers.py`
  - Add the `market` parameter to the registered MCP function.
  - Update the tool description from US-only to US/KR.
- Modify `app/mcp_server/README.md`
  - Document the public `get_earnings_calendar` contract and KR limitations.
- Modify `tests/services/test_market_events_query_service.py`
  - Add integration coverage for symbol filtering.
- Create `tests/test_mcp_earnings_calendar.py`
  - Add MCP handler and registration tests for US/KR routing.

---

### Task 1: Add Symbol Filtering To MarketEventsQueryService

**Files:**
- Modify: `app/services/market_events/query_service.py:9-143`
- Modify: `tests/services/test_market_events_query_service.py`

- [ ] **Step 1: Write the failing query-service test**

Append this test to `tests/services/test_market_events_query_service.py`:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_events_filters_by_symbol(db_session):
    from app.services.market_events.query_service import MarketEventsQueryService
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "kr",
            "symbol": "005930",
            "company_name": "삼성전자",
            "title": "삼성전자 2026년 1분기 실적발표 예정",
            "event_date": date(2026, 5, 13),
            "time_hint": "after_close",
            "status": "scheduled",
            "source": "wisefn",
            "source_event_id": "wisefn::005930::2026-05-13::2026::1",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
        },
        [],
    )
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "kr",
            "symbol": "000660",
            "company_name": "SK하이닉스",
            "title": "SK하이닉스 2026년 1분기 실적발표 예정",
            "event_date": date(2026, 5, 13),
            "time_hint": "before_open",
            "status": "scheduled",
            "source": "wisefn",
            "source_event_id": "wisefn::000660::2026-05-13::2026::1",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
        },
        [],
    )
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "us",
            "symbol": "AAPL",
            "title": "AAPL earnings release",
            "event_date": date(2026, 5, 13),
            "status": "scheduled",
            "source": "finnhub",
            "fiscal_year": 2026,
            "fiscal_quarter": 2,
        },
        [],
    )
    await db_session.flush()

    svc = MarketEventsQueryService(db_session)

    response = await svc.list_for_range(
        date(2026, 5, 13),
        date(2026, 5, 13),
        category="earnings",
        market="kr",
        symbol="A005930",
    )

    assert response.count == 1
    assert response.events[0].symbol == "005930"
    assert response.events[0].company_name == "삼성전자"

    unfiltered = await svc.list_for_range(
        date(2026, 5, 13),
        date(2026, 5, 13),
        category="earnings",
        market="kr",
    )
    assert unfiltered.count == 2
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
uv run pytest tests/services/test_market_events_query_service.py::test_list_events_filters_by_symbol -v
```

Expected: FAIL with a `TypeError` like:

```text
MarketEventsQueryService.list_for_range() got an unexpected keyword argument 'symbol'
```

- [ ] **Step 3: Implement the minimal query-service change**

In `app/services/market_events/query_service.py`, add this helper below the imports:

```python
def _normalize_symbol_filter(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    normalized = str(symbol).strip().upper()
    if not normalized:
        return None
    if len(normalized) == 7 and normalized.startswith("A") and normalized[1:].isdigit():
        return normalized[1:]
    if normalized.isdigit() and len(normalized) < 6:
        return normalized.zfill(6)
    return normalized
```

Update the service methods to pass `symbol` through:

```python
async def list_for_date(
    self,
    target_date: date,
    *,
    category: str | None = None,
    market: str | None = None,
    source: str | None = None,
    symbol: str | None = None,
) -> MarketEventsDayResponse:
    events = await self._query(
        from_date=target_date,
        to_date=target_date,
        category=category,
        market=market,
        source=source,
        symbol=symbol,
    )
    return MarketEventsDayResponse(date=target_date, events=events)

async def list_for_range(
    self,
    from_date: date,
    to_date: date,
    *,
    category: str | None = None,
    market: str | None = None,
    source: str | None = None,
    symbol: str | None = None,
) -> MarketEventsRangeResponse:
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    events = await self._query(
        from_date=from_date,
        to_date=to_date,
        category=category,
        market=market,
        source=source,
        symbol=symbol,
    )
    return MarketEventsRangeResponse(
        from_date=from_date,
        to_date=to_date,
        count=len(events),
        events=events,
    )
```

Update `_query`:

```python
async def _query(
    self,
    *,
    from_date: date,
    to_date: date,
    category: str | None,
    market: str | None,
    source: str | None,
    symbol: str | None,
) -> list[MarketEventResponse]:
    if category is not None:
        validate_category(category)
    if market is not None:
        validate_market(market)

    normalized_symbol = _normalize_symbol_filter(symbol)

    stmt = (
        select(MarketEvent)
        .where(
            MarketEvent.event_date >= from_date,
            MarketEvent.event_date <= to_date,
        )
        .order_by(MarketEvent.event_date.asc(), MarketEvent.symbol.asc())
    )
    if category is not None:
        stmt = stmt.where(MarketEvent.category == category)
    if market is not None:
        stmt = stmt.where(MarketEvent.market == market)
    if source is not None:
        stmt = stmt.where(MarketEvent.source == source)
    if normalized_symbol is not None:
        stmt = stmt.where(MarketEvent.symbol == normalized_symbol)
```

Keep the existing row-to-`MarketEventResponse` mapping unchanged after the new filter block.

- [ ] **Step 4: Verify the query-service test passes**

Run:

```bash
uv run pytest tests/services/test_market_events_query_service.py::test_list_events_filters_by_symbol -v
```

Expected: PASS.

- [ ] **Step 5: Commit query-service change**

```bash
git add app/services/market_events/query_service.py tests/services/test_market_events_query_service.py
git commit -m "feat(ROB-493): filter market events by symbol"
```

---

### Task 2: Add KR Read Path To get_earnings_calendar Handler

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals/_financials.py:1-137`
- Create: `tests/test_mcp_earnings_calendar.py`

- [ ] **Step 1: Write failing MCP handler tests**

Create `tests/test_mcp_earnings_calendar.py`:

```python
import inspect
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from tests._mcp_tooling_support import DummySessionManager


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}
        self.descriptions: dict[str, str] = {}

    def tool(self, name: str, description: str):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = func
            self.descriptions[name] = description
            return func

        return decorator


@pytest.mark.asyncio
@pytest.mark.integration
async def test_kr_earnings_calendar_reads_market_events(db_session, monkeypatch):
    from app.mcp_server.tooling.fundamentals import _financials as financials
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "kr",
            "symbol": "005930",
            "company_name": "삼성전자",
            "title": "삼성전자 2026년 1분기 실적발표 예정",
            "event_date": financials.datetime.date(2026, 5, 13),
            "time_hint": "after_close",
            "status": "scheduled",
            "source": "wisefn",
            "source_event_id": "wisefn::005930::2026-05-13::2026::1",
            "source_url": None,
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
        },
        [],
    )
    await db_session.flush()

    monkeypatch.setattr(
        financials,
        "AsyncSessionLocal",
        lambda: DummySessionManager(db_session),
    )
    monkeypatch.setattr(
        financials,
        "_fetch_earnings_calendar_finnhub",
        AsyncMock(side_effect=AssertionError("KR path must not call Finnhub")),
    )

    result = await financials.handle_get_earnings_calendar(
        symbol="A005930",
        from_date="2026-05-01",
        to_date="2026-05-31",
    )

    assert result["instrument_type"] == "equity_kr"
    assert result["market"] == "kr"
    assert result["source"] == "market_events"
    assert result["sources"] == ["wisefn"]
    assert result["symbol"] == "005930"
    assert result["from_date"] == "2026-05-01"
    assert result["to_date"] == "2026-05-31"
    assert result["count"] == 1
    assert result["earnings"] == [
        {
            "symbol": "005930",
            "company_name": "삼성전자",
            "date": "2026-05-13",
            "hour": "after_close",
            "time_hint": "after_close",
            "eps_estimate": None,
            "eps_actual": None,
            "revenue_estimate": None,
            "revenue_actual": None,
            "quarter": 1,
            "year": 2026,
            "status": "scheduled",
            "source": "wisefn",
            "source_event_id": "wisefn::005930::2026-05-13::2026::1",
            "source_url": None,
            "title": "삼성전자 2026년 1분기 실적발표 예정",
        }
    ]
    assert "KR earnings calendar is backed by market_events" in result["warning"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_earnings_calendar_keeps_finnhub_path(monkeypatch):
    from app.mcp_server.tooling.fundamentals import _financials as financials

    fake = AsyncMock(
        return_value={
            "symbol": "AAPL",
            "instrument_type": "equity_us",
            "source": "finnhub",
            "from_date": "2026-05-01",
            "to_date": "2026-05-31",
            "count": 0,
            "earnings": [],
        }
    )
    monkeypatch.setattr(financials, "_fetch_earnings_calendar_finnhub", fake)

    result = await financials.handle_get_earnings_calendar(
        symbol="AAPL",
        from_date="2026-05-01",
        to_date="2026-05-31",
    )

    assert result["source"] == "finnhub"
    fake.assert_awaited_once_with("AAPL", "2026-05-01", "2026-05-31")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_explicit_us_rejects_korean_symbol():
    from app.mcp_server.tooling.fundamentals import _financials as financials

    with pytest.raises(ValueError, match="Use market='kr' for Korean equities"):
        await financials.handle_get_earnings_calendar(
            symbol="005930",
            from_date="2026-05-01",
            to_date="2026-05-31",
            market="us",
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_crypto_symbol_still_rejected():
    from app.mcp_server.tooling.fundamentals import _financials as financials

    with pytest.raises(ValueError, match="not available for cryptocurrencies"):
        await financials.handle_get_earnings_calendar(symbol="KRW-BTC")


def test_registers_earnings_calendar_market_parameter() -> None:
    from app.mcp_server.tooling.fundamentals_registration import (
        register_fundamentals_tools,
    )

    mcp = DummyMCP()
    register_fundamentals_tools(cast(Any, mcp))

    tool = mcp.tools["get_earnings_calendar"]
    signature = inspect.signature(tool)

    assert list(signature.parameters) == ["symbol", "from_date", "to_date", "market"]
    assert signature.parameters["symbol"].default is None
    assert signature.parameters["from_date"].default is None
    assert signature.parameters["to_date"].default is None
    assert signature.parameters["market"].default is None
    assert "Korean" in mcp.descriptions["get_earnings_calendar"]
```

- [ ] **Step 2: Run the failing MCP tests**

Run:

```bash
uv run pytest tests/test_mcp_earnings_calendar.py -v
```

Expected: FAIL because `_financials.AsyncSessionLocal` does not exist and `handle_get_earnings_calendar` has no `market` parameter.

- [ ] **Step 3: Implement the handler imports and helpers**

In `app/mcp_server/tooling/fundamentals/_financials.py`, add imports:

```python
from decimal import Decimal

from app.core.db import AsyncSessionLocal
```

Update the existing helper import:

```python
from app.mcp_server.tooling.fundamentals._helpers import (
    normalize_equity_market,
    normalize_market_with_crypto,
)
```

Add this service import:

```python
from app.services.market_events.query_service import MarketEventsQueryService
```

Add these helpers above `handle_get_earnings_calendar`:

```python
def _parse_iso_date(value: str | None, *, field_name: str) -> datetime.date | None:
    if value is None:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO format (e.g., '2024-01-15')") from exc


def _normalize_kr_calendar_symbol(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    normalized = symbol.strip().upper()
    if len(normalized) == 7 and normalized.startswith("A") and normalized[1:].isdigit():
        normalized = normalized[1:]
    if normalized.isdigit() and len(normalized) < 6:
        normalized = normalized.zfill(6)
    return normalized


def _resolve_earnings_calendar_market(
    symbol: str | None,
    market: str | None,
) -> str:
    if symbol and _is_crypto_market(symbol):
        raise ValueError("Earnings calendar is not available for cryptocurrencies")
    if market is not None:
        normalized = normalize_market_with_crypto(market)
        if normalized == "crypto":
            raise ValueError("Earnings calendar is not available for cryptocurrencies")
        return normalized
    if symbol and _is_korean_equity_code(symbol):
        return "kr"
    return "us"


def _number_or_none(value: Decimal | int | float | None) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        as_float = float(value)
        return int(as_float) if as_float.is_integer() else as_float
    return value


def _metric_value(event: Any, metric_name: str, field_name: str) -> int | float | None:
    for value in event.values:
        if value.metric_name == metric_name:
            return _number_or_none(getattr(value, field_name))
    return None
```

- [ ] **Step 4: Implement the KR calendar query function**

Add this function above `handle_get_earnings_calendar`:

```python
async def _fetch_earnings_calendar_market_events_kr(
    symbol: str | None,
    from_date: str | None,
    to_date: str | None,
) -> dict[str, Any]:
    normalized_symbol = _normalize_kr_calendar_symbol(symbol)
    if normalized_symbol and not _is_korean_equity_code(normalized_symbol):
        raise ValueError("KR earnings calendar requires a Korean equity code")

    start = _parse_iso_date(from_date, field_name="from_date") or datetime.date.today()
    end = _parse_iso_date(to_date, field_name="to_date") or (
        start + datetime.timedelta(days=30)
    )
    if start > end:
        raise ValueError("from_date must be <= to_date")

    async with AsyncSessionLocal() as db:
        svc = MarketEventsQueryService(db)
        response = await svc.list_for_range(
            start,
            end,
            category="earnings",
            market="kr",
            symbol=normalized_symbol,
        )

    earnings: list[dict[str, Any]] = []
    for event in response.events:
        earnings.append(
            {
                "symbol": event.symbol,
                "company_name": event.company_name,
                "date": event.event_date.isoformat(),
                "hour": event.time_hint or "unknown",
                "time_hint": event.time_hint or "unknown",
                "eps_estimate": _metric_value(event, "eps", "forecast"),
                "eps_actual": _metric_value(event, "eps", "actual"),
                "revenue_estimate": _metric_value(event, "revenue", "forecast"),
                "revenue_actual": _metric_value(event, "revenue", "actual"),
                "quarter": event.fiscal_quarter,
                "year": event.fiscal_year,
                "status": event.status,
                "source": event.source,
                "source_event_id": event.source_event_id,
                "source_url": event.source_url,
                "title": event.title,
            }
        )

    return {
        "symbol": normalized_symbol,
        "instrument_type": "equity_kr",
        "market": "kr",
        "source": "market_events",
        "sources": sorted({item["source"] for item in earnings}),
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "count": len(earnings),
        "earnings": earnings,
        "warning": (
            "KR earnings calendar is backed by market_events rows only "
            "(WiseFn scheduled earnings and DART filings classified as earnings). "
            "Shareholder meetings, ex-dividend dates, IR, and conferences are not "
            "covered by this tool yet."
        ),
    }
```

- [ ] **Step 5: Route `handle_get_earnings_calendar` by market**

Replace the existing `handle_get_earnings_calendar` with:

```python
async def handle_get_earnings_calendar(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip() if symbol else None
    normalized_market = _resolve_earnings_calendar_market(symbol, market)

    if normalized_market == "kr":
        return await _fetch_earnings_calendar_market_events_kr(
            symbol,
            from_date,
            to_date,
        )

    if symbol and _is_korean_equity_code(symbol):
        raise ValueError("Use market='kr' for Korean equities")

    _parse_iso_date(from_date, field_name="from_date")
    _parse_iso_date(to_date, field_name="to_date")

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

- [ ] **Step 6: Verify MCP handler tests now fail only on registration**

Run:

```bash
uv run pytest tests/test_mcp_earnings_calendar.py -v
```

Expected: KR/US handler tests pass, registration test still fails because `fundamentals_handlers.py` has not exposed `market` yet.

- [ ] **Step 7: Commit handler change after registration is done in Task 3**

Do not commit at this step if Task 3 has not been completed. The handler and registration changes should land together.

---

### Task 3: Update MCP Registration And Public Docs

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py:161-173`
- Modify: `app/mcp_server/README.md`
- Test: `tests/test_mcp_earnings_calendar.py`

- [ ] **Step 1: Update tool registration**

In `app/mcp_server/tooling/fundamentals_handlers.py`, replace the `get_earnings_calendar` registration block with:

```python
@mcp.tool(
    name="get_earnings_calendar",
    description=(
        "Get earnings calendar for a US or Korean stock/date range. "
        "US uses Finnhub and includes EPS/revenue estimates when available. "
        "Korean equities read existing market_events rows from WiseFn/DART; "
        "KR shareholder meetings, ex-dividend dates, IR, and conferences are "
        "not included yet."
    ),
)
async def get_earnings_calendar(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    return await handle_get_earnings_calendar(symbol, from_date, to_date, market)
```

- [ ] **Step 2: Add README contract**

In `app/mcp_server/README.md`, add this section immediately before `### get_disclosures spec`:

```markdown
### `get_earnings_calendar` spec

Parameters:
- `symbol`: Optional equity ticker/code. US examples: `AAPL`, `MSFT`; KR examples: `005930`, `A005930`.
- `from_date`: Optional ISO start date, inclusive. Defaults to server `today`.
- `to_date`: Optional ISO end date, inclusive. Defaults to `from_date + 30 days`.
- `market`: Optional explicit market (`us`, `kr`). If omitted, 6-digit or A-prefixed KR codes route to KR; other non-crypto symbols route to US.

Behavior:
- US requests keep the existing Finnhub path and response shape: `symbol`, `instrument_type`, `source`, `from_date`, `to_date`, `count`, `earnings`.
- KR requests read existing `market_events` rows where `category="earnings"` and `market="kr"`.
- KR rows are read-only and may come from `source="wisefn"` scheduled earnings or `source="dart"` filings classified as earnings.
- KR response top-level includes `source="market_events"`, `sources`, `market="kr"`, `warning`, and the existing `earnings` list.
- KR `earnings` items include `symbol`, `company_name`, `date`, `hour`, `time_hint`, `quarter`, `year`, `status`, `source`, `source_event_id`, `source_url`, and `title`.
- KR `eps_*` and `revenue_*` fields are present for shape compatibility but usually `null` until realized-value joins are implemented.

Limitations:
- KR shareholder meetings, ex-dividend dates, IR, and conferences are not collected by this tool yet.
- Empty KR results mean no matching `market_events` rows are currently stored for the requested window; they do not prove there is no real-world event.
- Production WiseFn ingestion enablement and scheduler activation are operational follow-ups, not part of this MCP read-path contract.

Errors:
- Crypto symbols return an explicit error because earnings calendars apply to equities only.
- `from_date > to_date` is rejected.
- Explicit `market="us"` with a Korean equity code is rejected with guidance to use `market="kr"`.
```

- [ ] **Step 3: Verify registration tests**

Run:

```bash
uv run pytest tests/test_mcp_earnings_calendar.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit handler, registration, docs, and MCP tests**

```bash
git add app/mcp_server/tooling/fundamentals/_financials.py \
        app/mcp_server/tooling/fundamentals_handlers.py \
        app/mcp_server/README.md \
        tests/test_mcp_earnings_calendar.py
git commit -m "feat(ROB-493): expose KR earnings calendar via MCP"
```

---

### Task 4: Run Focused Regression Suite

**Files:**
- No source edits expected.

- [ ] **Step 1: Run market-events query tests**

```bash
uv run pytest tests/services/test_market_events_query_service.py -v
```

Expected: PASS.

- [ ] **Step 2: Run MCP earnings calendar tests**

```bash
uv run pytest tests/test_mcp_earnings_calendar.py -v
```

Expected: PASS.

- [ ] **Step 3: Run adjacent MCP/disclosure regression tests**

```bash
uv run pytest tests/test_mcp_disclosures.py tests/test_market_events_cli.py -v
```

Expected: PASS. This checks DART-facing behavior and market-events CLI wiring that are adjacent to the KR calendar source model.

- [ ] **Step 4: Run Ruff on touched files**

```bash
uv run ruff check app/services/market_events/query_service.py \
                  app/mcp_server/tooling/fundamentals/_financials.py \
                  app/mcp_server/tooling/fundamentals_handlers.py \
                  tests/services/test_market_events_query_service.py \
                  tests/test_mcp_earnings_calendar.py
```

Expected: PASS.

- [ ] **Step 5: Capture verification in Linear**

Add a Linear comment to `ROB-493`:

```markdown
## Verification

- `uv run pytest tests/services/test_market_events_query_service.py -v`
- `uv run pytest tests/test_mcp_earnings_calendar.py -v`
- `uv run pytest tests/test_mcp_disclosures.py tests/test_market_events_cli.py -v`
- `uv run ruff check app/services/market_events/query_service.py app/mcp_server/tooling/fundamentals/_financials.py app/mcp_server/tooling/fundamentals_handlers.py tests/services/test_market_events_query_service.py tests/test_mcp_earnings_calendar.py`

Scope note: ROB-493 is read-only MCP exposure for existing KR earnings rows in `market_events`. No DB migration, scheduler activation, broker/order change, or live trading behavior changed.
```

---

### Task 5: Final Review And PR Notes

**Files:**
- No source edits expected unless review finds issues.

- [ ] **Step 1: Inspect diff**

```bash
git diff --stat
git diff -- app/services/market_events/query_service.py \
           app/mcp_server/tooling/fundamentals/_financials.py \
           app/mcp_server/tooling/fundamentals_handlers.py \
           app/mcp_server/README.md \
           tests/services/test_market_events_query_service.py \
           tests/test_mcp_earnings_calendar.py
```

Expected:
- Only the files listed above changed.
- No migration files.
- No scheduler/task files.
- No order/trading execution files.

- [ ] **Step 2: Prepare PR summary**

Use this PR body:

```markdown
## Summary

- Extends `get_earnings_calendar` with optional `market="kr"` support.
- KR earnings calendar reads existing `market_events` rows for `category="earnings"` / `market="kr"`.
- Keeps the existing US Finnhub path unchanged.
- Documents KR coverage limitations: shareholder meetings, ex-dividend dates, IR, and conferences are follow-ups.

## Safety

- Read-only MCP/query change.
- No DB migration.
- No ingestion scheduler activation.
- No broker/order/live trading behavior change.
- No new MCP tool surface.

## Verification

- [ ] `uv run pytest tests/services/test_market_events_query_service.py -v`
- [ ] `uv run pytest tests/test_mcp_earnings_calendar.py -v`
- [ ] `uv run pytest tests/test_mcp_disclosures.py tests/test_market_events_cli.py -v`
- [ ] `uv run ruff check app/services/market_events/query_service.py app/mcp_server/tooling/fundamentals/_financials.py app/mcp_server/tooling/fundamentals_handlers.py tests/services/test_market_events_query_service.py tests/test_mcp_earnings_calendar.py`
```

- [ ] **Step 3: Apply model-lane labels if scope expands**

If implementation stays within this plan, keep it routine: `keep_on_gpt54`.

If execution expands into any of these areas, label the issue/PR with `high_risk_change` and `needs_stronger_model_review`:

```text
DB migration, production scheduler activation, source-of-truth policy changes,
live order/trading behavior, strategy policy changes, auth/permission changes,
or deployment automation changes.
```

---

## Self-Review

- Spec coverage: ROB-493 asks for KR earnings/events visibility. This plan delivers KR earnings visibility through the existing MCP tool and explicitly separates broader events into follow-ups.
- Placeholder scan: No unresolved placeholders, no unspecified handlers, no unnamed tests, no hidden implementation steps.
- Type consistency: `market` is added consistently to the registered MCP tool and handler. `symbol` is added only to `MarketEventsQueryService` as an optional keyword, preserving existing callers.
- Scope check: The plan is a single read-only implementation slice. It avoids scheduler activation, scraping-source expansion, and DB schema work.
