# KIS KR OHLCV 1H + Day Cache Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** MCP `get_ohlcv`에서 KR `1h`를 지원하고, KR `day`/`1h`를 Redis 캐시 경유로 제공하되 `1h` 미완성봉을 포함한 기존 응답 계약을 유지한다.

**Architecture:** MCP 계층은 KR `period` 라우팅만 확장하고, 실제 원격 호출/집계는 `KISClient`에 위임한다. 캐시는 신규 `app/services/kis_ohlcv_cache.py`에서 `day`/`1h` 공통 키 스키마로 처리하며, `end_date` 지정 요청은 캐시를 우회한다. Redis 실패 시 원본 조회로 fallback하여 기능을 유지한다.

**Tech Stack:** Python 3.13+, FastMCP, pandas, redis.asyncio, pytest, Ruff, Pyright

---

### Task 1: MCP KR 1H 계약 테스트를 먼저 실패로 고정

**Files:**
- Modify: `tests/test_mcp_server_tools.py`
- Reference: `app/mcp_server/tooling/market_data_quotes.py`

**Step 1: Write failing tests for KR `1h` and cache policy**

`tests/test_mcp_server_tools.py`에 아래 테스트를 추가/교체한다.

```python
@pytest.mark.asyncio
async def test_get_ohlcv_kr_equity_period_1h(monkeypatch):
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_time_dailychartprice(self, code, market, n, end_date=None):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    result = await tools["get_ohlcv"]("005930", market="kr", count=50, period="1h")

    assert result["instrument_type"] == "equity_kr"
    assert result["period"] == "1h"
    assert result["source"] == "kis"


@pytest.mark.asyncio
async def test_get_ohlcv_kr_1h_mock_unsupported_returns_error_payload(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def inquire_time_dailychartprice(self, code, market, n, end_date=None):
            raise RuntimeError("mock trading does not support inquire-time-dailychartprice")

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    result = await tools["get_ohlcv"]("005930", market="kr", period="1h")

    assert result["source"] == "kis"
    assert result["instrument_type"] == "equity_kr"
    assert "mock" in result["error"].lower()
```

기존 reject 테스트(`period '1h' is not supported for korean equity`)는 제거한다.

**Step 2: Run tests to verify failure first**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py -k "get_ohlcv and kr and 1h" -q`  
Expected: FAIL (`get_ohlcv`가 아직 KR `1h`를 허용하지 않음)

**Step 3: Commit test-only change**

```bash
git add tests/test_mcp_server_tools.py
git commit -m "test: add KR 1h get_ohlcv contract coverage"
```

---

### Task 2: KIS 캐시 설정값(TDD) 추가

**Files:**
- Modify: `tests/test_config.py`
- Modify: `app/core/config.py`

**Step 1: Add failing config tests**

`tests/test_config.py`에 속성 존재 검증을 추가한다.

```python
def test_has_kis_ohlcv_cache_settings():
    assert hasattr(settings, "kis_ohlcv_cache_enabled")
    assert hasattr(settings, "kis_ohlcv_cache_max_days")
    assert hasattr(settings, "kis_ohlcv_cache_max_hours")
    assert hasattr(settings, "kis_ohlcv_cache_lock_ttl_seconds")
```

**Step 2: Run test to verify failure**

Run: `uv run pytest --no-cov tests/test_config.py -k "kis_ohlcv_cache" -q`  
Expected: FAIL (신규 설정 미정의)

**Step 3: Add minimal settings implementation**

`app/core/config.py`에 기본값 추가:

```python
kis_ohlcv_cache_enabled: bool = True
kis_ohlcv_cache_max_days: int = 400
kis_ohlcv_cache_max_hours: int = 400 * 24
kis_ohlcv_cache_lock_ttl_seconds: int = 10
```

**Step 4: Re-run tests**

Run: `uv run pytest --no-cov tests/test_config.py -k "kis_ohlcv_cache" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/core/config.py tests/test_config.py
git commit -m "feat: add KIS OHLCV cache settings"
```

---

### Task 3: KIS `inquire_time_dailychartprice` + 1H 집계 구현(TDD)

**Files:**
- Modify: `tests/test_services.py`
- Modify: `app/services/kis.py`

**Step 1: Add failing tests for new KIS method and aggregation**

`tests/test_services.py`에 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_kis_inquire_time_dailychartprice_parses_rows(monkeypatch):
    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    monkeypatch.setattr(
        client,
        "_request_with_rate_limit",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "output2": [
                    {
                        "stck_bsop_date": "20260219",
                        "stck_cntg_hour": "100000",
                        "stck_oprc": "70000",
                        "stck_hgpr": "70200",
                        "stck_lwpr": "69900",
                        "stck_prpr": "70100",
                        "cntg_vol": "100",
                        "acml_tr_pbmn": "7010000",
                    }
                ],
            }
        ),
    )
    df = await client.inquire_time_dailychartprice("005930", market="UN", n=1)
    assert len(df) == 1
    assert {"datetime", "open", "high", "low", "close", "volume", "value"} <= set(df.columns)


def test_aggregate_to_hourly_keeps_partial_bucket():
    df = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-02-19 10:10:00", "2026-02-19 10:20:00"]),
            "open": [1, 2],
            "high": [3, 4],
            "low": [1, 2],
            "close": [2, 3],
            "volume": [10, 20],
            "value": [100, 200],
        }
    )
    out = KISClient._aggregate_intraday_to_hour(df)
    assert len(out) == 1
    assert out.iloc[0]["close"] == 3
```

**Step 2: Run tests to confirm failure**

Run: `uv run pytest --no-cov tests/test_services.py -k "time_dailychartprice or aggregate_to_hourly" -q`  
Expected: FAIL (신규 메서드/헬퍼 없음)

**Step 3: Implement minimal KIS method + aggregation**

`app/services/kis.py`에 추가:

```python
async def inquire_time_dailychartprice(...): ...

@staticmethod
def _aggregate_intraday_to_hour(df: pd.DataFrame) -> pd.DataFrame:
    grouped = (
        df.assign(hour_bucket=lambda d: d["datetime"].dt.floor("60min"))
        .groupby("hour_bucket", as_index=False)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            value=("value", "sum"),
        )
        .rename(columns={"hour_bucket": "datetime"})
    )
    return grouped
```

**Step 4: Re-run tests**

Run: `uv run pytest --no-cov tests/test_services.py -k "time_dailychartprice or aggregate_to_hourly" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/kis.py tests/test_services.py
git commit -m "feat: add KIS time-dailychartprice and hourly aggregation"
```

---

### Task 4: KIS OHLCV Redis 캐시 모듈 추가(TDD)

**Files:**
- Create: `app/services/kis_ohlcv_cache.py`
- Create: `tests/test_kis_ohlcv_cache.py`

**Step 1: Write failing cache tests**

`tests/test_kis_ohlcv_cache.py`에 최소 계약 테스트를 작성한다.

```python
@pytest.mark.asyncio
async def test_get_candles_returns_cached_when_sufficient(monkeypatch):
    # fake redis에 day row 2건 저장 후 raw_fetcher가 호출되지 않는지 검증
    ...


@pytest.mark.asyncio
async def test_get_candles_fallbacks_to_raw_on_redis_error(monkeypatch):
    # _get_redis_client 예외 시 raw_fetcher 반환 검증
    ...
```

**Step 2: Run tests to verify failure**

Run: `uv run pytest --no-cov tests/test_kis_ohlcv_cache.py -q`  
Expected: FAIL (모듈 미존재)

**Step 3: Implement minimal cache module**

`app/services/kis_ohlcv_cache.py` 구현:

```python
_SUPPORTED_PERIODS = {"day", "1h"}

async def get_candles(
    symbol: str,
    count: int,
    period: str,
    raw_fetcher: Callable[[int], Awaitable[pd.DataFrame]],
) -> pd.DataFrame:
    ...
```

키/락/retention/fallback은 `yahoo_ohlcv_cache.py`, `upbit_ohlcv_cache.py` 패턴을 재사용한다.

**Step 4: Re-run tests**

Run: `uv run pytest --no-cov tests/test_kis_ohlcv_cache.py -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/kis_ohlcv_cache.py tests/test_kis_ohlcv_cache.py
git commit -m "feat: add Redis cache for KIS day and 1h ohlcv"
```

---

### Task 5: MCP KR `day`/`1h` 라우팅 및 캐시 통합

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Modify: `tests/test_mcp_server_tools.py`

**Step 1: Add failing integration tests for cache bypass and day cache usage**

```python
@pytest.mark.asyncio
async def test_get_ohlcv_kr_day_bypasses_cache_when_end_date_provided(monkeypatch):
    ...
```

**Step 2: Run tests to verify failure**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py -k "get_ohlcv and kr and (1h or day)" -q`  
Expected: FAIL

**Step 3: Implement routing**

`_fetch_ohlcv_equity_kr`를 분기한다.

```python
if period == "1h":
    # KIS 분봉 -> 1시간 집계
elif period == "day":
    # KIS 일봉
else:
    # 기존 week/month
```

캐시 사용 조건:

```python
use_cache = end_date is None and settings.kis_ohlcv_cache_enabled and period in {"day", "1h"}
```

**Step 4: Re-run tests**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py -k "get_ohlcv and kr and (1h or day)" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/market_data_quotes.py tests/test_mcp_server_tools.py
git commit -m "feat: support KR 1h get_ohlcv with day/1h cache integration"
```

---

### Task 6: 문서 동기화 + 전체 검증

**Files:**
- Modify: `app/mcp_server/README.md`

**Step 1: Update README tool contract**

`get_ohlcv` period 설명을 아래로 갱신한다.

```markdown
- period: `day`, `week`, `month`, `4h`, `1h`
- `4h`: crypto only
- `1h`: KR/US equity + crypto
- KR `1h` includes in-progress (partial) hourly candle
```

**Step 2: Run quality gates**

Run:

```bash
uv run ruff check app/services/kis.py app/services/kis_ohlcv_cache.py app/mcp_server/tooling/market_data_quotes.py tests/test_mcp_server_tools.py tests/test_services.py tests/test_kis_ohlcv_cache.py
uv run pyright app/services/kis.py app/services/kis_ohlcv_cache.py app/mcp_server/tooling/market_data_quotes.py
uv run pytest --no-cov tests/test_mcp_server_tools.py -k "get_ohlcv and (kr or 1h or day)" -q
uv run pytest --no-cov tests/test_services.py -k "time_dailychartprice or aggregate_to_hourly" -q
uv run pytest --no-cov tests/test_kis_ohlcv_cache.py -q
```

Expected: All PASS

**Step 3: Final commit**

```bash
git add app/mcp_server/README.md
git commit -m "docs: update get_ohlcv KR 1h support and cache policy"
```

---

### Task 7: 통합 PR 정리

**Files:**
- Modify: (none, release note only)

**Step 1: Summarize behavior changes**

PR 본문에 다음을 포함한다.

- KR `get_ohlcv(period="1h")` 지원
- KR `day/1h` Redis cache 도입
- `1h` 미완성봉 포함 정책
- `end_date` 지정 시 cache bypass
- mock 미지원 에러 처리

**Step 2: Final sanity check**

Run: `git log --oneline -n 8`  
Expected: task 단위 커밋 메시지 정렬 확인

**Step 3: Push and open PR**

```bash
git push origin <branch>
gh pr create --base main --head <branch> --title "feat: support KR 1h OHLCV with Redis cache" --body-file <prepared-body-file>
```

