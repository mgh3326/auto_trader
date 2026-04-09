# Candles Sync Common Module Extraction

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract ~130 lines of structural duplication from `kr_candles_sync_service.py` and `us_candles_sync_service.py` into a shared `candles_sync_common.py` module.

**Architecture:** A new `candles_sync_common.py` provides: (1) `SyncTableConfig` dataclass that parameterizes table name and partition column, (2) SQL template builders that generate cursor/upsert SQL from config, (3) identical utility functions (`normalize_mode`, `parse_float`), (4) parameterized `build_symbol_union` with injectable field name and normalizer. Each service file replaces duplicated definitions with imports from common, keeping only market-specific logic.

**Tech Stack:** Python 3.13, SQLAlchemy (async), pytest, Ruff, ty

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| **Create** | `app/services/candles_sync_common.py` | Shared config, SQL builders, utility functions |
| **Create** | `tests/test_candles_sync_common.py` | Tests for all common functions |
| **Modify** | `app/services/kr_candles_sync_service.py` | Remove duplicated code, import from common |
| **Modify** | `app/services/us_candles_sync_service.py` | Remove duplicated code, import from common |
| **Modify** | `tests/test_kr_candles_sync.py` | Update `_build_symbol_union` test to use common |
| **Modify** | `tests/test_us_candles_sync.py` | Update `_build_symbol_union` test to use common |

### What stays in each service (NOT extracted)

| Function | Reason |
|----------|--------|
| `_normalize_symbol()` | KR zero-pads to 6 chars; US uses `to_db_symbol` — completely different logic |
| `_upsert_rows()` | KR sends all rows blindly; US pre-fetches existing and filters delta — fundamentally different |
| `MinuteCandleRow` | Field 4 differs (`venue` vs `exchange`), propagates into SQL payloads throughout |
| `VenueConfig` / `SessionWindow` | Structurally different dataclasses with different fields |
| `_collect_day_rows()` / `_collect_window_rows()` | Market-specific API call patterns and timezone handling |
| All market-specific orchestration | `sync_kr_candles`, `sync_us_candles`, venue/session planning, etc. |

---

## Task 1: Create common module with pure utilities

**Files:**
- Create: `app/services/candles_sync_common.py`
- Create: `tests/test_candles_sync_common.py`

- [ ] **Step 1: Write failing tests for `normalize_mode` and `parse_float`**

```python
# tests/test_candles_sync_common.py
from __future__ import annotations

import pytest


class TestNormalizeMode:
    def test_returns_incremental(self) -> None:
        from app.services.candles_sync_common import normalize_mode

        assert normalize_mode("incremental") == "incremental"

    def test_returns_backfill(self) -> None:
        from app.services.candles_sync_common import normalize_mode

        assert normalize_mode("BACKFILL") == "backfill"

    def test_strips_whitespace(self) -> None:
        from app.services.candles_sync_common import normalize_mode

        assert normalize_mode("  Incremental  ") == "incremental"

    def test_rejects_invalid(self) -> None:
        from app.services.candles_sync_common import normalize_mode

        with pytest.raises(ValueError, match="mode must be"):
            normalize_mode("unknown")

    def test_rejects_empty(self) -> None:
        from app.services.candles_sync_common import normalize_mode

        with pytest.raises(ValueError, match="mode must be"):
            normalize_mode("")


class TestParseFloat:
    def test_parses_string_number(self) -> None:
        from app.services.candles_sync_common import parse_float

        assert parse_float("3.14") == pytest.approx(3.14)

    def test_parses_int(self) -> None:
        from app.services.candles_sync_common import parse_float

        assert parse_float(42) == 42.0

    def test_returns_none_for_none(self) -> None:
        from app.services.candles_sync_common import parse_float

        assert parse_float(None) is None

    def test_returns_none_for_garbage(self) -> None:
        from app.services.candles_sync_common import parse_float

        assert parse_float("abc") is None

    def test_returns_none_for_empty_string(self) -> None:
        from app.services.candles_sync_common import parse_float

        assert parse_float("") is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_candles_sync_common.py -v -x
```

Expected: `ModuleNotFoundError: No module named 'app.services.candles_sync_common'`

- [ ] **Step 3: Create `candles_sync_common.py` with `SyncTableConfig`, `normalize_mode`, `parse_float`**

```python
# app/services/candles_sync_common.py
"""Shared utilities for candles sync services (KR / US).

kr_candles_sync_service, us_candles_sync_service 가 공유하는 함수 모음.
ohlcv_cache_common.py 와 동일한 패턴으로 사용.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class SyncTableConfig:
    """Market-specific table metadata for candle sync SQL generation."""

    table_name: str  # e.g. "kr_candles_1m", "us_candles_1m"
    partition_col: str  # e.g. "venue", "exchange"


def normalize_mode(mode: str) -> Literal["incremental", "backfill"]:
    normalized = str(mode or "").strip().lower()
    if normalized not in {"incremental", "backfill"}:
        raise ValueError("mode must be 'incremental' or 'backfill'")
    return cast(Literal["incremental", "backfill"], normalized)


def parse_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(str(value))
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_candles_sync_common.py -v -x
```

Expected: all 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/candles_sync_common.py tests/test_candles_sync_common.py
git commit -m "refactor: create candles_sync_common with normalize_mode and parse_float"
```

---

## Task 2: Add SQL template builders

**Files:**
- Modify: `app/services/candles_sync_common.py`
- Modify: `tests/test_candles_sync_common.py`

- [ ] **Step 1: Write failing tests for SQL builders**

Append to `tests/test_candles_sync_common.py`:

```python
from sqlalchemy import text as sa_text


class TestBuildCursorSql:
    def test_kr_cursor_sql_contains_table_and_partition(self) -> None:
        from app.services.candles_sync_common import SyncTableConfig, build_cursor_sql

        cfg = SyncTableConfig(table_name="kr_candles_1m", partition_col="venue")
        sql_text = build_cursor_sql(cfg).text

        assert "kr_candles_1m" in sql_text
        assert "venue = :venue" in sql_text
        assert "MAX(time)" in sql_text

    def test_us_cursor_sql_uses_exchange(self) -> None:
        from app.services.candles_sync_common import SyncTableConfig, build_cursor_sql

        cfg = SyncTableConfig(table_name="us_candles_1m", partition_col="exchange")
        sql_text = build_cursor_sql(cfg).text

        assert "us_candles_1m" in sql_text
        assert "exchange = :exchange" in sql_text


class TestBuildUpsertSql:
    def test_kr_upsert_sql_structure(self) -> None:
        from app.services.candles_sync_common import SyncTableConfig, build_upsert_sql

        cfg = SyncTableConfig(table_name="kr_candles_1m", partition_col="venue")
        sql_text = build_upsert_sql(cfg).text

        assert "INSERT INTO public.kr_candles_1m" in sql_text
        assert ":venue" in sql_text
        assert "ON CONFLICT (time, symbol, venue)" in sql_text
        assert "kr_candles_1m.open IS DISTINCT FROM EXCLUDED.open" in sql_text
        assert "kr_candles_1m.volume IS DISTINCT FROM EXCLUDED.volume" in sql_text

    def test_us_upsert_sql_structure(self) -> None:
        from app.services.candles_sync_common import SyncTableConfig, build_upsert_sql

        cfg = SyncTableConfig(table_name="us_candles_1m", partition_col="exchange")
        sql_text = build_upsert_sql(cfg).text

        assert "INSERT INTO public.us_candles_1m" in sql_text
        assert ":exchange" in sql_text
        assert "ON CONFLICT (time, symbol, exchange)" in sql_text
        assert "us_candles_1m.close IS DISTINCT FROM EXCLUDED.close" in sql_text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_candles_sync_common.py::TestBuildCursorSql -v -x
```

Expected: `ImportError: cannot import name 'build_cursor_sql'`

- [ ] **Step 3: Implement `build_cursor_sql` and `build_upsert_sql`**

Add to `app/services/candles_sync_common.py` after `parse_float`:

```python
def build_cursor_sql(cfg: SyncTableConfig) -> text:
    return text(f"""
    SELECT MAX(time)
    FROM public.{cfg.table_name}
    WHERE symbol = :symbol
      AND {cfg.partition_col} = :{cfg.partition_col}
    """)


def build_upsert_sql(cfg: SyncTableConfig) -> text:
    t = cfg.table_name
    p = cfg.partition_col
    return text(f"""
    INSERT INTO public.{t}
        (time, symbol, {p}, open, high, low, close, volume, value)
    VALUES
        (:time, :symbol, :{p}, :open, :high, :low, :close, :volume, :value)
    ON CONFLICT (time, symbol, {p})
    DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        value = EXCLUDED.value
    WHERE
        {t}.open IS DISTINCT FROM EXCLUDED.open
        OR {t}.high IS DISTINCT FROM EXCLUDED.high
        OR {t}.low IS DISTINCT FROM EXCLUDED.low
        OR {t}.close IS DISTINCT FROM EXCLUDED.close
        OR {t}.volume IS DISTINCT FROM EXCLUDED.volume
        OR {t}.value IS DISTINCT FROM EXCLUDED.value
    """)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_candles_sync_common.py -v -x
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/candles_sync_common.py tests/test_candles_sync_common.py
git commit -m "feat: add SQL template builders to candles_sync_common"
```

---

## Task 3: Add `build_symbol_union` and `read_cursor_utc`

**Files:**
- Modify: `app/services/candles_sync_common.py`
- Modify: `tests/test_candles_sync_common.py`

- [ ] **Step 1: Write failing tests for `build_symbol_union`**

Append to `tests/test_candles_sync_common.py`:

```python
from types import SimpleNamespace


class TestBuildSymbolUnion:
    @staticmethod
    def _identity_normalize(value: object) -> str | None:
        s = str(value or "").strip().upper()
        return s or None

    def test_combines_kis_and_manual(self) -> None:
        from app.services.candles_sync_common import build_symbol_union

        kis = [{"pdno": "005930"}, {"pdno": "035420"}]
        manual = [SimpleNamespace(ticker="000660")]

        result = build_symbol_union(
            kis, manual, holdings_field="pdno", normalize_fn=self._identity_normalize,
        )

        assert result == {"005930", "035420", "000660"}

    def test_skips_none_values(self) -> None:
        from app.services.candles_sync_common import build_symbol_union

        kis = [{"pdno": None}, {"pdno": ""}]
        manual = [SimpleNamespace(ticker=None)]

        result = build_symbol_union(
            kis, manual, holdings_field="pdno", normalize_fn=self._identity_normalize,
        )

        assert result == set()

    def test_uses_ovrs_pdno_for_us(self) -> None:
        from app.services.candles_sync_common import build_symbol_union

        kis = [{"ovrs_pdno": "AAPL"}, {"ovrs_pdno": "MSFT"}]
        manual = [SimpleNamespace(ticker="NVDA")]

        result = build_symbol_union(
            kis, manual, holdings_field="ovrs_pdno", normalize_fn=self._identity_normalize,
        )

        assert result == {"AAPL", "MSFT", "NVDA"}

    def test_handles_object_attrs(self) -> None:
        from app.services.candles_sync_common import build_symbol_union

        kis = [SimpleNamespace(pdno="005930")]
        manual = [SimpleNamespace(ticker="000660")]

        result = build_symbol_union(
            kis, manual, holdings_field="pdno", normalize_fn=self._identity_normalize,
        )

        assert result == {"005930", "000660"}

    def test_deduplicates(self) -> None:
        from app.services.candles_sync_common import build_symbol_union

        kis = [{"pdno": "005930"}]
        manual = [SimpleNamespace(ticker="005930")]

        result = build_symbol_union(
            kis, manual, holdings_field="pdno", normalize_fn=self._identity_normalize,
        )

        assert result == {"005930"}
```

- [ ] **Step 2: Write failing tests for `read_cursor_utc`**

Append to `tests/test_candles_sync_common.py`:

```python
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock


class TestReadCursorUtc:
    @pytest.mark.asyncio
    async def test_returns_datetime_when_present(self) -> None:
        from app.services.candles_sync_common import read_cursor_utc

        expected = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = expected

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        sql = MagicMock()
        result = await read_cursor_utc(mock_session, sql, {"symbol": "005930", "venue": "KRX"})

        assert result == expected
        mock_session.execute.assert_awaited_once_with(sql, {"symbol": "005930", "venue": "KRX"})

    @pytest.mark.asyncio
    async def test_returns_none_when_no_rows(self) -> None:
        from app.services.candles_sync_common import read_cursor_utc

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        result = await read_cursor_utc(mock_session, MagicMock(), {"symbol": "X"})

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_non_datetime(self) -> None:
        from app.services.candles_sync_common import read_cursor_utc

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "not-a-datetime"

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        result = await read_cursor_utc(mock_session, MagicMock(), {"symbol": "X"})

        assert result is None
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run pytest tests/test_candles_sync_common.py::TestBuildSymbolUnion -v -x
```

Expected: `ImportError: cannot import name 'build_symbol_union'`

- [ ] **Step 4: Implement `build_symbol_union` and `read_cursor_utc`**

Add to `app/services/candles_sync_common.py`:

```python
def build_symbol_union(
    kis_holdings: Sequence[object],
    manual_holdings: Sequence[object],
    *,
    holdings_field: str,
    normalize_fn: Callable[[object], str | None],
) -> set[str]:
    symbols: set[str] = set()

    for item in kis_holdings:
        raw = (
            cast(dict[str, object], item).get(holdings_field)
            if isinstance(item, dict)
            else getattr(item, holdings_field, None)
        )
        symbol = normalize_fn(raw)
        if symbol is not None:
            symbols.add(symbol)

    for holding in manual_holdings:
        symbol = normalize_fn(getattr(holding, "ticker", None))
        if symbol is not None:
            symbols.add(symbol)

    return symbols


async def read_cursor_utc(
    session: AsyncSession,
    cursor_sql: object,
    params: dict[str, object],
) -> datetime | None:
    result = await session.execute(cursor_sql, params)
    value = result.scalar_one_or_none()
    return value if isinstance(value, datetime) else None
```

- [ ] **Step 5: Run all common tests**

```bash
uv run pytest tests/test_candles_sync_common.py -v -x
```

Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/candles_sync_common.py tests/test_candles_sync_common.py
git commit -m "feat: add build_symbol_union and read_cursor_utc to candles_sync_common"
```

---

## Task 4: Migrate `kr_candles_sync_service.py`

**Files:**
- Modify: `app/services/kr_candles_sync_service.py:1-155,359-391`
- Modify: `tests/test_kr_candles_sync.py:28-43`

- [ ] **Step 1: Replace imports and constants**

In `app/services/kr_candles_sync_service.py`, add the common import after existing imports (line ~20):

```python
from app.services.candles_sync_common import (
    SyncTableConfig,
    build_cursor_sql,
    build_symbol_union,
    build_upsert_sql,
    normalize_mode,
    parse_float,
    read_cursor_utc,
)
```

Replace the SQL constants (lines 67-98) with:

```python
_TABLE_CFG = SyncTableConfig(table_name="kr_candles_1m", partition_col="venue")
_CURSOR_SQL = build_cursor_sql(_TABLE_CFG)
_UPSERT_SQL = build_upsert_sql(_TABLE_CFG)
```

- [ ] **Step 2: Remove duplicated function definitions**

Delete these functions from `kr_candles_sync_service.py`:

1. `_normalize_mode` (lines 106-110) — replaced by `normalize_mode` from common
2. `_parse_float` (lines 124-130) — replaced by `parse_float` from common
3. `_build_symbol_union` (lines 133-154) — replaced by `build_symbol_union` from common
4. `_read_cursor_utc` (lines 359-369) — replaced by `read_cursor_utc` from common

- [ ] **Step 3: Update all call sites in `kr_candles_sync_service.py`**

Replace every internal call with the common import. The key changes:

1. Where `_normalize_mode(mode)` is called → `normalize_mode(mode)` (drop underscore prefix)
2. Where `_parse_float(...)` is called → `parse_float(...)` (drop underscore prefix)
3. Where `_build_symbol_union(kis, manual)` is called → `build_symbol_union(kis, manual, holdings_field="pdno", normalize_fn=_normalize_symbol)`
4. Where `await _read_cursor_utc(session, symbol=..., venue=...)` is called → `await read_cursor_utc(session, _CURSOR_SQL, {"symbol": symbol, "venue": venue})`

Search for call sites:

```bash
rg "_normalize_mode\(|_parse_float\(|_build_symbol_union\(|_read_cursor_utc\(" app/services/kr_candles_sync_service.py
```

- [ ] **Step 4: Remove unused imports**

After removing `_normalize_mode`, `_parse_float`, check if `Literal`, `cast` are still used elsewhere in the file. If not, remove them from imports. (`select` from sqlalchemy may also be unused now — the file used it for `KRSymbolUniverse` queries, verify before removing.)

- [ ] **Step 5: Run existing KR tests**

```bash
uv run pytest tests/test_kr_candles_sync.py -v -x
```

Expected: all tests PASS except `test_build_symbol_union_combines_kis_and_manual_symbols` (the function is no longer at `svc._build_symbol_union`).

- [ ] **Step 6: Update KR `_build_symbol_union` test**

In `tests/test_kr_candles_sync.py`, update `test_build_symbol_union_combines_kis_and_manual_symbols` (line 28-43) to import from common and pass the KR-specific parameters:

```python
def test_build_symbol_union_combines_kis_and_manual_symbols() -> None:
    from app.services import kr_candles_sync_service as svc
    from app.services.candles_sync_common import build_symbol_union

    kis_holdings = [
        {"pdno": "5930"},
        {"pdno": "035420"},
        {"pdno": None},
    ]
    manual_holdings = [
        SimpleNamespace(ticker="005930"),
        SimpleNamespace(ticker="000660"),
    ]

    symbols = build_symbol_union(
        kis_holdings,
        manual_holdings,
        holdings_field="pdno",
        normalize_fn=svc._normalize_symbol,
    )

    assert symbols == {"005930", "035420", "000660"}
```

- [ ] **Step 7: Run all KR tests**

```bash
uv run pytest tests/test_kr_candles_sync.py -v -x
```

Expected: all tests PASS

- [ ] **Step 8: Commit**

```bash
git add app/services/kr_candles_sync_service.py tests/test_kr_candles_sync.py
git commit -m "refactor(kr): use candles_sync_common for shared utilities"
```

---

## Task 5: Migrate `us_candles_sync_service.py`

**Files:**
- Modify: `app/services/us_candles_sync_service.py:1-230,403-479`
- Modify: `tests/test_us_candles_sync.py:443-462`

- [ ] **Step 1: Replace imports and constants**

In `app/services/us_candles_sync_service.py`, add common import after existing imports (line ~28):

```python
from app.services.candles_sync_common import (
    SyncTableConfig,
    build_cursor_sql,
    build_symbol_union,
    build_upsert_sql,
    normalize_mode,
    parse_float,
    read_cursor_utc,
)
```

Replace the SQL constants (lines 64-106) with:

```python
_TABLE_CFG = SyncTableConfig(table_name="us_candles_1m", partition_col="exchange")
_CURSOR_SQL = build_cursor_sql(_TABLE_CFG)
_UPSERT_SQL = build_upsert_sql(_TABLE_CFG)

_EXISTING_ROWS_SQL = text(
    """
    SELECT time, open, high, low, close, volume, value
    FROM public.us_candles_1m
    WHERE symbol = :symbol
      AND exchange = :exchange
      AND time >= :start_time
      AND time <= :end_time
    """
)
```

Note: `_EXISTING_ROWS_SQL` stays in-place because it's US-only (KR doesn't pre-filter existing rows).

- [ ] **Step 2: Remove duplicated function definitions**

Delete these functions from `us_candles_sync_service.py`:

1. `_normalize_mode` (lines 163-167) — replaced by `normalize_mode` from common
2. `_parse_float` (lines 175-181) — replaced by `parse_float` from common
3. `_build_symbol_union` (lines 208-229) — replaced by `build_symbol_union` from common

For `_read_cursor_utc` (lines 403-413): replace the body with a delegation to common:

```python
async def _read_cursor_utc(
    session: AsyncSession,
    *,
    symbol: str,
    exchange: str,
) -> datetime | None:
    return await read_cursor_utc(
        session, _CURSOR_SQL, {"symbol": symbol, "exchange": exchange}
    )
```

Alternatively, if `_read_cursor_utc` is only called in 1-2 places, inline the `read_cursor_utc` call at those sites and delete the wrapper entirely. Check call sites first:

```bash
rg "_read_cursor_utc\(" app/services/us_candles_sync_service.py
```

- [ ] **Step 3: Update all call sites in `us_candles_sync_service.py`**

1. Where `_normalize_mode(mode)` is called → `normalize_mode(mode)`
2. Where `_parse_float(...)` is called → `parse_float(...)`
3. Where `_build_symbol_union(kis, manual)` is called → `build_symbol_union(kis, manual, holdings_field="ovrs_pdno", normalize_fn=_normalize_symbol)`

- [ ] **Step 4: Remove unused imports**

Check if `Literal`, `cast` (from typing) are still needed. `cast` is used in `_upsert_rows` (`cast(_RowcountResult, ...)`), so keep it. Check `Literal` — it may no longer be needed if `normalize_mode` was the only user.

- [ ] **Step 5: Run existing US tests**

```bash
uv run pytest tests/test_us_candles_sync.py -v -x
```

Expected: all tests PASS except `test_build_symbol_union_combines_kis_and_manual_us_symbols`.

- [ ] **Step 6: Update US `_build_symbol_union` test**

In `tests/test_us_candles_sync.py`, update `test_build_symbol_union_combines_kis_and_manual_us_symbols` (line 443-462):

```python
def test_build_symbol_union_combines_kis_and_manual_us_symbols() -> None:
    import app.services.us_candles_sync_service as svc
    from app.services.candles_sync_common import build_symbol_union

    kis_holdings = [
        {"ovrs_pdno": "aapl"},
        {"ovrs_pdno": "BRK/B"},
        {"ovrs_pdno": None},
    ]
    manual_holdings = [
        SimpleNamespace(ticker="BRK.B", market_type=MarketType.US),
        SimpleNamespace(ticker="msft", market_type=MarketType.US),
        SimpleNamespace(ticker="nvda", market_type=MarketType.US),
    ]

    assert build_symbol_union(
        kis_holdings,
        manual_holdings,
        holdings_field="ovrs_pdno",
        normalize_fn=svc._normalize_symbol,
    ) == {
        "AAPL",
        "BRK.B",
        "MSFT",
        "NVDA",
    }
```

- [ ] **Step 7: Run all US tests**

```bash
uv run pytest tests/test_us_candles_sync.py -v -x
```

Expected: all tests PASS

- [ ] **Step 8: Commit**

```bash
git add app/services/us_candles_sync_service.py tests/test_us_candles_sync.py
git commit -m "refactor(us): use candles_sync_common for shared utilities"
```

---

## Task 6: Final verification

**Files:** (none modified — verification only)

- [ ] **Step 1: Run full candle-related test suite**

```bash
uv run pytest tests/ -v -k "candle" --timeout=30 -x
```

Expected: all tests PASS

- [ ] **Step 2: Run lint**

```bash
make lint
```

Expected: no errors. Fix any unused import warnings.

- [ ] **Step 3: Run typecheck**

```bash
make typecheck
```

Expected: no new errors. The `text(f"...")` return type annotation in `build_cursor_sql`/`build_upsert_sql` should be `TextClause` (from `sqlalchemy`), not `text`. If ty complains, update the return type annotation to `TextClause`:

```python
from sqlalchemy import TextClause, text

def build_cursor_sql(cfg: SyncTableConfig) -> TextClause:
    ...
```

- [ ] **Step 4: Verify SQL hasn't changed**

Spot-check that the generated SQL matches the original by running in a Python REPL:

```bash
uv run python -c "
from app.services.candles_sync_common import SyncTableConfig, build_cursor_sql, build_upsert_sql
kr = SyncTableConfig('kr_candles_1m', 'venue')
us = SyncTableConfig('us_candles_1m', 'exchange')
print('=== KR CURSOR ===')
print(build_cursor_sql(kr).text)
print('=== KR UPSERT ===')
print(build_upsert_sql(kr).text)
print('=== US CURSOR ===')
print(build_cursor_sql(us).text)
print('=== US UPSERT ===')
print(build_upsert_sql(us).text)
"
```

Verify output matches the original SQL templates in both files (table names, column names, bind params, IS DISTINCT FROM clauses).

- [ ] **Step 5: Commit any fixes**

```bash
git add -A
git commit -m "chore: fix lint/type issues from candles_sync_common extraction"
```

(Only if Step 2 or 3 found issues.)

- [ ] **Step 6: Verify import paths are clean**

```bash
rg "from app.services.candles_sync_common import" app/ tests/
```

Expected: imports in 4 files:
- `app/services/kr_candles_sync_service.py`
- `app/services/us_candles_sync_service.py`
- `tests/test_candles_sync_common.py`
- `tests/test_kr_candles_sync.py`
- `tests/test_us_candles_sync.py`
