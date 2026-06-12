# ROB-536 Market Calendar Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use Toss market-calendar as a supplemental trading-hours source for KR NXT and US day/pre/regular/after sessions, while keeping XKRX/XNYS as the regular-session authority and adding holiday gates to scheduled read-only reviews.

**Architecture:** Add a focused Toss calendar parser/cache service under `app/services/brokers/toss/`, then wire only the paths that need extended-hours knowledge. Exchange calendars remain the fail-closed authority for regular trading days and regular bounds; Toss is a short-window supplement for NXT, US dayMarket, and partial-session closures. No DB schema or persisted `market_session` vocabulary changes are introduced.

**Tech Stack:** Python 3.13, dataclasses, `datetime`/`zoneinfo`, existing `exchange_calendars`, existing Toss read client, pytest, pytest-asyncio, Ruff, ty.

---

## Starting State

- `app/services/brokers/toss/client.py` already has raw `market_calendar_kr()` and `market_calendar_us()` methods from ROB-530.
- `app/mcp_server/tooling/market_data_quotes.py` still has `_NXT_AFTER_OPEN = datetime.time(16, 0)` and therefore misses the 15:30-16:00 KST NXT after session.
- `app/jobs/research_run_refresh_runner.py` gates by weekday and time only; it does not consult XKRX holidays.
- `app/jobs/intraday_order_review.py` gates by weekday and hardcoded KST times only; it does not consult XKRX/XNYS holidays, half-days, or US DST.
- `app/services/market_events/session_calendar.py` already provides fail-closed XKRX/XNYS primitives and should be reused.

## Decisions

- **No migration:** Do not add a `day` value to `MarketSessionLiteral` or the `review.investment_reports.market_session` DB CHECK. US `dayMarket` is parsed and exposed by the Toss calendar service, but persisted report sessions remain `regular|nxt|pre|post|24x7`.
- **Toss is supplemental:** If `toss_api_enabled` is false, credentials are missing, or the Toss calendar call fails, read paths fall back to XKRX/XNYS plus corrected hardcoded windows.
- **Fail-closed on confirmed closure:** If exchange calendars say a regular session day is closed, scheduled gates close. If Toss calendar is available and a specific extended session is null, that extended-session gate closes.
- **No order mutation:** This issue changes read/session gates only. It must not add Toss order placement, modification, cancellation, ledger, or portfolio mutation paths.
- **Labels before implementation:** ROB-536 should be tagged `high_risk_change`, `needs_stronger_model_review`, and `hold_for_final_review` because market-hours gates affect live-review/order-adjacent workflows.

## File Structure

- Create: `app/services/brokers/toss/market_calendar.py`
  - Parse raw Toss KR/US market-calendar responses.
  - Cache parsed responses per `(market, query_date)` for the current KST day.
  - Expose async helpers for KR NXT sessions and US day/pre/regular/after sessions.
- Modify: `app/services/brokers/toss/__init__.py`
  - Export only the stable calendar DTOs/helpers needed by callers.
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
  - Change NXT after fallback open from 16:00 to 15:30.
  - Make NXT session resolution async so Toss calendar can close partial NXT holidays when available.
- Modify: `app/jobs/research_run_refresh_runner.py`
  - Add XKRX holiday gating for `_within_window`.
  - Add optional Toss session cross-check for `preopen` and `nxt_aftermarket`.
- Modify: `app/jobs/intraday_order_review.py`
  - Replace weekday-only KR/US helpers with `regular_session_bounds()` checks.
  - Keep crypto unchanged.
- Modify: `app/mcp_server/README.md`
  - Update NXT documented session from 16:00-20:00 to 15:30-20:00 and note calendar-backed partial-session handling.
- Test: `tests/services/brokers/toss/test_market_calendar.py`
- Test: `tests/test_mcp_quotes_tools.py`
- Test: `tests/test_research_run_refresh_runner.py`
- Test: `tests/test_intraday_order_review_jobs.py`

## Task 1: Toss Market Calendar Parser And Cache

**Files:**
- Create: `app/services/brokers/toss/market_calendar.py`
- Modify: `app/services/brokers/toss/__init__.py`
- Test: `tests/services/brokers/toss/test_market_calendar.py`

- [ ] **Step 1: Write parser and cache tests**

Create `tests/services/brokers/toss/test_market_calendar.py` with these tests:

```python
from __future__ import annotations

import datetime as dt

import pytest

from app.services.brokers.toss.market_calendar import (
    clear_toss_market_calendar_cache,
    get_toss_market_day,
    kr_nxt_session_for,
    parse_kr_market_calendar,
    parse_us_market_calendar,
    us_toss_session_for,
)

KST = dt.timezone(dt.timedelta(hours=9))


def _session(start: str, end: str, **extra: str | None) -> dict[str, str | None]:
    return {"startTime": start, "endTime": end, **extra}


def test_parse_kr_calendar_reads_nxt_after_1530() -> None:
    raw = {
        "today": {
            "date": "2026-03-25",
            "integrated": {
                "preMarket": _session(
                    "2026-03-25T08:00:00+09:00",
                    "2026-03-25T09:00:00+09:00",
                    singlePriceAuctionStartTime="2026-03-25T08:50:00+09:00",
                ),
                "regularMarket": _session(
                    "2026-03-25T09:00:00+09:00",
                    "2026-03-25T15:30:00+09:00",
                    singlePriceAuctionStartTime="2026-03-25T15:20:00+09:00",
                ),
                "afterMarket": _session(
                    "2026-03-25T15:30:00+09:00",
                    "2026-03-25T20:00:00+09:00",
                    singlePriceAuctionEndTime="2026-03-25T15:40:00+09:00",
                ),
            },
        },
        "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
        "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
    }

    calendar = parse_kr_market_calendar(raw)
    today = calendar.day_for(dt.date(2026, 3, 25))

    assert today is not None
    assert today.after_market is not None
    assert today.after_market.start == dt.datetime(2026, 3, 25, 15, 30, tzinfo=KST)
    assert today.after_market.end == dt.datetime(2026, 3, 25, 20, 0, tzinfo=KST)


def test_kr_nxt_session_for_partial_nxt_holiday_returns_none() -> None:
    raw = {
        "today": {
            "date": "2026-03-25",
            "integrated": {
                "preMarket": None,
                "regularMarket": _session(
                    "2026-03-25T09:00:00+09:00",
                    "2026-03-25T15:30:00+09:00",
                    singlePriceAuctionStartTime=None,
                ),
                "afterMarket": None,
            },
        },
        "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
        "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
    }
    calendar = parse_kr_market_calendar(raw)

    assert (
        kr_nxt_session_for(
            dt.datetime(2026, 3, 25, 15, 45, tzinfo=KST), calendar=calendar
        )
        is None
    )


def test_parse_us_calendar_reads_day_market_without_persisted_session_literal() -> None:
    raw = {
        "today": {
            "date": "2026-03-25",
            "dayMarket": _session(
                "2026-03-25T09:00:00+09:00",
                "2026-03-25T16:50:00+09:00",
            ),
            "preMarket": _session(
                "2026-03-25T17:00:00+09:00",
                "2026-03-25T22:30:00+09:00",
            ),
            "regularMarket": _session(
                "2026-03-25T22:30:00+09:00",
                "2026-03-26T05:00:00+09:00",
            ),
            "afterMarket": _session(
                "2026-03-26T05:00:00+09:00",
                "2026-03-26T07:00:00+09:00",
            ),
        },
        "previousBusinessDay": {"date": "2026-03-24"},
        "nextBusinessDay": {"date": "2026-03-26"},
    }

    calendar = parse_us_market_calendar(raw)

    assert (
        us_toss_session_for(
            dt.datetime(2026, 3, 25, 10, 0, tzinfo=KST), calendar=calendar
        )
        == "day"
    )
    assert (
        us_toss_session_for(
            dt.datetime(2026, 3, 25, 18, 0, tzinfo=KST), calendar=calendar
        )
        == "pre"
    )


@pytest.mark.asyncio
async def test_get_toss_market_day_uses_one_kst_day_cache(monkeypatch) -> None:
    clear_toss_market_calendar_cache()
    calls: list[str | None] = []

    class Client:
        async def market_calendar_kr(self, *, date: str | None = None):
            calls.append(date)
            return {
                "today": {"date": "2026-03-25", "integrated": None},
                "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
                "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
            }

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        "app.services.brokers.toss.market_calendar.TossReadClient.from_settings",
        lambda: Client(),
    )

    first = await get_toss_market_day("kr", dt.date(2026, 3, 25))
    second = await get_toss_market_day("kr", dt.date(2026, 3, 25))

    assert first is second
    assert calls == ["2026-03-25"]
```

- [ ] **Step 2: Run parser/cache tests and verify they fail**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_market_calendar.py -q
```

Expected: FAIL because `app.services.brokers.toss.market_calendar` does not exist.

- [ ] **Step 3: Implement `market_calendar.py`**

Create `app/services/brokers/toss/market_calendar.py` with this structure:

```python
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Literal

from app.services.brokers.toss.client import TossReadClient

logger = logging.getLogger(__name__)

Market = Literal["kr", "us"]
KrNxtSession = Literal["nxt_premarket", "nxt_after", "closed"]
KrTossSession = Literal["nxt_premarket", "regular", "nxt_after", "closed"]
UsTossSession = Literal["day", "pre", "regular", "post"]

_KST = dt.timezone(dt.timedelta(hours=9))
_CACHE: dict[tuple[Market, dt.date], tuple[dt.date, TossMarketCalendar]] = {}


@dataclass(frozen=True)
class TossSessionWindow:
    start: dt.datetime
    end: dt.datetime
    single_price_auction_start: dt.datetime | None = None
    single_price_auction_end: dt.datetime | None = None

    def contains(self, moment: dt.datetime) -> bool:
        local = _to_kst(moment)
        return self.start <= local < self.end


@dataclass(frozen=True)
class TossKrMarketDay:
    date: dt.date
    pre_market: TossSessionWindow | None
    regular_market: TossSessionWindow | None
    after_market: TossSessionWindow | None


@dataclass(frozen=True)
class TossUsMarketDay:
    date: dt.date
    day_market: TossSessionWindow | None
    pre_market: TossSessionWindow | None
    regular_market: TossSessionWindow | None
    after_market: TossSessionWindow | None


@dataclass(frozen=True)
class TossMarketCalendar:
    market: Market
    days: tuple[TossKrMarketDay | TossUsMarketDay, ...]

    def day_for(self, day: dt.date) -> TossKrMarketDay | TossUsMarketDay | None:
        for item in self.days:
            if item.date == day:
                return item
        return None


def clear_toss_market_calendar_cache() -> None:
    _CACHE.clear()


def _to_kst(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_KST)
    return moment.astimezone(_KST)


def _parse_datetime(value: object) -> dt.datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Toss calendar datetime must be a string or null")
    return dt.datetime.fromisoformat(value).astimezone(_KST)


def _parse_window(raw: dict[str, Any] | None) -> TossSessionWindow | None:
    if raw is None:
        return None
    return TossSessionWindow(
        start=_parse_datetime(raw["startTime"]) or _missing_datetime("startTime"),
        end=_parse_datetime(raw["endTime"]) or _missing_datetime("endTime"),
        single_price_auction_start=_parse_datetime(
            raw.get("singlePriceAuctionStartTime")
        ),
        single_price_auction_end=_parse_datetime(raw.get("singlePriceAuctionEndTime")),
    )


def _missing_datetime(field_name: str) -> dt.datetime:
    raise ValueError(f"Toss calendar missing required {field_name}")


def _calendar_items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(raw["today"]),
        dict(raw["previousBusinessDay"]),
        dict(raw["nextBusinessDay"]),
    ]


def parse_kr_market_calendar(raw: dict[str, Any]) -> TossMarketCalendar:
    days: list[TossKrMarketDay] = []
    for item in _calendar_items(raw):
        integrated = item.get("integrated") or {}
        days.append(
            TossKrMarketDay(
                date=dt.date.fromisoformat(str(item["date"])),
                pre_market=_parse_window(integrated.get("preMarket")),
                regular_market=_parse_window(integrated.get("regularMarket")),
                after_market=_parse_window(integrated.get("afterMarket")),
            )
        )
    return TossMarketCalendar(market="kr", days=tuple(days))


def parse_us_market_calendar(raw: dict[str, Any]) -> TossMarketCalendar:
    days: list[TossUsMarketDay] = []
    for item in _calendar_items(raw):
        days.append(
            TossUsMarketDay(
                date=dt.date.fromisoformat(str(item["date"])),
                day_market=_parse_window(item.get("dayMarket")),
                pre_market=_parse_window(item.get("preMarket")),
                regular_market=_parse_window(item.get("regularMarket")),
                after_market=_parse_window(item.get("afterMarket")),
            )
        )
    return TossMarketCalendar(market="us", days=tuple(days))


def kr_nxt_session_for(
    moment: dt.datetime, *, calendar: TossMarketCalendar
) -> KrNxtSession | None:
    session = kr_toss_session_for(moment, calendar=calendar)
    if session in {"nxt_premarket", "nxt_after"}:
        return session
    return None


def kr_toss_session_for(
    moment: dt.datetime, *, calendar: TossMarketCalendar
) -> KrTossSession | None:
    local = _to_kst(moment)
    day = calendar.day_for(local.date())
    if not isinstance(day, TossKrMarketDay):
        return None
    if day.pre_market is not None and day.pre_market.contains(local):
        return "nxt_premarket"
    if day.regular_market is not None and day.regular_market.contains(local):
        return "regular"
    if day.after_market is not None and day.after_market.contains(local):
        return "nxt_after"
    return None


def us_toss_session_for(
    moment: dt.datetime, *, calendar: TossMarketCalendar
) -> UsTossSession | None:
    local = _to_kst(moment)
    for day in calendar.days:
        if not isinstance(day, TossUsMarketDay):
            continue
        if day.day_market is not None and day.day_market.contains(local):
            return "day"
        if day.pre_market is not None and day.pre_market.contains(local):
            return "pre"
        if day.regular_market is not None and day.regular_market.contains(local):
            return "regular"
        if day.after_market is not None and day.after_market.contains(local):
            return "post"
    return None


async def get_toss_market_calendar(
    market: Market, query_date: dt.date
) -> TossMarketCalendar | None:
    fetched_on = dt.datetime.now(_KST).date()
    key = (market, query_date)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == fetched_on:
        return cached[1]

    client = TossReadClient.from_settings()
    try:
        if market == "kr":
            raw = await client.market_calendar_kr(date=query_date.isoformat())
            parsed = parse_kr_market_calendar(raw)
        else:
            raw = await client.market_calendar_us(date=query_date.isoformat())
            parsed = parse_us_market_calendar(raw)
    except Exception:
        logger.info("Toss market calendar unavailable for %s %s", market, query_date)
        return None
    finally:
        await client.aclose()

    _CACHE[key] = (fetched_on, parsed)
    return parsed


async def get_toss_market_day(
    market: Market, day: dt.date
) -> TossKrMarketDay | TossUsMarketDay | None:
    calendar = await get_toss_market_calendar(market, day)
    if calendar is None:
        return None
    return calendar.day_for(day)


async def get_kr_nxt_session_from_toss(moment: dt.datetime) -> KrNxtSession | None:
    local = _to_kst(moment)
    calendar = await get_toss_market_calendar("kr", local.date())
    if calendar is None:
        return None
    return kr_nxt_session_for(local, calendar=calendar) or "closed"


async def get_kr_toss_session_from_toss(moment: dt.datetime) -> KrTossSession | None:
    local = _to_kst(moment)
    calendar = await get_toss_market_calendar("kr", local.date())
    if calendar is None:
        return None
    return kr_toss_session_for(local, calendar=calendar) or "closed"


async def get_us_toss_session_from_toss(moment: dt.datetime) -> UsTossSession | None:
    local = _to_kst(moment)
    calendar = await get_toss_market_calendar("us", local.date())
    if calendar is None:
        return None
    return us_toss_session_for(local, calendar=calendar)
```

- [ ] **Step 4: Export stable helpers**

Add these exports to `app/services/brokers/toss/__init__.py`:

```python
from app.services.brokers.toss.market_calendar import (
    TossKrMarketDay,
    TossMarketCalendar,
    TossSessionWindow,
    TossUsMarketDay,
    get_kr_nxt_session_from_toss,
    get_us_toss_session_from_toss,
)
```

and add the same names to `__all__`.

- [ ] **Step 5: Run parser/cache tests**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_market_calendar.py -q
```

Expected: PASS.

## Task 2: NXT Quote Session Uses Toss Calendar With Correct Fallback

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Test: `tests/test_mcp_quotes_tools.py`

- [ ] **Step 1: Add failing NXT 15:30 regression test**

Append this test near `test_get_quote_korean_equity_after_hours_routes_to_nxt` in `tests/test_mcp_quotes_tools.py`:

```python
@pytest.mark.asyncio
async def test_get_quote_korean_equity_after_hours_routes_to_nxt_at_1535(monkeypatch):
    """ROB-536: NXT after starts at 15:30 KST, not 16:00."""
    from app.mcp_server.tooling import market_data_quotes

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    get_orderbook_mock = AsyncMock(return_value=_nxt_quote_book(expected_price=113900))

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
        lambda: pd.Timestamp("2026-06-11 15:35:00", tz="Asia/Seoul").to_pydatetime(),
    )
    monkeypatch.setattr(
        market_data_quotes,
        "get_kr_nxt_session_from_toss",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )

    result = await tools["get_quote"]("005930")

    get_orderbook_mock.assert_awaited_once_with("005930", "kr", venue="nxt")
    assert result["session"] == "nxt_after"
    assert result["data_state"] == "fresh"
```

- [ ] **Step 2: Add failing partial NXT holiday test**

Add:

```python
@pytest.mark.asyncio
async def test_get_quote_korean_equity_respects_toss_partial_nxt_holiday(monkeypatch):
    """ROB-536: Toss calendar can close NXT after while XKRX regular day exists."""
    from app.mcp_server.tooling import market_data_quotes

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
        lambda *a, **k: "market_closed",
    )
    monkeypatch.setattr(market_data_quotes, "is_kr_session_day", lambda date: True)
    monkeypatch.setattr(
        market_data_quotes,
        "now_kst",
        lambda: pd.Timestamp("2026-06-11 15:45:00", tz="Asia/Seoul").to_pydatetime(),
    )
    monkeypatch.setattr(
        market_data_quotes,
        "get_kr_nxt_session_from_toss",
        AsyncMock(return_value="closed"),
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )

    result = await tools["get_quote"]("005930")

    get_orderbook_mock.assert_not_awaited()
    assert result["data_state"] == "market_closed"
    assert "session" not in result
```

- [ ] **Step 3: Run the NXT tests and verify failure**

Run:

```bash
uv run pytest tests/test_mcp_quotes_tools.py -k "after_hours_routes_to_nxt_at_1535 or partial_nxt_holiday" -q
```

Expected: FAIL because `_NXT_AFTER_OPEN` is still 16:00 and `_nxt_quote_session` is sync/Toss-unaware.

- [ ] **Step 4: Implement async NXT session resolution**

In `app/mcp_server/tooling/market_data_quotes.py`:

1. Import the Toss helper:

```python
from app.services.brokers.toss.market_calendar import (
    get_kr_nxt_session_from_toss,
    get_kr_toss_session_from_toss,
)
```

2. Change the fallback constant:

```python
_NXT_AFTER_OPEN = datetime.time(15, 30)
_NXT_AFTER_CLOSE = datetime.time(20, 0)
```

3. Replace the existing `_nxt_quote_session` function with this async helper:

```python
async def _nxt_quote_session(
    data_state: str,
    *,
    now: datetime.datetime | None = None,
) -> str | None:
    current = _current_kst_datetime(now)
    toss_session = await get_kr_nxt_session_from_toss(current)
    if toss_session in {"nxt_premarket", "nxt_after"}:
        return toss_session
    if toss_session == "closed":
        return None

    if data_state == DATA_STATE_PREMARKET_UNAVAILABLE:
        return "nxt_premarket"

    if not is_kr_session_day(current.date()):
        return None

    current_time = current.timetz().replace(tzinfo=None)
    if _NXT_AFTER_OPEN <= current_time < _NXT_AFTER_CLOSE:
        return "nxt_after"
    return None
```

The Task 1 helper must use this exact semantic split: `None` means Toss calendar was unavailable and fallback is allowed; `"closed"` means Toss calendar was available and the NXT session is closed, so fallback must be suppressed.

4. Update the call site:

```python
session = await _nxt_quote_session(data_state)
```

- [ ] **Step 5: Run NXT tests**

Run:

```bash
uv run pytest tests/test_mcp_quotes_tools.py -k "nxt or after_hours_routes_to_nxt_at_1535 or partial_nxt_holiday" -q
```

Expected: PASS.

## Task 3: Research Run Refresh Holiday Gate

**Files:**
- Modify: `app/jobs/research_run_refresh_runner.py`
- Test: `tests/test_research_run_refresh_runner.py`

- [ ] **Step 1: Write failing holiday tests**

Add to `tests/test_research_run_refresh_runner.py`:

```python
def test_preopen_window_excludes_xkrx_holiday(monkeypatch):
    import app.jobs.research_run_refresh_runner as runner

    monkeypatch.setattr(runner, "is_trading_session", lambda market, day: False)

    assert (
        _within_window(stage="preopen", now=datetime(2026, 5, 5, 8, 10))
        is False
    )


@pytest.mark.asyncio
async def test_holiday_gate_skips_before_db_work(monkeypatch):
    from app.core.config import settings
    import app.jobs.research_run_refresh_runner as runner

    monkeypatch.setattr(settings, "research_run_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", 1, raising=False)
    monkeypatch.setattr(
        settings, "research_run_refresh_market_hours_only", True, raising=False
    )
    monkeypatch.setattr(runner, "is_trading_session", lambda market, day: False)

    result = await run_research_run_refresh(
        stage="preopen",
        market_scope="kr",
        db_factory=_fake_factory,
        now_local=lambda: datetime(2026, 5, 5, 8, 10),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "outside_trading_hours"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_research_run_refresh_runner.py -k "holiday_gate or xkrx_holiday" -q
```

Expected: FAIL because `_within_window` currently checks weekday only.

- [ ] **Step 3: Add XKRX session gate**

In `app/jobs/research_run_refresh_runner.py`, import:

```python
from app.services.market_events.session_calendar import is_trading_session
```

Change `_within_window`:

```python
def _within_window(*, stage: StageLiteral, now: datetime) -> bool:
    """Return True if `now` falls within the allowed trading window for `stage`."""
    if not is_trading_session("kr", now.date()):
        return False
    minutes = now.hour * 60 + now.minute
    if stage == "preopen":
        start = _KR_PREOPEN_WINDOW[0][0] * 60 + _KR_PREOPEN_WINDOW[0][1]
        end = _KR_PREOPEN_WINDOW[1][0] * 60 + _KR_PREOPEN_WINDOW[1][1]
    elif stage == "nxt_aftermarket":
        start = _KR_NXT_WINDOW[0][0] * 60 + _KR_NXT_WINDOW[0][1]
        end = _KR_NXT_WINDOW[1][0] * 60 + _KR_NXT_WINDOW[1][1]
    else:
        return False
    return start <= minutes <= end
```

- [ ] **Step 4: Run research refresh tests**

Run:

```bash
uv run pytest tests/test_research_run_refresh_runner.py -q
```

Expected: PASS.

## Task 4: Intraday Order Review Uses Exchange Calendar Bounds

**Files:**
- Modify: `app/jobs/intraday_order_review.py`
- Test: `tests/test_intraday_order_review_jobs.py`

- [ ] **Step 1: Replace inaccurate US tests and add holiday cases**

In `tests/test_intraday_order_review_jobs.py`, change the old US early-morning assertions to real KST/ET cases and add holiday skips:

```python
def test_us_trading_hours_open_regular_session_dst(self) -> None:
    # 2026-03-16 22:45 KST == 2026-03-16 09:45 ET.
    dt_value = datetime(2026, 3, 16, 22, 45)
    assert is_us_trading_hours(dt_value) is True


def test_us_trading_hours_open_regular_session_standard_time(self) -> None:
    # 2026-01-05 23:45 KST == 2026-01-05 09:45 ET.
    dt_value = datetime(2026, 1, 5, 23, 45)
    assert is_us_trading_hours(dt_value) is True


def test_us_trading_hours_closed_kst_daytime(self) -> None:
    dt_value = datetime(2026, 3, 16, 12, 0)
    assert is_us_trading_hours(dt_value) is False


def test_us_trading_hours_closed_on_us_holiday(self) -> None:
    # 2026-07-03 is the observed Independence Day market holiday.
    dt_value = datetime(2026, 7, 3, 23, 45)
    assert is_us_trading_hours(dt_value) is False


def test_kr_trading_hours_closed_on_xkrx_holiday() -> None:
    # Children's Day in Korea.
    dt_value = datetime(2026, 5, 5, 10, 0)
    assert is_kr_trading_hours(dt_value) is False
```

Update `TestRunUsOrderReview.test_runs_during_trading_hours`:

```python
fake_dt = datetime(2026, 3, 16, 22, 45)
```

- [ ] **Step 2: Run intraday tests and verify failure**

Run:

```bash
uv run pytest tests/test_intraday_order_review_jobs.py -q
```

Expected: FAIL on holiday/DST-aware assertions.

- [ ] **Step 3: Implement regular-session bounds checks**

In `app/jobs/intraday_order_review.py`, import:

```python
from datetime import UTC
from zoneinfo import ZoneInfo

from app.services.market_events.session_calendar import regular_session_bounds
```

Add helpers:

```python
_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")


def _as_kst(dt_value: datetime) -> datetime:
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=_KST)
    return dt_value.astimezone(_KST)


def _within_utc_bounds(dt_value: datetime, bounds: tuple[datetime, datetime]) -> bool:
    as_utc = _as_kst(dt_value).astimezone(UTC)
    start, end = bounds
    return start <= as_utc < end
```

Replace `is_kr_trading_hours`:

```python
def is_kr_trading_hours(dt_value: datetime) -> bool:
    """Return True if dt is within the XKRX regular session."""
    local = _as_kst(dt_value)
    bounds = regular_session_bounds("kr", local.date())
    return bounds is not None and _within_utc_bounds(local, bounds)
```

Replace `is_us_trading_hours`:

```python
def is_us_trading_hours(dt_value: datetime) -> bool:
    """Return True if dt is within the XNYS regular session, DST/holiday aware."""
    local = _as_kst(dt_value)
    et_day = local.astimezone(_ET).date()
    bounds = regular_session_bounds("us", et_day)
    return bounds is not None and _within_utc_bounds(local, bounds)
```

- [ ] **Step 4: Run intraday tests**

Run:

```bash
uv run pytest tests/test_intraday_order_review_jobs.py -q
```

Expected: PASS.

## Task 5: Optional Toss Cross-Check For KR Extended Session Gates

**Files:**
- Modify: `app/jobs/research_run_refresh_runner.py`
- Test: `tests/test_research_run_refresh_runner.py`

- [ ] **Step 1: Add tests for Toss null NXT session**

Add:

```python
@pytest.mark.asyncio
async def test_nxt_aftermarket_skips_when_toss_calendar_closes_session(monkeypatch):
    from app.core.config import settings
    import app.jobs.research_run_refresh_runner as runner

    monkeypatch.setattr(settings, "research_run_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "research_run_refresh_user_id", 1, raising=False)
    monkeypatch.setattr(
        settings, "research_run_refresh_market_hours_only", True, raising=False
    )
    monkeypatch.setattr(runner, "is_trading_session", lambda market, day: True)
    monkeypatch.setattr(
        runner,
        "get_kr_nxt_session_from_toss",
        AsyncMock(return_value="closed"),
    )

    result = await run_research_run_refresh(
        stage="nxt_aftermarket",
        market_scope="kr",
        db_factory=_fake_factory,
        now_local=lambda: datetime(2026, 6, 11, 15, 45),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "outside_trading_hours"
```

- [ ] **Step 2: Run the new test and verify failure**

Run:

```bash
uv run pytest tests/test_research_run_refresh_runner.py -k "toss_calendar_closes_session" -q
```

Expected: FAIL because the job does not consult Toss.

- [ ] **Step 3: Implement async stage cross-check**

In `app/jobs/research_run_refresh_runner.py`, import:

```python
from app.services.brokers.toss.market_calendar import get_kr_nxt_session_from_toss
```

Add:

```python
async def _toss_stage_allows_run(*, stage: StageLiteral, now: datetime) -> bool:
    if stage == "nxt_aftermarket":
        session = await get_kr_nxt_session_from_toss(now)
        return session in {None, "nxt_after"}
    if stage == "preopen":
        session = await get_kr_toss_session_from_toss(now)
        return session in {None, "nxt_premarket", "regular"}
    return False
```

Then update the market-hours gate:

```python
    local_now = now_local()
    if settings.research_run_refresh_market_hours_only and (
        not _within_window(stage=stage, now=local_now)
        or not await _toss_stage_allows_run(stage=stage, now=local_now)
    ):
        logger.info(
            "research_run_refresh outside trading hours; skipping (%s/%s)",
            stage,
            market_scope,
        )
        return {**base, "status": "skipped", "reason": "outside_trading_hours"}
```

Keep the exact Task 1 behavior: `None` means Toss unavailable/no opinion and allows fallback; `"closed"` means Toss calendar is available and the stage must close.

- [ ] **Step 4: Run research tests**

Run:

```bash
uv run pytest tests/test_research_run_refresh_runner.py -q
```

Expected: PASS.

## Task 6: Documentation And Verification

**Files:**
- Modify: `app/mcp_server/README.md`
- No migration files.

- [ ] **Step 1: Update MCP README NXT times**

In `app/mcp_server/README.md`, update the orderbook note currently saying:

```markdown
- During the NXT session (`16:00`-`20:00` KST), KIS may return `expected_price`
```

to:

```markdown
- During the NXT after session (`15:30`-`20:00` KST; Toss market-calendar when available, corrected hardcoded fallback otherwise), KIS may return `expected_price`
```

Also add one sentence near the `get_quote` NXT bullets:

```markdown
  - KR NXT overlay honors Toss market-calendar partial-session closures when the Toss API is enabled; otherwise it falls back to XKRX session days and the corrected NXT windows.
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
uv run pytest \
  tests/services/brokers/toss/test_market_calendar.py \
  tests/test_mcp_quotes_tools.py -k "nxt or after_hours_routes_to_nxt_at_1535 or partial_nxt_holiday" \
  tests/test_research_run_refresh_runner.py \
  tests/test_intraday_order_review_jobs.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run lint/type checks for touched files**

Run:

```bash
uv run ruff check \
  app/services/brokers/toss/market_calendar.py \
  app/services/brokers/toss/__init__.py \
  app/mcp_server/tooling/market_data_quotes.py \
  app/jobs/research_run_refresh_runner.py \
  app/jobs/intraday_order_review.py \
  tests/services/brokers/toss/test_market_calendar.py \
  tests/test_mcp_quotes_tools.py \
  tests/test_research_run_refresh_runner.py \
  tests/test_intraday_order_review_jobs.py
```

Expected: PASS.

Run:

```bash
uv run ty check \
  app/services/brokers/toss/market_calendar.py \
  app/mcp_server/tooling/market_data_quotes.py \
  app/jobs/research_run_refresh_runner.py \
  app/jobs/intraday_order_review.py
```

Expected: PASS.

- [ ] **Step 4: Confirm migration 0**

Run:

```bash
git status --short alembic/versions
```

Expected: no output.

- [ ] **Step 5: Linear hold comment**

After implementation and local verification, add a ROB-536 comment:

```markdown
Implementation is ready for ROB-536, but I am applying `high_risk_change` + `needs_stronger_model_review` + `hold_for_final_review` because this changes market-hours gates used by read-side review and NXT session behavior. No deploy or live operational use until stronger-model/CTO review clears the fallback and partial-holiday assumptions.

Local verification:
- `uv run pytest tests/services/brokers/toss/test_market_calendar.py tests/test_mcp_quotes_tools.py -k "nxt or after_hours_routes_to_nxt_at_1535 or partial_nxt_holiday" tests/test_research_run_refresh_runner.py tests/test_intraday_order_review_jobs.py -q`
- `uv run ruff check app/services/brokers/toss/market_calendar.py app/services/brokers/toss/__init__.py app/mcp_server/tooling/market_data_quotes.py app/jobs/research_run_refresh_runner.py app/jobs/intraday_order_review.py tests/services/brokers/toss/test_market_calendar.py tests/test_mcp_quotes_tools.py tests/test_research_run_refresh_runner.py tests/test_intraday_order_review_jobs.py`
- `uv run ty check app/services/brokers/toss/market_calendar.py app/mcp_server/tooling/market_data_quotes.py app/jobs/research_run_refresh_runner.py app/jobs/intraday_order_review.py`
- migration 0 confirmed
```

## Self-Review

Spec coverage:

- NXT 15:30-16:00 recognized: Task 2.
- Toss calendar-backed KR NXT and partial closures: Tasks 1, 2, 5.
- US dayMarket parsing: Task 1.
- US/KR holiday gates for research/intraday: Tasks 3, 4.
- One-day cache: Task 1.
- XKRX/XNYS retained as regular authority: Tasks 3, 4 and Decisions.
- Migration 0: Tasks 4 and 6; no schema literal change.

Completeness scan:

- The Toss session sentinel is fixed: `None` means unavailable/no opinion, and `"closed"` means available but closed.

Type consistency:

- `KrNxtSession` values are `nxt_premarket`, `nxt_after`, and `closed`; only the first two are copied into MCP response `session`.
- `UsTossSession` includes `day`, but this is not a `MarketSessionLiteral` and must not be persisted as `market_session`.
- `regular_session_bounds()` returns UTC datetimes and intraday review converts KST inputs to UTC before comparing.
