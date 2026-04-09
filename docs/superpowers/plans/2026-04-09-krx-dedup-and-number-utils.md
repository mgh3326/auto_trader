# KRX 리팩토링: 숫자 파싱 통합 + fetch 중복 제거 + SRP 분리

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `krx.py`의 중복 코드를 제거하고 `_parse_korean_number`를 공용 모듈로 통합하며, 무관한 클래스를 별도 파일로 분리한다.

**Architecture:** `_parse_korean_number`를 `app/core/number_utils.py`로 추출하여 `krx.py`와 `naver_finance.py` 양쪽에서 공유한다. `fetch_stock_all`/`fetch_etf_all`/`fetch_valuation_all`의 공통 날짜 폴백+캐시+API 호출 루프를 `_fetch_with_date_fallback` 헬퍼로 추출한다. `KRXMarketDataService`와 `Kospi200Service`를 각각 별도 파일로 분리한다.

**Tech Stack:** Python 3.13, pytest, pytest-asyncio, monkeypatch

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `app/core/number_utils.py` | 공용 `parse_korean_number()` 함수 |
| Create | `tests/test_number_utils.py` | number_utils 단위 테스트 |
| Create | `app/services/krx_market_data_service.py` | `KRXMarketDataService` 클래스 |
| Create | `app/services/kospi200_service.py` | `Kospi200Service` 클래스 |
| Modify | `app/services/naver_finance.py:99-199` | `_parse_korean_number` 삭제, number_utils에서 import |
| Modify | `app/services/krx.py:286-310` | `_parse_korean_number` 삭제, number_utils에서 import |
| Modify | `app/services/krx.py:456-877` | 3개 fetch 함수를 `_fetch_with_date_fallback` 사용으로 리팩토링 |
| Modify | `app/services/krx.py:964-1207` | `KRXMarketDataService`/`Kospi200Service` 제거 |
| Modify | `app/jobs/kospi200.py:4` | import 경로 업데이트 |
| Modify | `tests/test_naver_finance.py:58-124` | `TestParseKoreanNumber` 클래스 삭제 (test_number_utils.py로 이동) |
| Modify | `tests/test_services_krx.py` | 기존 테스트 전부 통과 확인 (변경 불필요 예상) |

---

### Task 1: `parse_korean_number` 공용 모듈 생성

**Files:**
- Create: `app/core/number_utils.py`
- Create: `tests/test_number_utils.py`

- [ ] **Step 1: Write failing tests for `parse_korean_number`**

`tests/test_number_utils.py` — `naver_finance.py`의 포괄적 버전 기준 + `krx.py` 버전의 `-` 처리 엣지 케이스 추가:

```python
"""Unit tests for app.core.number_utils."""

from __future__ import annotations

import pytest

from app.core.number_utils import parse_korean_number


class TestParseKoreanNumber:
    """Tests for parse_korean_number."""

    def test_simple_integer(self) -> None:
        assert parse_korean_number("1234") == 1234
        assert parse_korean_number("1,234") == 1234
        assert parse_korean_number("1,234,567") == 1234567

    def test_simple_float(self) -> None:
        assert parse_korean_number("12.34") == 12.34
        assert parse_korean_number("1,234.56") == 1234.56

    def test_percentage(self) -> None:
        result = parse_korean_number("5.67%")
        assert result is not None
        assert abs(result - 0.0567) < 0.0001

        result = parse_korean_number("100%")
        assert result is not None
        assert abs(result - 1.0) < 0.0001

    def test_korean_unit_jo(self) -> None:
        assert parse_korean_number("1조") == 1_0000_0000_0000
        assert parse_korean_number("2.5조") == 2_5000_0000_0000

    def test_korean_unit_eok(self) -> None:
        assert parse_korean_number("1억") == 1_0000_0000
        assert parse_korean_number("100억") == 100_0000_0000

    def test_korean_unit_man(self) -> None:
        assert parse_korean_number("1만") == 1_0000
        assert parse_korean_number("5만") == 5_0000

    def test_korean_units_combined(self) -> None:
        result = parse_korean_number("1조 2,345억")
        expected = 1_0000_0000_0000 + 2345 * 1_0000_0000
        assert result == expected

        result = parse_korean_number("400조 1,234억")
        expected = 400 * 1_0000_0000_0000 + 1234 * 1_0000_0000
        assert result == expected

    def test_negative_number_with_minus(self) -> None:
        assert parse_korean_number("-1,234") == -1234
        assert parse_korean_number("-5.67") == -5.67

    def test_negative_number_with_arrow(self) -> None:
        assert parse_korean_number("▼1,234") == -1234
        assert parse_korean_number("▼100") == -100

    def test_positive_number_with_arrow(self) -> None:
        assert parse_korean_number("▲1,234") == 1234

    def test_none_for_invalid(self) -> None:
        assert parse_korean_number("") is None
        assert parse_korean_number(None) is None
        assert parse_korean_number("N/A") is None
        assert parse_korean_number("--") is None

    def test_with_whitespace(self) -> None:
        assert parse_korean_number("  1,234  ") == 1234
        assert parse_korean_number("1 억") == 1_0000_0000

    # krx.py 버전에만 있던 엣지 케이스: 단일 "-"
    def test_single_dash_returns_none(self) -> None:
        assert parse_korean_number("-") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_number_utils.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.number_utils'`

- [ ] **Step 3: Implement `parse_korean_number` in `app/core/number_utils.py`**

`naver_finance.py:99-199`의 포괄적 버전을 기반으로 생성. 공개 API이므로 언더스코어 접두사 제거:

```python
"""Korean number format parsing utilities.

Shared parser for Korean number formats used across KRX, Naver Finance,
and other Korean financial data sources.
"""

from __future__ import annotations

import re


def parse_korean_number(value_str: str | None) -> int | float | None:
    """Parse Korean number formats.

    Handles formats like:
    - "1,234" → 1234
    - "5.67%" → 0.0567
    - "1조 2,345억" → 1,234,500,000,000
    - "▼1,234" or "-1,234" → -1234
    - "-" → None

    Args:
        value_str: Number string in Korean format

    Returns:
        Parsed number (int for whole numbers, float for decimals) or None
    """
    if not value_str:
        return None

    # Remove whitespace
    cleaned = value_str.strip()
    if not cleaned or cleaned == "-":
        return None

    # Handle percentage
    is_percent = "%" in cleaned
    cleaned = cleaned.replace("%", "")

    # Handle negative indicators
    is_negative = (
        cleaned.startswith("-")
        or "▼" in cleaned
        or "하락" in cleaned
        or cleaned.startswith("−")  # Unicode minus
    )
    cleaned = re.sub(r"[▲▼하락상승\-+−]", "", cleaned)

    # Remove commas and spaces
    cleaned = cleaned.replace(",", "").replace(" ", "")

    # Handle Korean units (조, 억, 만)
    # Process from largest to smallest
    total = 0.0
    remaining = cleaned

    # 조 (trillion in Korean, 10^12)
    if "조" in remaining:
        parts = remaining.split("조")
        try:
            jo_value = float(parts[0]) if parts[0] else 0
            total += jo_value * 1_0000_0000_0000
            remaining = parts[1] if len(parts) > 1 else ""
        except ValueError:
            pass

    # 억 (hundred million, 10^8)
    if "억" in remaining:
        parts = remaining.split("억")
        try:
            eok_value = float(parts[0]) if parts[0] else 0
            total += eok_value * 1_0000_0000
            remaining = parts[1] if len(parts) > 1 else ""
        except ValueError:
            pass

    # 만 (ten thousand, 10^4)
    if "만" in remaining:
        parts = remaining.split("만")
        try:
            man_value = float(parts[0]) if parts[0] else 0
            total += man_value * 1_0000
            remaining = parts[1] if len(parts) > 1 else ""
        except ValueError:
            pass

    # Add any remaining number
    if remaining:
        try:
            total += float(remaining)
        except ValueError:
            if total == 0:
                return None

    # If no Korean units were found, try parsing as plain number
    if total == 0 and not any(unit in value_str for unit in ["조", "억", "만"]):
        try:
            total = float(cleaned)
        except ValueError:
            return None

    # Apply percentage
    if is_percent:
        total = total / 100

    # Apply negative
    if is_negative:
        total = -abs(total)

    # Return int if whole number, float otherwise
    if total == int(total) and not is_percent:
        return int(total)
    return total
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_number_utils.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/number_utils.py tests/test_number_utils.py
git commit -m "feat: extract parse_korean_number to app/core/number_utils"
```

---

### Task 2: `naver_finance.py`와 `krx.py`에서 로컬 `_parse_korean_number` 제거

**Files:**
- Modify: `app/services/naver_finance.py:1-5, 99-199`
- Modify: `app/services/krx.py:1-25, 286-310`
- Modify: `tests/test_naver_finance.py:58-125`

- [ ] **Step 1: `naver_finance.py` — import 추가 + 로컬 함수 삭제**

`app/services/naver_finance.py` 상단에 import 추가:

```python
from app.core.number_utils import parse_korean_number as _parse_korean_number
```

그리고 99-199행의 로컬 `_parse_korean_number` 함수 정의를 **전부 삭제**.

`as _parse_korean_number`로 alias하여 파일 내 모든 호출부(`_parse_korean_number(...)`)가 변경 없이 동작하도록 한다.

- [ ] **Step 2: `krx.py` — import 추가 + 로컬 함수 삭제**

`app/services/krx.py` 상단에 import 추가:

```python
from app.core.number_utils import parse_korean_number as _parse_korean_number
```

그리고 286-310행의 로컬 `_parse_korean_number` 함수 정의를 **전부 삭제**.

- [ ] **Step 3: `test_naver_finance.py` — `TestParseKoreanNumber` 클래스 삭제**

`tests/test_naver_finance.py`에서 58-125행의 `TestParseKoreanNumber` 클래스를 **전부 삭제**. 이 테스트는 이미 `tests/test_number_utils.py`에 있으므로 중복이다.

- [ ] **Step 4: 기존 테스트 전부 실행하여 통과 확인**

Run: `uv run pytest tests/test_number_utils.py tests/test_naver_finance.py tests/test_services_krx.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/naver_finance.py app/services/krx.py tests/test_naver_finance.py
git commit -m "refactor: replace local _parse_korean_number with shared number_utils"
```

---

### Task 3: `_fetch_with_date_fallback` 공통 헬퍼 추출

**Files:**
- Modify: `app/services/krx.py` (새 함수 추가 + 3개 fetch 함수 리팩토링)
- Test: `tests/test_services_krx.py` (기존 테스트가 그대로 통과해야 함)

- [ ] **Step 1: Write failing test for `_fetch_with_date_fallback`**

`tests/test_services_krx.py` 하단에 추가:

```python
class TestFetchWithDateFallback:
    """Test the _fetch_with_date_fallback common helper."""

    @pytest.mark.asyncio
    async def test_returns_normalized_data_on_success(self, monkeypatch):
        """First date succeeds → returns normalized data."""

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return [{"RAW_FIELD": "value1"}]

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        def normalize(raw_data, actual_date):
            return [{"normalized": item["RAW_FIELD"], "date": actual_date} for item in raw_data]

        result = await krx._fetch_with_date_fallback(
            cache_prefix="test:prefix",
            bld="dbms/TEST/bld",
            extra_params=None,
            normalize_fn=normalize,
            trd_date="20250401",
        )

        assert len(result) == 1
        assert result[0]["normalized"] == "value1"
        assert result[0]["date"] == "20250401"

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached(self, monkeypatch):
        """Cache hit → returns cached data without calling API."""
        cached = [{"from": "cache"}]

        async def mock_get_cached_data(cache_key):
            return cached

        fetch_called = False

        async def mock_fetch_krx_data(**kwargs):
            nonlocal fetch_called
            fetch_called = True
            return []

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)

        result = await krx._fetch_with_date_fallback(
            cache_prefix="test:prefix",
            bld="dbms/TEST/bld",
            extra_params=None,
            normalize_fn=lambda raw, dt: raw,
            trd_date="20250401",
        )

        assert result == cached
        assert not fetch_called

    @pytest.mark.asyncio
    async def test_fallback_to_next_date_on_empty(self, monkeypatch):
        """Empty response → tries next date candidate."""
        call_dates = []

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            call_dates.append(kwargs.get("trdDd"))
            if len(call_dates) == 1:
                return []  # first date empty
            return [{"RAW": "ok"}]

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx._fetch_with_date_fallback(
            cache_prefix="test:prefix",
            bld="dbms/TEST/bld",
            extra_params=None,
            normalize_fn=lambda raw, dt: [{"done": True}],
            trd_date=None,  # auto-detect → multiple date candidates
        )

        assert len(call_dates) >= 2
        assert result == [{"done": True}]

    @pytest.mark.asyncio
    async def test_all_dates_exhausted_returns_empty(self, monkeypatch):
        """All dates return empty → returns []."""

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return []

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)

        result = await krx._fetch_with_date_fallback(
            cache_prefix="test:prefix",
            bld="dbms/TEST/bld",
            extra_params=None,
            normalize_fn=lambda raw, dt: raw,
            trd_date="20250101",
        )

        assert result == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services_krx.py::TestFetchWithDateFallback -v`
Expected: FAIL — `AttributeError: module 'app.services.krx' has no attribute '_fetch_with_date_fallback'`

- [ ] **Step 3: Implement `_fetch_with_date_fallback`**

`app/services/krx.py`에서 `_get_recent_trading_date` 함수 뒤(~454행)에 추가:

```python
async def _fetch_with_date_fallback(
    cache_prefix: str,
    bld: str,
    extra_params: dict[str, str] | None,
    normalize_fn: Callable[[list[dict[str, Any]], str], list[dict[str, Any]]],
    trd_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch KRX data with date fallback, caching, and normalization.

    Common pattern shared by fetch_stock_all, fetch_etf_all, fetch_valuation_all:
    1. Resolve date candidates (KRX resource bundle + weekday fallback)
    2. For each date: check cache → call API → normalize → cache → return
    3. On empty response: try next date

    Args:
        cache_prefix: Cache key prefix (e.g. "stock:all:STK")
        bld: KRX API bld parameter
        extra_params: Additional params for _fetch_krx_data (e.g. {"mktId": "STK"})
        normalize_fn: Function(raw_data, actual_date) → normalized list
        trd_date: Specific date in YYYYMMDD format, or None for auto-detect

    Returns:
        Normalized data list, or empty list if all dates exhausted
    """
    # Resolve date candidates
    if trd_date:
        date_candidates = [trd_date]
    else:
        fallback = _generate_date_candidates(None, KRX_MAX_RETRY_DATES)
        try:
            max_date = await _fetch_max_working_date()
            logger.info(f"KRX max working date: {max_date}")
            date_candidates = [max_date] + [d for d in fallback if d != max_date]
        except Exception as e:
            logger.warning(f"Failed to fetch max working date: {e}, using fallback")
            date_candidates = fallback

    for actual_date in date_candidates:
        cache_key = await _get_cache_key(cache_prefix, actual_date)

        # Try cache
        cached = await _get_cached_data(cache_key)
        if cached:
            logger.info(f"Cache hit for {cache_prefix} on {actual_date}")
            return cached

        # Fetch from KRX API
        logger.info(f"Fetching KRX data for {cache_prefix}, date={actual_date}")
        raw_data = await _fetch_krx_data(
            bld=bld, trdDd=actual_date, **(extra_params or {})
        )

        if raw_data:
            normalized = normalize_fn(raw_data, actual_date)
            await _set_cached_data(cache_key, normalized)
            return normalized
        else:
            logger.warning(
                f"Empty KRX response for {cache_prefix} on {actual_date}, trying next date"
            )
            continue

    logger.error(
        f"Failed to fetch {cache_prefix} data after trying {len(date_candidates)} dates"
    )
    return []
```

`from collections.abc import Callable`를 파일 상단 imports에 추가:

```python
from collections.abc import Callable
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_services_krx.py::TestFetchWithDateFallback -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/krx.py tests/test_services_krx.py
git commit -m "feat: add _fetch_with_date_fallback common helper to krx.py"
```

---

### Task 4: 3개 fetch 함수를 `_fetch_with_date_fallback` 사용으로 리팩토링

**Files:**
- Modify: `app/services/krx.py:456-877`
- Test: `tests/test_services_krx.py` (기존 테스트 전부 통과)

- [ ] **Step 1: `fetch_stock_all` 리팩토링**

기존 `fetch_stock_all` 함수 본문을 `_fetch_with_date_fallback` + normalize 함수로 교체:

```python
def _normalize_stock_row(item: dict[str, Any], actual_date: str) -> dict[str, Any] | None:
    """Normalize a single KRX stock row."""
    raw_market_cap = _parse_korean_number(item.get("MKTCAP"))
    market_cap_in_100m_won = (
        raw_market_cap / 1_0000_0000 if raw_market_cap is not None else None
    )

    close = _parse_korean_number(item.get("TDD_CLSPRC") or item.get("CLSPRC"))
    volume = _parse_korean_number(item.get("ACC_TRDVOL") or item.get("TRDVOL"))
    value = _parse_korean_number(item.get("ACC_TRDVAL") or item.get("TRDVAL"))

    change_rate = _parse_korean_number(item.get("FLUC_RT"))
    change_price = _parse_korean_number(item.get("CMPPREVDD_PRC"))
    if item.get("FLUC_TP_CD") == "2":
        if change_rate is not None:
            change_rate = -change_rate
        if change_price is not None:
            change_price = -change_price

    name = item.get("ISU_ABBRV", "").strip() or item.get("ISU_NM", "").strip()
    code = item.get("ISU_CD", "").strip()
    if not code or not name:
        return None

    return {
        "code": code,
        "short_code": item.get("ISU_SRT_CD", "").strip(),
        "abbreviation": item.get("ISU_ABBRV", "").strip(),
        "name": name,
        "market": item.get("MKT_NM", "").strip(),
        "date": actual_date,
        "close": close,
        "market_cap": market_cap_in_100m_won,
        "volume": volume,
        "value": value,
        "change_rate": change_rate,
        "change_price": change_price,
    }


async def fetch_stock_all(
    market: str = "STK",
    trd_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all stocks from KRX.

    Args:
        market: Market code - "STK" for KOSPI, "KSQ" for KOSDAQ
        trd_date: Trading date in YYYYMMDD format (None for auto-detect)

    Returns:
        List of stock dictionaries with keys:
        - code, short_code, abbreviation, name, market, date
        - close, market_cap (억원), volume, value, change_rate, change_price
    """

    def normalize(raw_data: list[dict[str, Any]], actual_date: str) -> list[dict[str, Any]]:
        stocks = []
        for item in raw_data:
            stock = _normalize_stock_row(item, actual_date)
            if stock is not None:
                stocks.append(stock)
        return stocks

    return await _fetch_with_date_fallback(
        cache_prefix=f"stock:all:{market}",
        bld="dbms/MDC/STAT/standard/MDCSTAT01501",
        extra_params={"mktId": market},
        normalize_fn=normalize,
        trd_date=trd_date,
    )
```

- [ ] **Step 2: Run stock tests**

Run: `uv run pytest tests/test_services_krx.py::TestKRXCaching tests/test_services_krx.py::TestKRXChangeRate -v`
Expected: All PASS

- [ ] **Step 3: `fetch_etf_all` 리팩토링**

```python
def _normalize_etf_row(item: dict[str, Any], actual_date: str) -> dict[str, Any] | None:
    """Normalize a single KRX ETF row."""
    raw_market_cap = _parse_korean_number(item.get("MKTCAP"))
    market_cap_in_100m_won = (
        raw_market_cap / 1_0000_0000 if raw_market_cap is not None else None
    )

    close = _parse_korean_number(item.get("TDD_CLSPRC") or item.get("CLSPRC"))
    volume = _parse_korean_number(item.get("ACC_TRDVOL") or item.get("TRDVOL"))
    value = _parse_korean_number(item.get("ACC_TRDVAL") or item.get("TRDVAL"))

    change_rate = _parse_korean_number(item.get("FLUC_RT"))
    change_price = _parse_korean_number(item.get("CMPPREVDD_PRC"))
    if item.get("FLUC_TP_CD") == "2":
        if change_rate is not None:
            change_rate = -change_rate
        if change_price is not None:
            change_price = -change_price

    name = item.get("ISU_ABBRV", "").strip() or item.get("ISU_NM", "").strip()
    code = item.get("ISU_CD", "").strip()
    if not code or not name:
        return None

    index_name = item.get("IDX_IND_NM", "").strip() or item.get("IDX_NM", "").strip()

    return {
        "code": code,
        "short_code": item.get("ISU_SRT_CD", "").strip(),
        "abbreviation": item.get("ISU_ABBRV", "").strip(),
        "name": name,
        "index_name": index_name,
        "index_class_code": item.get("IDX_IND_CLSS_CD", "").strip(),
        "index_class_name": item.get("IDX_IND_CLSS_NM", "").strip(),
        "date": actual_date,
        "close": close,
        "market_cap": market_cap_in_100m_won,
        "volume": volume,
        "value": value,
        "change_rate": change_rate,
        "change_price": change_price,
    }


async def fetch_etf_all(
    trd_date: str | None = None,
    idx_ind_clss_cd: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all ETFs from KRX.

    Args:
        trd_date: Trading date in YYYYMMDD format (None for auto-detect)
        idx_ind_clss_cd: Index classification code for category filtering

    Returns:
        List of ETF dictionaries with keys:
        - code, short_code, abbreviation, name, index_name
        - index_class_code, index_class_name, date
        - close, market_cap (억원), volume, value, change_rate, change_price
    """
    cache_suffix = "etf:all"
    if idx_ind_clss_cd:
        cache_suffix += f":{idx_ind_clss_cd}"

    def normalize(raw_data: list[dict[str, Any]], actual_date: str) -> list[dict[str, Any]]:
        etfs = []
        for item in raw_data:
            etf = _normalize_etf_row(item, actual_date)
            if etf is not None:
                etfs.append(etf)
        return etfs

    return await _fetch_with_date_fallback(
        cache_prefix=cache_suffix,
        bld="dbms/MDC/STAT/standard/MDCSTAT04301",
        extra_params=None,
        normalize_fn=normalize,
        trd_date=trd_date,
    )
```

- [ ] **Step 4: Run ETF tests**

Run: `uv run pytest tests/test_services_krx.py::TestKRXETFCaching -v`
Expected: All PASS

- [ ] **Step 5: `fetch_valuation_all` 리팩토링**

Valuation은 `dict[str, dict]`를 반환하므로, 내부에서 `_fetch_with_date_fallback`로 `list[dict]`를 얻은 뒤 dict로 변환한다:

```python
def _normalize_valuation_row(item: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Normalize a single KRX valuation row. Returns (code, valuation_dict) or None."""
    code = item.get("ISU_SRT_CD", "").strip()
    if not code:
        return None

    per = _parse_korean_number(item.get("PER"))
    pbr = _parse_korean_number(item.get("PBR"))
    eps = _parse_korean_number(item.get("EPS"))
    bps = _parse_korean_number(item.get("BPS"))
    dividend_yield_raw = _parse_korean_number(item.get("DVD_YLD"))

    dividend_yield = (
        dividend_yield_raw / 100.0 if dividend_yield_raw is not None else None
    )

    # Set PER/PBR to None for 0 values
    per = None if per == 0 else per
    pbr = None if pbr == 0 else pbr

    return code, {
        "ISU_SRT_CD": code,
        "per": per,
        "pbr": pbr,
        "eps": eps,
        "bps": bps,
        "dividend_yield": dividend_yield,
    }


async def fetch_valuation_all(
    market: str = "ALL",
    trd_date: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch PER/PBR/dividend yield data from KRX for all stocks.

    Args:
        market: Market code - "STK" for KOSPI, "KSQ" for KOSDAQ, "ALL" for both
        trd_date: Trading date in YYYYMMDD format (None for auto-detect)

    Returns:
        Dictionary keyed by ISU_SRT_CD (6-digit short code) with values:
        - per, pbr, eps, bps, dividend_yield (decimal, 0.0256 = 2.56%)
    """

    def normalize(raw_data: list[dict[str, Any]], actual_date: str) -> list[dict[str, Any]]:
        results = []
        for item in raw_data:
            parsed = _normalize_valuation_row(item)
            if parsed is not None:
                _, val_dict = parsed
                results.append(val_dict)
        return results

    cached_list = await _fetch_with_date_fallback(
        cache_prefix=f"valuation:{market}",
        bld="dbms/MDC/STAT/standard/MDCSTAT03501",
        extra_params={"mktId": market},
        normalize_fn=normalize,
        trd_date=trd_date,
    )

    # Convert list → dict keyed by ISU_SRT_CD
    return {
        item["ISU_SRT_CD"]: item
        for item in cached_list
        if item.get("ISU_SRT_CD")
    }
```

- [ ] **Step 6: Run all KRX tests**

Run: `uv run pytest tests/test_services_krx.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add app/services/krx.py
git commit -m "refactor: deduplicate fetch_stock/etf/valuation_all via _fetch_with_date_fallback"
```

---

### Task 5: SRP 분리 — `KRXMarketDataService`와 `Kospi200Service`

**Files:**
- Create: `app/services/krx_market_data_service.py`
- Create: `app/services/kospi200_service.py`
- Modify: `app/services/krx.py:964-1207` (클래스 삭제)
- Modify: `app/jobs/kospi200.py:4` (import 경로 변경)

- [ ] **Step 1: `app/services/krx_market_data_service.py` 생성**

`krx.py:964-1066`의 `KRXMarketDataService`를 그대로 이동:

```python
"""KRX Market Data Service for fetching KOSPI200 constituents via CSV download."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class KRXMarketDataService:
    """KRX 마켓 데이터 서비스"""

    KRX_DOWNLOAD_URL = "https://data.krx.co.kr/comm/fileDn/DownloadOfFileService"

    async def fetch_kospi200_constituents(self) -> list[dict[str, Any]]:
        """KRX에서 KOSPI200 구성종목 데이터를 가져옵니다.

        Returns:
            List[Dict]: 종목 정보 목록
            {
                "종목코드": "005930",
                "종목명": "삼성전자",
                "시가총액": 1234567890,
                "지수비중": 1.23,
                "섹터": "전기전자"
            }
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            params = {
                "mktId": "STK",
                "trdDd": __import__("datetime").datetime.now().strftime("%Y%m%d"),
                "share": "1",
                "money": "1",
                "csvxls_isNo": "false",
            }

            try:
                response = await client.post(
                    self.KRX_DOWNLOAD_URL,
                    data=params,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": "Mozilla/5.0",
                    },
                )

                if response.status_code == 200:
                    content = response.text
                    return self._parse_krx_csv_content(content)
                else:
                    logger.error(
                        "KRX API 호출 실패: status_code=%d", response.status_code
                    )
                    return []

            except Exception as e:
                logger.error("KRX 데이터 수집 중 오류 발생: %s", e)
                return []

    def _parse_krx_csv_content(self, content: str) -> list[dict[str, Any]]:
        """KRX에서 반환된 CSV 형식의 데이터를 파싱합니다."""
        if not content or len(content) < 100:
            logger.warning("KRX 응답 데이터가 비어있거나 너무 짧습니다")
            return []

        lines = content.split("\n")
        if len(lines) < 2:
            return []

        headers = lines[0].split("\t")
        constituents = []

        for line in lines[1:]:
            if not line.strip():
                continue

            values = line.split("\t")
            if len(values) < len(headers):
                continue

            row = dict(zip(headers, values, strict=False))

            stock_code = row.get("종목코드", "")
            if stock_code.startswith("KR7"):
                stock_code = stock_code[4:]

            market_cap_str = row.get("시가총액", "0").replace(",", "")
            try:
                market_cap = float(market_cap_str) if market_cap_str else 0.0
            except ValueError:
                market_cap = 0.0

            weight_str = row.get("지수비중", "0").replace(",", "")
            try:
                weight = float(weight_str) if weight_str else 0.0
            except ValueError:
                weight = 0.0

            constituents.append(
                {
                    "stock_code": stock_code,
                    "stock_name": row.get("종목명", ""),
                    "market_cap": market_cap,
                    "weight": weight,
                    "sector": row.get("섹터", ""),
                }
            )

        return constituents
```

**주의:** `datetime` import를 정식으로 상단에서 한다. 원본의 `datetime.now()` 인라인 import를 정리:

```python
from datetime import datetime
```

그리고 `fetch_kospi200_constituents` 안의 `trdDd` 값을:

```python
"trdDd": datetime.now().strftime("%Y%m%d"),
```

- [ ] **Step 2: `app/services/kospi200_service.py` 생성**

`krx.py:1069-1207`의 `Kospi200Service`를 이동:

```python
"""KOSPI200 constituent management service."""

from __future__ import annotations

import logging
from typing import Any

from app.models.kospi200 import Kospi200Constituent

logger = logging.getLogger(__name__)


class Kospi200Service:
    """KOSPI200 구성종목 관리 서비스"""

    def __init__(self, db_session):
        self.db = db_session

    async def get_all_constituents(
        self, active_only: bool = True
    ) -> list[Kospi200Constituent]:
        """KOSPI200 구성종목 목록 조회"""
        from sqlalchemy import select

        query = select(Kospi200Constituent)

        if active_only:
            query = query.where(Kospi200Constituent.is_active == True)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_constituent_by_code(
        self, stock_code: str
    ) -> Kospi200Constituent | None:
        """종목코드로 구성종목 조회"""
        from sqlalchemy import select

        query = select(Kospi200Constituent).where(
            Kospi200Constituent.stock_code == stock_code
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def update_constituents(
        self, constituents_data: list[dict[str, Any]]
    ) -> dict[str, int]:
        """KOSPI200 구성종목 정보를 업데이트합니다.

        Args:
            constituents_data: KRX에서 가져온 종목 데이터 목록

        Returns:
            Dict: 업데이트 결과 통계
            {
                "added": 10,
                "updated": 180,
                "removed": 5
            }
        """
        from datetime import datetime as dt

        from sqlalchemy import select, update

        added = 0
        updated = 0
        removed = 0

        now = dt.now()

        existing_codes_query = select(Kospi200Constituent.stock_code).where(
            Kospi200Constituent.is_active == True
        )
        existing_codes_result = await self.db.execute(existing_codes_query)
        existing_codes = {row[0] for row in existing_codes_result.fetchall()}

        new_codes = set()

        for data in constituents_data:
            stock_code = data["stock_code"]
            new_codes.add(stock_code)

            existing = await self.get_constituent_by_code(stock_code)

            if existing:
                if existing.is_active is True:
                    await self.db.execute(
                        update(Kospi200Constituent)
                        .where(Kospi200Constituent.id == existing.id)
                        .values(
                            stock_name=data["stock_name"],
                            market_cap=data["market_cap"],
                            weight=data["weight"],
                            sector=data["sector"],
                            updated_at=now,
                        )
                    )
                    updated += 1
                else:
                    await self.db.execute(
                        update(Kospi200Constituent)
                        .where(Kospi200Constituent.id == existing.id)
                        .values(
                            stock_name=data["stock_name"],
                            market_cap=data["market_cap"],
                            weight=data["weight"],
                            sector=data["sector"],
                            is_active=True,
                            removed_at=None,
                            added_at=now,
                            updated_at=now,
                        )
                    )
                    added += 1
            else:
                new_constituent = Kospi200Constituent(
                    stock_code=stock_code,
                    stock_name=data["stock_name"],
                    market_cap=data["market_cap"],
                    weight=data["weight"],
                    sector=data["sector"],
                    is_active=True,
                    added_at=now,
                )
                self.db.add(new_constituent)
                added += 1

        removed_codes = existing_codes - new_codes
        if removed_codes:
            await self.db.execute(
                update(Kospi200Constituent)
                .where(Kospi200Constituent.stock_code.in_(removed_codes))
                .values(is_active=False, removed_at=now, updated_at=now)
            )
            removed = len(removed_codes)

        await self.db.commit()

        logger.info(
            "KOSPI200 구성종목 업데이트 완료: 추가=%d, 업데이트=%d, 제외=%d",
            added,
            updated,
            removed,
        )

        return {"added": added, "updated": updated, "removed": removed}
```

- [ ] **Step 3: `krx.py`에서 두 클래스 삭제 + import 정리**

`app/services/krx.py`에서:
1. 964-1207행의 `KRXMarketDataService` 클래스와 `Kospi200Service` 클래스를 **전부 삭제**
2. 상단의 `from app.models.kospi200 import Kospi200Constituent` import를 **삭제** (더 이상 krx.py에서 사용하지 않음)

- [ ] **Step 4: `app/jobs/kospi200.py` import 경로 업데이트**

변경 전:
```python
from app.services.krx import Kospi200Service, KRXMarketDataService
```

변경 후:
```python
from app.services.kospi200_service import Kospi200Service
from app.services.krx_market_data_service import KRXMarketDataService
```

- [ ] **Step 5: 전체 테스트 실행**

Run: `uv run pytest tests/test_services_krx.py tests/test_naver_finance.py tests/test_number_utils.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/krx_market_data_service.py app/services/kospi200_service.py app/services/krx.py app/jobs/kospi200.py
git commit -m "refactor: extract KRXMarketDataService and Kospi200Service to separate files"
```

---

### Task 6: 최종 검증

**Files:** 없음 (검증만)

- [ ] **Step 1: lint 통과**

Run: `make lint`
Expected: 0 errors

- [ ] **Step 2: typecheck 통과**

Run: `make typecheck`
Expected: 0 errors

- [ ] **Step 3: 전체 관련 테스트 실행**

Run: `uv run pytest tests/test_number_utils.py tests/test_services_krx.py tests/test_naver_finance.py -v --timeout=10 -x`
Expected: All PASS

- [ ] **Step 4: import 경로 깨진 곳 없는지 확인**

```bash
grep -r "from app.services.krx import" app/ tests/
```

Expected: `KRXMarketDataService`와 `Kospi200Service`가 더 이상 `krx.py`에서 import되지 않음. 나머지 import는 유효한 함수/클래스만 참조.

```bash
grep -r "from app.services.naver_finance import.*_parse_korean" app/ tests/
```

Expected: 0 matches (naver_finance에서 직접 _parse_korean_number를 import하는 외부 코드 없음)

- [ ] **Step 5: Commit (필요 시 lint/format 수정)**

```bash
make format
git add -A
git commit -m "chore: lint/format fixes after krx refactoring"
```
