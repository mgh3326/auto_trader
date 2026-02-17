# Yahoo US Market Closed-Candle Cache Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** yfinance OHLCV(day/week/month) 요청을 NYSE 실제 마감 기준의 Redis 확정봉 캐시로 감싸 반복 HTTP 호출을 줄이고, 기존 `fetch_ohlcv()` 계약은 유지한다.

**Architecture:** `app/services/yahoo.py`의 서비스 경계에서 캐시를 투명 적용한다. 캐시 구현은 신규 `app/services/yahoo_ohlcv_cache.py`에 분리하고, 확정 버킷 계산은 `exchange_calendars`의 `XNYS` 세션 close 시각으로 처리한다. 캐시 실패/락 경합 시 `None`을 반환해 서비스 계층이 raw fallback을 수행한다.

**Tech Stack:** Python 3.13, yfinance 1.1.x, exchange_calendars, redis.asyncio, pandas, pytest, pytest-asyncio, AsyncMock

---

### Task 1: Add NYSE calendar dependency and Yahoo cache settings

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `app/core/config.py`
- Modify: `tests/test_config.py`

**Step 1: Write the failing test**

`tests/test_config.py`에 Yahoo 캐시 설정 필드 존재 검증을 추가한다.

```python
def test_yahoo_cache_settings_attributes_exist(self):
    assert hasattr(settings, "yahoo_ohlcv_cache_enabled")
    assert hasattr(settings, "yahoo_ohlcv_cache_max_days")
    assert hasattr(settings, "yahoo_ohlcv_cache_lock_ttl_seconds")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::TestSettings::test_yahoo_cache_settings_attributes_exist -v`  
Expected: FAIL (`hasattr(...)=False`)

**Step 3: Write minimal implementation**

- `pyproject.toml`에 dependency 추가:

```toml
"exchange-calendars>=4.7,<5.0",
```

- `app/core/config.py`에 Yahoo 캐시 설정 추가:

```python
yahoo_ohlcv_cache_enabled: bool = True
yahoo_ohlcv_cache_max_days: int = 400
yahoo_ohlcv_cache_lock_ttl_seconds: int = 10
```

- lock 파일 갱신:
  - `uv lock`

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py::TestSettings::test_yahoo_cache_settings_attributes_exist -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add pyproject.toml uv.lock app/core/config.py tests/test_config.py
git commit -m "chore: add NYSE calendar dependency and yahoo cache settings"
```

### Task 2: Build NYSE closed-bucket time resolver helpers

**Files:**
- Create: `app/services/yahoo_ohlcv_cache.py`
- Create: `tests/test_yahoo_ohlcv_cache.py`

**Step 1: Write the failing test**

`tests/test_yahoo_ohlcv_cache.py`에 period별 확정 버킷 계산 테스트를 작성한다.

```python
def test_get_last_closed_bucket_nyse_day_uses_last_session_close(monkeypatch):
    monkeypatch.setattr(yahoo_ohlcv_cache, "_get_xnys_calendar", lambda: fake_calendar)
    now = datetime(2026, 2, 17, 21, 30, tzinfo=UTC)
    assert yahoo_ohlcv_cache.get_last_closed_bucket_nyse("day", now) == date(2026, 2, 17)
```

추가 케이스:
- week: 같은 주에서 close가 아직 안 지난 경우 이전 주 선택
- month: 월말 마지막 거래 세션 close가 지나야 확정
- invalid period: `ValueError`

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_yahoo_ohlcv_cache.py -k "closed_bucket" -v`  
Expected: FAIL (`ModuleNotFoundError` or missing function)

**Step 3: Write minimal implementation**

`app/services/yahoo_ohlcv_cache.py`에 아래 최소 구현을 추가한다.

```python
def get_last_closed_bucket_nyse(period: str, now: datetime | None = None) -> date:
    normalized = _normalize_period(period)
    now_utc = _normalize_now_utc(now)
    sessions = _recent_sessions(now_utc, lookback_days=120)
    closed_sessions = [
        (session_date, close_ts)
        for session_date, close_ts in sessions
        if close_ts <= now_utc
    ]
    if not closed_sessions:
        raise ValueError("No closed NYSE session available")
    return _resolve_bucket_date(normalized, closed_sessions)
```

필수 보조 함수:
- `_normalize_period`
- `_normalize_now_utc`
- `_get_xnys_calendar`
- `_resolve_bucket_date` (day/week/month)

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_yahoo_ohlcv_cache.py -k "closed_bucket" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/yahoo_ohlcv_cache.py tests/test_yahoo_ohlcv_cache.py
git commit -m "feat: add NYSE closed-bucket resolver for yahoo ohlcv cache"
```

### Task 3: Implement Redis cache storage/backfill flow for Yahoo OHLCV

**Files:**
- Modify: `app/services/yahoo_ohlcv_cache.py`
- Modify: `tests/test_yahoo_ohlcv_cache.py`

**Step 1: Write the failing test**

`FakeRedis` 기반으로 캐시 read/backfill/retention 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_get_closed_candles_cache_hit_returns_without_raw_fetch(monkeypatch):
    fake_redis = _FakeRedis()
    await yahoo_ohlcv_cache._upsert_rows(fake_redis, dates_key, rows_key, sample_frame)
    fetch_mock = AsyncMock()
    monkeypatch.setattr(yahoo_ohlcv_cache, "_get_redis_client", AsyncMock(return_value=fake_redis))
    monkeypatch.setattr(yahoo_ohlcv_cache, "get_last_closed_bucket_nyse", lambda period, now=None: date(2026, 2, 14))

    result = await yahoo_ohlcv_cache.get_closed_candles("AAPL", count=3, period="day", raw_fetcher=fetch_mock)

    assert len(result) == 3
    fetch_mock.assert_not_awaited()
```

추가 케이스:
- partial hit에서 부족분만 backfill
- max_days 초과 시 trim
- oldest_confirmed=true일 때 count 부족이어도 latest가 맞으면 반환

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_yahoo_ohlcv_cache.py -k "cache_hit or partial_hit or trim or oldest_confirmed" -v`  
Expected: FAIL (캐시 저장/백필 로직 미구현)

**Step 3: Write minimal implementation**

`upbit_ohlcv_cache` 패턴을 Yahoo 도메인으로 맞춰 구현한다.

핵심 구현:

```python
async def get_closed_candles(
    ticker: str,
    count: int,
    period: str,
    raw_fetcher: Callable[[str, int, str, datetime | None], Awaitable[pd.DataFrame]],
) -> pd.DataFrame | None:
    ...
    cached = await _read_cached_rows(...)
    if _is_cache_sufficient(...):
        return cached.tail(requested_count).reset_index(drop=True)
    ...
    await _backfill_until_satisfied(...)
    return final_rows.tail(requested_count).reset_index(drop=True)
```

필수 구성:
- `_base_key/_keys`
- `_read_cached_rows`, `_upsert_rows`
- `_acquire_lock`, `_release_lock`
- `_enforce_retention_limit`
- `_read_cache_status`, `_is_cache_sufficient`
- `_backfill_until_satisfied`

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_yahoo_ohlcv_cache.py -k "cache_hit or partial_hit or trim or oldest_confirmed" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/yahoo_ohlcv_cache.py tests/test_yahoo_ohlcv_cache.py
git commit -m "feat: implement redis closed-candle cache flow for yahoo ohlcv"
```

### Task 4: Wire cache into Yahoo service boundary with raw fallback

**Files:**
- Modify: `app/services/yahoo.py`
- Create: `tests/test_yahoo_service_cache.py`

**Step 1: Write the failing test**

`tests/test_yahoo_service_cache.py`에 서비스 경계 동작을 검증한다.

```python
@pytest.mark.asyncio
async def test_fetch_ohlcv_uses_cache_for_day(monkeypatch):
    cached = sample_frame()
    monkeypatch.setattr(yahoo_cache, "get_closed_candles", AsyncMock(return_value=cached))
    monkeypatch.setattr(yahoo.settings, "yahoo_ohlcv_cache_enabled", True, raising=False)

    result = await yahoo.fetch_ohlcv("AAPL", days=3, period="day")

    assert len(result) == 3
```

추가 케이스:
- cache `None`이면 `_fetch_ohlcv_raw` fallback
- fallback 결과에서 `date > last_closed_bucket` 제거
- week/month도 동일 정책 적용

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_yahoo_service_cache.py -v`  
Expected: FAIL (캐시 경계 미연결)

**Step 3: Write minimal implementation**

`app/services/yahoo.py`에서 raw 함수를 분리하고 캐시를 주입한다.

```python
async def fetch_ohlcv(...):
    normalized_period = str(period or "").strip().lower()
    if normalized_period in {"day", "week", "month"} and settings.yahoo_ohlcv_cache_enabled:
        cached = await yahoo_ohlcv_cache.get_closed_candles(
            ticker,
            count=days,
            period=normalized_period,
            raw_fetcher=_fetch_ohlcv_raw,
        )
        if cached is not None:
            return cached

    raw = await _fetch_ohlcv_raw(...)
    if normalized_period in {"day", "week", "month"}:
        return _filter_closed_buckets_nyse(raw, normalized_period)
    return raw
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_yahoo_service_cache.py -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/yahoo.py tests/test_yahoo_service_cache.py
git commit -m "feat: apply yahoo ohlcv cache at service boundary"
```

### Task 5: Add lock contention/fallback hardening and observability tests

**Files:**
- Modify: `app/services/yahoo_ohlcv_cache.py`
- Modify: `tests/test_yahoo_ohlcv_cache.py`

**Step 1: Write the failing test**

락 경합 및 장애 폴백 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_get_closed_candles_returns_none_when_lock_contention_and_cache_stale(monkeypatch):
    monkeypatch.setattr(yahoo_ohlcv_cache, "_acquire_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(yahoo_ohlcv_cache.asyncio, "sleep", AsyncMock())
    result = await yahoo_ohlcv_cache.get_closed_candles("AAPL", count=30, period="day", raw_fetcher=AsyncMock())
    assert result is None
```

추가 케이스:
- Redis 예외 시 fallback `None` 반환
- `_FALLBACK_COUNT` 증가
- 경고 로그 메시지 키(`yahoo_ohlcv_cache fallback`) 포함

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_yahoo_ohlcv_cache.py -k "lock_contention or fallback" -v`  
Expected: FAIL

**Step 3: Write minimal implementation**

- lock 재시도(짧은 sleep 2회) 추가
- 예외 처리 경로에서 fallback 카운트/경고 로그 기록

```python
except Exception as exc:
    _FALLBACK_COUNT += 1
    logger.warning(
        "yahoo_ohlcv_cache fallback ticker=%s period=%s fallback_count=%d error=%s",
        normalized_ticker,
        normalized_period,
        _FALLBACK_COUNT,
        exc,
    )
    return None
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_yahoo_ohlcv_cache.py -k "lock_contention or fallback" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/yahoo_ohlcv_cache.py tests/test_yahoo_ohlcv_cache.py
git commit -m "fix: harden yahoo cache lock contention and fallback path"
```

### Task 6: Document behavior and run final verification gates

**Files:**
- Modify: `app/mcp_server/README.md`
- Modify: `docs/plans/2026-02-17-yahoo-us-market-closed-candle-cache-design.md` (if drift exists)

**Step 1: Update docs**

`app/mcp_server/README.md`에 Yahoo 캐시 정책을 명시한다.

```md
- Yahoo OHLCV(day/week/month) requests use Redis closed-candle cache at service boundary.
- Closed-bucket cutoff uses NYSE session close times via exchange_calendars (handles DST/holidays/early close).
```

**Step 2: Run focused quality gates**

Run: `uv run pytest tests/test_yahoo_ohlcv_cache.py tests/test_yahoo_service_cache.py -v`  
Expected: PASS

Run: `uv run pytest tests/test_services.py -k "TestYahooService and fetch_ohlcv" -v`  
Expected: PASS

Run: `uv run ruff check app/services/yahoo.py app/services/yahoo_ohlcv_cache.py tests/test_yahoo_ohlcv_cache.py tests/test_yahoo_service_cache.py`  
Expected: PASS

Run: `uv run pyright app/services/yahoo.py app/services/yahoo_ohlcv_cache.py`  
Expected: PASS

**Step 3: Commit**

```bash
git add app/mcp_server/README.md docs/plans/2026-02-17-yahoo-us-market-closed-candle-cache-design.md
git commit -m "docs: document yahoo closed-candle cache behavior"
```
