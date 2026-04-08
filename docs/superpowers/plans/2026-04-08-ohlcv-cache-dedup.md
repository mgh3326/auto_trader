# OHLCV Cache 3중 복제 제거 구현 플랜

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 3개 OHLCV Redis 캐시 모듈의 동일 함수를 공통 모듈로 추출하여 중복을 제거한다. 로직 변경 없이 순수 구조 리팩토링.

**Architecture:** Shared utility module (composition) 패턴. 동일한 순수 함수들을 `ohlcv_cache_common.py`로 추출하고, 각 서비스 모듈이 `from ... import` 로 재사용. 기존 모듈 레벨 함수명 유지로 테스트 monkeypatch 호환성 보장. 테스트 FakeRedis도 공유 헬퍼로 통합.

**Tech Stack:** Python 3.13+, redis.asyncio, pandas, exchange_calendars, pytest-asyncio

---

## 설계 결정: Composition (합성) 선택 이유

**베이스 클래스(상속) 불가 사유:**
1. 테스트가 `monkeypatch.setattr(upbit_ohlcv_cache, "_get_redis_client", mock)` 패턴으로 모듈 레벨 이름을 대체함. 클래스 메서드가 `self._get_redis_client()`를 호출하면 monkeypatch가 무효화됨
2. KIS는 backfill/meta/oldest_confirmed가 없어 빈 override가 필요 — 상속 anti-pattern
3. `_REDIS_CLIENT` 모듈 글로벌을 테스트가 직접 `= None`으로 리셋 — 클래스 변수와 호환 불가

**Composition 동작 원리:**
`from ohlcv_cache_common import _acquire_lock` → `upbit_ohlcv_cache._acquire_lock`가 모듈 레벨 이름으로 등록됨 → Python 함수는 같은 모듈의 글로벌 네임스페이스를 통해 이름 조회 → `monkeypatch.setattr(upbit_ohlcv_cache, "_acquire_lock", mock)` 정상 동작

**내부 호출 안전성 검증:**
`_read_cache_status`가 `_read_latest_date`를 호출하는 등 common 내부 호출이 있음. 테스트가 `_read_latest_date`, `_read_oldest_date`, `_epoch_day`, `_to_json_value` 등을 직접 monkeypatch하는 케이스는 없으므로 common 모듈 내부 호출은 안전함.

---

## 파일 구조

| 파일 | 변경 | Before | After | 비고 |
|------|------|--------|-------|------|
| `app/services/ohlcv_cache_common.py` | **NEW** | — | ~170줄 | 공통 함수 |
| `app/services/upbit_ohlcv_cache.py` | MODIFY | 742줄 | ~370줄 | -50% |
| `app/services/yahoo_ohlcv_cache.py` | MODIFY | 725줄 | ~380줄 | -48% |
| `app/services/kis_ohlcv_cache.py` | MODIFY | 570줄 | ~350줄 | -39% |
| `tests/ohlcv_cache_fakes.py` | **NEW** | — | ~130줄 | FakeRedis 통합 |
| `tests/test_upbit_ohlcv_cache.py` | MODIFY | 644줄 | ~300줄 | FakeRedis 임포트 |
| `tests/test_yahoo_ohlcv_cache.py` | MODIFY | ~730줄 | ~460줄 | FakeRedis 임포트 |
| `tests/test_kis_ohlcv_cache.py` | MODIFY | ~500줄 | ~340줄 | FakeRedis 임포트 |

**총 소스 코드:** 2,037줄 → ~1,270줄 (38% 감소)
**총 테스트 코드:** ~1,874줄 → ~1,230줄 (34% 감소)

---

## 추출 대상 함수 (common 모듈로 이동)

### 3개 모듈 공통 (all 3)
| 함수 | 줄수 | 비고 |
|------|------|------|
| `_to_json_value(value)` | 4 | 완전 동일 |
| `_acquire_lock(redis_client, lock_key, ttl_seconds)` | 11 | 완전 동일 |
| `_release_lock(redis_client, lock_key, lock_token)` | 12 | 완전 동일 |
| `_enforce_retention_limit(redis_client, dates_key, rows_key, max_items)` | 18 | KIS는 `<=0` 가드 없지만 caller가 항상 >=1 전달하므로 호환 |

### upbit/yahoo만 공통
| 함수 | 줄수 | 비고 |
|------|------|------|
| `_normalize_bool(value)` | 5 | 완전 동일 |
| `_epoch_day(value: date)` | 3 | 완전 동일 |
| `_empty_dataframe()` | 2 | 완전 동일 (7-column) |
| `_read_cached_rows(redis_client, dates_key, rows_key, target_closed_date, count)` | 47 | 완전 동일 |
| `_upsert_rows(redis_client, dates_key, rows_key, frame)` | 42 | 완전 동일 |
| `_read_oldest_date(redis_client, dates_key)` | 6 | 완전 동일 |
| `_read_latest_date(redis_client, dates_key)` | 10 | 완전 동일 |
| `_read_cache_status(redis_client, dates_key, meta_key, target_closed_date)` | 7 | 완전 동일 |
| `_refresh_meta(redis_client, dates_key, meta_key, target, oldest_confirmed, meta_date_field)` | 10 | 파라미터 추가: `meta_date_field` (upbit="last_closed_date", yahoo="last_closed_bucket") |

### 추출하지 않는 항목 (각 모듈에 유지)
| 항목 | 사유 |
|------|------|
| `_REDIS_CLIENT`, `_FALLBACK_COUNT` 글로벌 | 테스트가 `module._REDIS_CLIENT = None` 직접 설정 |
| `_get_redis_client()`, `close_ohlcv_cache_redis()` | 모듈별 독립 Redis 커넥션 + 테스트 monkeypatch |
| `_normalize_period()` | 4줄 + 모듈별 `_SUPPORTED_PERIODS` 다름 |
| `_base_key()`, `_keys()` | prefix/route 파라미터 다름 |
| `_bucket_gap_count()` | upbit은 단순 날짜, yahoo는 ISO week 기반 |
| `_is_cache_sufficient()` | 시그니처 + 비교 로직 다름 |
| `_backfill_until_satisfied()` | 로깅, 필터, break 조건 다름 |
| `get_closed_candles()` / `get_candles()` | 오케스트레이션 로직 서비스별 고유 |
| KIS 전용 함수들 | `_canonicalize_frame`, `_is_cache_fresh`, `_field_and_score`, `_parse_cached_row` 등 |

---

## Task 0: 베이스라인 검증

**Files:** (none)

- [ ] **Step 1: 전체 테스트 실행으로 현재 상태 확인**

Run: `make test-unit`
Expected: ALL PASS (현재 테스트 수 기록)

- [ ] **Step 2: Commit baseline (선택)**

현재 브랜치가 clean 상태이므로 skip 가능.

---

## Task 1: `app/services/ohlcv_cache_common.py` 생성

**Files:**
- Create: `app/services/ohlcv_cache_common.py`
- Create: `tests/test_ohlcv_cache_common.py`

- [ ] **Step 1: 테스트 파일 작성**

```python
# tests/test_ohlcv_cache_common.py
"""Tests for the shared OHLCV cache utility module."""
import json
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services import ohlcv_cache_common as common


class TestToJsonValue:
    def test_nan_returns_none(self):
        assert common._to_json_value(float("nan")) is None

    def test_int_returns_float(self):
        assert common._to_json_value(42) == 42.0

    def test_float_passthrough(self):
        assert common._to_json_value(3.14) == 3.14

    def test_string_passthrough(self):
        assert common._to_json_value("hello") == "hello"

    def test_none_returns_none(self):
        assert common._to_json_value(None) is None


class TestNormalizeBool:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            (None, False),
            ("true", True),
            ("false", False),
            ("1", True),
            ("0", False),
            ("yes", True),
            ("on", True),
            ("off", False),
        ],
    )
    def test_values(self, value, expected):
        assert common._normalize_bool(value) is expected


class TestEpochDay:
    def test_known_date(self):
        result = common._epoch_day(date(2026, 2, 14))
        expected = int(
            datetime(2026, 2, 14, tzinfo=UTC).timestamp() // 86400
        )
        assert result == expected

    def test_epoch_origin(self):
        assert common._epoch_day(date(1970, 1, 1)) == 0


class TestEmptyDataframe:
    def test_has_correct_columns(self):
        df = common._empty_dataframe()
        assert list(df.columns) == [
            "date", "open", "high", "low", "close", "volume", "value"
        ]
        assert len(df) == 0


class TestAcquireLock:
    @pytest.mark.asyncio
    async def test_acquire_succeeds(self):
        redis_client = AsyncMock()
        redis_client.set = AsyncMock(return_value=True)
        token = await common._acquire_lock(redis_client, "lock:test", 10)
        assert token is not None
        redis_client.set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_acquire_fails_when_held(self):
        redis_client = AsyncMock()
        redis_client.set = AsyncMock(return_value=False)
        token = await common._acquire_lock(redis_client, "lock:test", 10)
        assert token is None


class TestReleaseLock:
    @pytest.mark.asyncio
    async def test_release_calls_eval(self):
        redis_client = AsyncMock()
        await common._release_lock(redis_client, "lock:test", "tok-123")
        redis_client.eval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_swallows_errors(self):
        redis_client = AsyncMock()
        redis_client.eval = AsyncMock(side_effect=RuntimeError("oops"))
        await common._release_lock(redis_client, "lock:test", "tok-123")


class TestEnforceRetentionLimit:
    @pytest.mark.asyncio
    async def test_no_overflow_returns_zero(self):
        redis_client = AsyncMock()
        redis_client.zcard = AsyncMock(return_value=5)
        result = await common._enforce_retention_limit(
            redis_client, "dates", "rows", 10
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_zero_max_returns_zero(self):
        redis_client = AsyncMock()
        result = await common._enforce_retention_limit(
            redis_client, "dates", "rows", 0
        )
        assert result == 0


class TestRefreshMeta:
    @pytest.mark.asyncio
    async def test_uses_custom_meta_date_field(self):
        redis_client = AsyncMock()
        redis_client.zrange = AsyncMock(return_value=["2026-01-01"])
        redis_client.hset = AsyncMock()

        await common._refresh_meta(
            redis_client, "dates", "meta",
            date(2026, 2, 14), True,
            meta_date_field="last_closed_bucket",
        )

        call_args = redis_client.hset.call_args
        mapping = call_args.kwargs.get("mapping") or call_args[1].get("mapping")
        assert "last_closed_bucket" in mapping
        assert "last_closed_date" not in mapping

    @pytest.mark.asyncio
    async def test_default_meta_date_field(self):
        redis_client = AsyncMock()
        redis_client.zrange = AsyncMock(return_value=["2026-01-01"])
        redis_client.hset = AsyncMock()

        await common._refresh_meta(
            redis_client, "dates", "meta",
            date(2026, 2, 14), False,
        )

        call_args = redis_client.hset.call_args
        mapping = call_args.kwargs.get("mapping") or call_args[1].get("mapping")
        assert "last_closed_date" in mapping
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_ohlcv_cache_common.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ohlcv_cache_common'`

- [ ] **Step 3: `ohlcv_cache_common.py` 구현**

```python
# app/services/ohlcv_cache_common.py
"""Shared utilities for OHLCV Redis cache modules.

Upbit, Yahoo, KIS 캐시 모듈이 공유하는 순수 함수 모음.
각 서비스 모듈에서 `from app.services.ohlcv_cache_common import ...` 로 사용.
"""
import json
import uuid
from datetime import UTC, date, datetime

import pandas as pd
import redis.asyncio as redis

_EMPTY_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]


# ---------------------------------------------------------------------------
# Pure utilities
# ---------------------------------------------------------------------------

def _to_json_value(value: object) -> object:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return value


def _normalize_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _epoch_day(value: date) -> int:
    return int(
        datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp()
        // 86400
    )


def _empty_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=_EMPTY_COLUMNS)


# ---------------------------------------------------------------------------
# Redis lock
# ---------------------------------------------------------------------------

async def _acquire_lock(
    redis_client: redis.Redis,
    lock_key: str,
    ttl_seconds: int,
) -> str | None:
    lock_token = f"{uuid.uuid4()}"
    acquired = await redis_client.set(
        lock_key,
        lock_token,
        nx=True,
        ex=max(int(ttl_seconds), 1),
    )
    if acquired:
        return lock_token
    return None


async def _release_lock(
    redis_client: redis.Redis,
    lock_key: str,
    lock_token: str,
) -> None:
    release_script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    else
        return 0
    end
    """
    try:
        await redis_client.eval(release_script, 1, lock_key, lock_token)
    except Exception:
        return


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

async def _enforce_retention_limit(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    max_items: int,
) -> int:
    if max_items <= 0:
        return 0

    total_count = int(await redis_client.zcard(dates_key))
    overflow = total_count - max_items
    if overflow <= 0:
        return 0

    stale_fields = await redis_client.zrange(dates_key, 0, overflow - 1)
    if not stale_fields:
        return 0

    pipeline = redis_client.pipeline(transaction=True)
    pipeline.zremrangebyrank(dates_key, 0, overflow - 1)
    pipeline.hdel(rows_key, *stale_fields)
    await pipeline.execute()
    return len(stale_fields)


# ---------------------------------------------------------------------------
# Date helpers (upbit/yahoo shared)
# ---------------------------------------------------------------------------

async def _read_oldest_date(
    redis_client: redis.Redis, dates_key: str
) -> date | None:
    oldest_dates = await redis_client.zrange(dates_key, 0, 0)
    if not oldest_dates:
        return None
    try:
        return date.fromisoformat(oldest_dates[0])
    except ValueError:
        return None


async def _read_latest_date(
    redis_client: redis.Redis, dates_key: str
) -> date | None:
    latest_dates = await redis_client.zrevrangebyscore(
        dates_key, "+inf", "-inf", start=0, num=1,
    )
    if not latest_dates:
        return None
    try:
        return date.fromisoformat(latest_dates[0])
    except ValueError:
        return None


async def _read_cache_status(
    redis_client: redis.Redis,
    dates_key: str,
    meta_key: str,
    target_closed_date: date,
) -> tuple[int, date | None, bool]:
    cached_count = int(
        await redis_client.zcount(
            dates_key, "-inf", _epoch_day(target_closed_date)
        )
    )
    latest_cached_date = await _read_latest_date(redis_client, dates_key)
    meta = await redis_client.hgetall(meta_key)
    oldest_confirmed = _normalize_bool(meta.get("oldest_confirmed"))
    return cached_count, latest_cached_date, oldest_confirmed


# ---------------------------------------------------------------------------
# Daily read/write (upbit/yahoo shared)
# ---------------------------------------------------------------------------

async def _read_cached_rows(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    target_closed_date: date,
    count: int,
) -> pd.DataFrame:
    if count <= 0:
        return _empty_dataframe()

    date_fields = await redis_client.zrevrangebyscore(
        dates_key,
        _epoch_day(target_closed_date),
        "-inf",
        start=0,
        num=count,
    )
    if not date_fields:
        return _empty_dataframe()

    row_payloads = await redis_client.hmget(rows_key, date_fields)
    rows: list[dict[str, object]] = []
    for field, payload in zip(date_fields, row_payloads, strict=False):
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        date_value = parsed.get("date", field)
        try:
            parsed["date"] = date.fromisoformat(str(date_value))
        except ValueError:
            continue
        rows.append(parsed)

    if not rows:
        return _empty_dataframe()

    frame = pd.DataFrame(rows)
    for column in _EMPTY_COLUMNS:
        if column not in frame.columns:
            frame[column] = None

    return frame.loc[:, _EMPTY_COLUMNS].sort_values("date").reset_index(
        drop=True
    )


async def _upsert_rows(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    frame: pd.DataFrame,
) -> int:
    if frame.empty:
        return 0

    zadd_mapping: dict[str, int] = {}
    hset_mapping: dict[str, str] = {}

    for row in frame.itertuples(index=False):
        row_date = getattr(row, "date", None)
        if row_date is None:
            continue
        if not isinstance(row_date, date):
            try:
                row_date = pd.to_datetime(row_date).date()
            except Exception:
                continue

        field = row_date.isoformat()
        zadd_mapping[field] = _epoch_day(row_date)
        payload = {
            "date": field,
            "open": _to_json_value(getattr(row, "open", None)),
            "high": _to_json_value(getattr(row, "high", None)),
            "low": _to_json_value(getattr(row, "low", None)),
            "close": _to_json_value(getattr(row, "close", None)),
            "volume": _to_json_value(getattr(row, "volume", None)),
            "value": _to_json_value(getattr(row, "value", None)),
        }
        hset_mapping[field] = json.dumps(payload)

    if not zadd_mapping or not hset_mapping:
        return 0

    pipeline = redis_client.pipeline(transaction=True)
    pipeline.zadd(dates_key, zadd_mapping)
    pipeline.hset(rows_key, mapping=hset_mapping)
    await pipeline.execute()
    return len(zadd_mapping)


# ---------------------------------------------------------------------------
# Meta refresh (upbit/yahoo shared, parameterized)
# ---------------------------------------------------------------------------

async def _refresh_meta(
    redis_client: redis.Redis,
    dates_key: str,
    meta_key: str,
    target_closed_date: date,
    oldest_confirmed: bool,
    meta_date_field: str = "last_closed_date",
) -> None:
    oldest_date = await _read_oldest_date(redis_client, dates_key)
    mapping = {
        meta_date_field: target_closed_date.isoformat(),
        "oldest_date": oldest_date.isoformat() if oldest_date else "",
        "oldest_confirmed": "true" if oldest_confirmed else "false",
        "last_sync_ts": str(int(datetime.now(UTC).timestamp())),
    }
    await redis_client.hset(meta_key, mapping=mapping)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_ohlcv_cache_common.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/ohlcv_cache_common.py tests/test_ohlcv_cache_common.py
git commit -m "feat: add ohlcv_cache_common module with shared cache utilities"
```

---

## Task 2: `tests/ohlcv_cache_fakes.py` 생성 (테스트 FakeRedis 통합)

**Files:**
- Create: `tests/ohlcv_cache_fakes.py`

- [ ] **Step 1: 통합 FakeRedis 모듈 작성**

3개 테스트 파일의 `_FakeRedis`/`_FakePipeline`을 통합한 superset 생성. upbit/yahoo의 `zcount`/`zrevrangebyscore` + KIS의 `zrevrange` 모두 포함.

```python
# tests/ohlcv_cache_fakes.py
"""Shared fake Redis implementation for OHLCV cache tests."""
from __future__ import annotations

from typing import Any


class FakePipeline:
    def __init__(self, redis_client: FakeRedis):
        self.redis_client = redis_client
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def zremrangebyrank(self, *args: Any, **kwargs: Any) -> FakePipeline:
        self.commands.append(("zremrangebyrank", args, kwargs))
        return self

    def hdel(self, *args: Any, **kwargs: Any) -> FakePipeline:
        self.commands.append(("hdel", args, kwargs))
        return self

    def zadd(self, *args: Any, **kwargs: Any) -> FakePipeline:
        self.commands.append(("zadd", args, kwargs))
        return self

    def hset(self, *args: Any, **kwargs: Any) -> FakePipeline:
        self.commands.append(("hset", args, kwargs))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for method_name, args, kwargs in self.commands:
            method = getattr(self.redis_client, method_name)
            results.append(await method(*args, **kwargs))
        self.commands.clear()
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        del transaction
        return FakePipeline(self)

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        del ex
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    async def eval(
        self,
        script: str,
        key_count: int,
        key: str,
        token: str,
    ) -> int:
        del script, key_count
        if self.strings.get(key) == token:
            self.strings.pop(key, None)
            return 1
        return 0

    async def zadd(
        self, key: str, mapping: dict[str, int | float]
    ) -> int:
        zset = self.zsets.setdefault(key, {})
        inserted = 0
        for member, score in mapping.items():
            if member not in zset:
                inserted += 1
            zset[member] = float(score)
        return inserted

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def zcount(
        self,
        key: str,
        minimum: str | int | float,
        maximum: str | int | float,
    ) -> int:
        zset = self.zsets.get(key, {})
        min_score = self._normalize_score(minimum, is_min=True)
        max_score = self._normalize_score(maximum, is_min=False)
        return sum(
            1 for score in zset.values() if min_score <= score <= max_score
        )

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        items = sorted(
            self.zsets.get(key, {}).items(),
            key=lambda item: (item[1], item[0]),
        )
        members = [member for member, _ in items]
        if not members:
            return []
        if end < 0:
            end = len(members) + end
        if end < start:
            return []
        return members[start : end + 1]

    async def zrevrange(self, key: str, start: int, end: int) -> list[str]:
        items = sorted(
            self.zsets.get(key, {}).items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )
        members = [member for member, _ in items]
        if not members:
            return []
        if end < 0:
            end = len(members) + end
        if end < start:
            return []
        return members[start : end + 1]

    async def zrevrangebyscore(
        self,
        key: str,
        maximum: str | int | float,
        minimum: str | int | float,
        start: int = 0,
        num: int | None = None,
    ) -> list[str]:
        zset = self.zsets.get(key, {})
        min_score = self._normalize_score(minimum, is_min=True)
        max_score = self._normalize_score(maximum, is_min=False)
        items = [
            (member, score)
            for member, score in zset.items()
            if min_score <= score <= max_score
        ]
        items.sort(key=lambda item: (item[1], item[0]), reverse=True)
        members = [member for member, _ in items]
        if num is None:
            return members[start:]
        return members[start : start + num]

    async def zremrangebyrank(
        self, key: str, start: int, end: int
    ) -> int:
        members = await self.zrange(key, 0, -1)
        if not members:
            return 0
        if end < 0:
            end = len(members) + end
        if end < start:
            return 0
        removable = members[start : end + 1]
        zset = self.zsets.get(key, {})
        for member in removable:
            zset.pop(member, None)
        return len(removable)

    async def hset(self, key: str, mapping: dict[str, str]) -> int:
        target = self.hashes.setdefault(key, {})
        inserted = 0
        for field, value in mapping.items():
            if field not in target:
                inserted += 1
            target[field] = value
        return inserted

    async def hmget(self, key: str, fields: list[str]) -> list[str | None]:
        target = self.hashes.get(key, {})
        return [target.get(field) for field in fields]

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key: str, *fields: str) -> int:
        target = self.hashes.get(key, {})
        removed = 0
        for field in fields:
            if field in target:
                removed += 1
                target.pop(field, None)
        return removed

    @staticmethod
    def _normalize_score(
        value: str | int | float, is_min: bool
    ) -> float:
        if isinstance(value, str):
            if value == "-inf":
                return float("-inf")
            if value == "+inf":
                return float("inf")
        parsed = float(value)
        if parsed == float("-inf") and not is_min:
            return float("-inf")
        if parsed == float("inf") and is_min:
            return float("inf")
        return parsed
```

- [ ] **Step 2: FakeRedis가 기존 테스트 패턴과 호환되는지 smoke test**

Run: `uv run python -c "from tests.ohlcv_cache_fakes import FakeRedis, FakePipeline; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/ohlcv_cache_fakes.py
git commit -m "test: add shared FakeRedis helper for OHLCV cache tests"
```

---

## Task 3: `upbit_ohlcv_cache.py` 리팩토링

**Files:**
- Modify: `app/services/upbit_ohlcv_cache.py`
- Modify: `tests/test_upbit_ohlcv_cache.py`
- Test: `tests/test_upbit_ohlcv_cache.py`, `tests/test_upbit_service_cache.py`

- [ ] **Step 1: upbit_ohlcv_cache.py에서 공통 함수를 import로 교체**

변경 사항:
1. `import json`, `import uuid` 제거 (common에서 사용하므로)
2. 공통 함수 import 추가
3. 다음 함수 정의 삭제: `_normalize_bool`, `_empty_dataframe`, `_to_json_value`, `_epoch_day`, `_acquire_lock`, `_release_lock`, `_enforce_retention_limit`, `_read_cached_rows`, `_upsert_rows`, `_read_oldest_date`, `_read_latest_date`, `_read_cache_status`, `_refresh_meta`
4. `_EMPTY_COLUMNS` 정의를 import로 교체

**import 블록 변경:**
```python
# 기존:
import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta, timezone

import pandas as pd
import redis.asyncio as redis

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings

# 변경 후:
import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta, timezone

import pandas as pd
import redis.asyncio as redis

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
from app.services.ohlcv_cache_common import (
    _EMPTY_COLUMNS,
    _acquire_lock,
    _empty_dataframe,
    _enforce_retention_limit,
    _epoch_day,
    _normalize_bool,
    _read_cache_status,
    _read_cached_rows,
    _read_latest_date,
    _read_oldest_date,
    _refresh_meta,
    _release_lock,
    _to_json_value,
    _upsert_rows,
)
```

**삭제할 함수 목록 (정의 전체 삭제):**
- `_normalize_bool` (5줄)
- `_empty_dataframe` (2줄)
- `_to_json_value` (4줄)
- `_epoch_day` (3줄)
- `_acquire_lock` (14줄)
- `_release_lock` (16줄)
- `_enforce_retention_limit` (22줄)
- `_read_cached_rows` (47줄)
- `_upsert_rows` (42줄)
- `_read_oldest_date` (7줄)
- `_read_latest_date` (13줄)
- `_read_cache_status` (12줄)
- `_refresh_meta` (14줄)

**`_EMPTY_COLUMNS` 정의 삭제** (import로 대체됨)

**`_refresh_meta` 호출부 변경 불필요** — upbit의 meta key는 `"last_closed_date"` (common의 default)

- [ ] **Step 2: 테스트 파일에서 FakeRedis를 공유 모듈로 교체**

`tests/test_upbit_ohlcv_cache.py` 변경:
- `_FakePipeline`, `_FakeRedis` 클래스 정의 전체 삭제 (~110줄)
- import 추가: `from tests.ohlcv_cache_fakes import FakeRedis`
- 코드 내 `_FakeRedis()` → `FakeRedis()` 교체

- [ ] **Step 3: upbit 테스트 실행**

Run: `uv run pytest tests/test_upbit_ohlcv_cache.py tests/test_upbit_service_cache.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add app/services/upbit_ohlcv_cache.py tests/test_upbit_ohlcv_cache.py
git commit -m "refactor(upbit): extract shared functions to ohlcv_cache_common"
```

---

## Task 4: `yahoo_ohlcv_cache.py` 리팩토링

**Files:**
- Modify: `app/services/yahoo_ohlcv_cache.py`
- Modify: `tests/test_yahoo_ohlcv_cache.py`
- Test: `tests/test_yahoo_ohlcv_cache.py`, `tests/test_yahoo_service_cache.py`

- [ ] **Step 1: yahoo_ohlcv_cache.py에서 공통 함수를 import로 교체**

upbit과 동일 패턴. 추가 변경:

**`_refresh_meta` 호출부 변경 필요:**
yahoo의 `_backfill_until_satisfied`와 `get_closed_candles`에서 `_refresh_meta` 호출 시 `meta_date_field="last_closed_bucket"` 추가:

```python
# 기존:
await _refresh_meta(redis_client, dates_key, meta_key, target_closed_date, oldest_confirmed)
# 변경:
await _refresh_meta(redis_client, dates_key, meta_key, target_closed_date, oldest_confirmed, meta_date_field="last_closed_bucket")
```

이 변경은 `_backfill_until_satisfied` 내부 2곳 + `get_closed_candles` 내부 1곳 = **총 3곳**.

- [ ] **Step 2: 테스트 파일에서 FakeRedis를 공유 모듈로 교체**

`tests/test_yahoo_ohlcv_cache.py` 변경:
- `_FakePipeline`, `_FakeRedis` 클래스 삭제
- `from tests.ohlcv_cache_fakes import FakeRedis` 추가
- `_FakeRedis()` → `FakeRedis()` 교체

- [ ] **Step 3: yahoo 테스트 실행**

Run: `uv run pytest tests/test_yahoo_ohlcv_cache.py tests/test_yahoo_service_cache.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add app/services/yahoo_ohlcv_cache.py tests/test_yahoo_ohlcv_cache.py
git commit -m "refactor(yahoo): extract shared functions to ohlcv_cache_common"
```

---

## Task 5: `kis_ohlcv_cache.py` 리팩토링

**Files:**
- Modify: `app/services/kis_ohlcv_cache.py`
- Modify: `tests/test_kis_ohlcv_cache.py`
- Test: `tests/test_kis_ohlcv_cache.py`

- [ ] **Step 1: kis_ohlcv_cache.py에서 공통 함수를 import로 교체**

KIS는 공유 가능 함수가 적음 (4개만):
- `_to_json_value` → common에서 import
- `_acquire_lock` → common에서 import
- `_release_lock` → common에서 import
- `_enforce_retention_limit` → common에서 import

**import 블록 변경:**
```python
# 기존:
import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

# 변경 후:
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
```

`import uuid` 제거 (`_acquire_lock`이 common으로 이동), `import json` 유지 (KIS 고유 함수에서 사용)

```python
from app.services.ohlcv_cache_common import (
    _acquire_lock,
    _enforce_retention_limit,
    _release_lock,
    _to_json_value,
)
```

**삭제할 함수:**
- `_to_json_value` (4줄)
- `_acquire_lock` (14줄)
- `_release_lock` (12줄)
- `_enforce_retention_limit` (19줄)

총 ~49줄 삭제.

- [ ] **Step 2: 테스트 파일에서 FakeRedis를 공유 모듈로 교체**

`tests/test_kis_ohlcv_cache.py` 변경:
- `_FakePipeline`, `_FakeRedis` 클래스 삭제
- `from tests.ohlcv_cache_fakes import FakeRedis` 추가
- `_FakeRedis()` → `FakeRedis()` 교체

- [ ] **Step 3: kis 테스트 실행**

Run: `uv run pytest tests/test_kis_ohlcv_cache.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add app/services/kis_ohlcv_cache.py tests/test_kis_ohlcv_cache.py
git commit -m "refactor(kis): extract shared functions to ohlcv_cache_common"
```

---

## Task 6: 최종 검증

**Files:** (none modified)

- [ ] **Step 1: 전체 테스트 실행**

Run: `make test-unit`
Expected: ALL PASS (Task 0에서 기록한 테스트 수와 동일)

- [ ] **Step 2: 소비자 코드 import 검증**

```bash
uv run python -c "
from app.services import upbit_ohlcv_cache
from app.services import yahoo_ohlcv_cache
from app.services import kis_ohlcv_cache
print('upbit __all__:', upbit_ohlcv_cache.__all__)
print('yahoo __all__:', yahoo_ohlcv_cache.__all__)
print('kis __all__:', kis_ohlcv_cache.__all__)
print('All imports OK')
"
```
Expected: 모든 `__all__` 변경 없음, import 성공

- [ ] **Step 3: 줄 수 비교 확인**

```bash
wc -l app/services/ohlcv_cache_common.py app/services/upbit_ohlcv_cache.py app/services/yahoo_ohlcv_cache.py app/services/kis_ohlcv_cache.py
```
Expected: 총합 ~1,270줄 이하 (기존 2,037줄 대비 38%+ 감소)

- [ ] **Step 4: lint/format 검사**

Run: `make lint && make format`
Expected: PASS

- [ ] **Step 5: 최종 Commit**

```bash
git add -A
git commit -m "chore: verify ohlcv cache deduplication complete"
```
