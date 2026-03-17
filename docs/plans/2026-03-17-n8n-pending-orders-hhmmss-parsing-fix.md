# n8n pending-orders KR HHMMSS 시간 파싱 버그 수정

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** KIS API가 `ordered_at`으로 날짜 없이 시간만(`HHMMSS`, 예: `"135334"`) 반환할 때 `_parse_created_at()`이 `ValueError`를 던지는 버그를 수정한다.

**Architecture:** `_parse_created_at()`의 KR/US 분기에서 6자리 `HHMMSS` 문자열을 감지하고, `fallback` 날짜(= `as_of` KST)의 날짜 부분을 결합하여 파싱한다. `fill_notification.py`의 `_parse_timestamp()`와 동일한 패턴이지만, UTC 대신 `fallback` 날짜를 사용하는 점이 다르다.

**Tech Stack:** Python 3.13, pytest, `datetime.strptime`

**References:**
- GitHub Issue: [#328](https://github.com/mgh3326/auto_trader/issues/328)
- Sentry Issue: [AUTO_TRADER-3E](https://mgh3326-daum.sentry.io/issues/7340414685/)
- 참조 구현: `app/services/fill_notification.py:_parse_timestamp()` (line 252-254)

---

## Root Cause

`app/services/n8n_pending_orders_service.py:_parse_created_at()` (line 43-65)

KIS domestic pending order API가 `ordered_at` 필드를 `" 135334"` 형태(앞 공백 + 시간만)로 반환하는 경우가 존재한다.

현재 파싱 흐름:
1. `strip()` → `"135334"`
2. `strptime("%Y%m%d %H%M%S")` → 실패 (15자 필요, 6자 입력)
3. `strptime("%Y%m%d%H%M%S")` → 실패 (14자 필요, 6자 입력)
4. `datetime.fromisoformat("135334")` → **ValueError** 💥

`fill_notification.py`의 `_parse_timestamp()`은 `len(text) == 6` 체크로 오늘 날짜를 prepend하여 정상 처리한다.

## Fix

`for fmt in (...)` 루프와 `fromisoformat` fallback 사이에 6자리 `HHMMSS` 감지 로직을 추가한다. `fallback.strftime("%Y%m%d")` + text를 `%Y%m%d%H%M%S`로 파싱.

---

### Task 1: Failing test — HHMMSS-only string

**Files:**
- Modify: `tests/test_n8n_api.py`

**Step 1: Write the failing test**

`TestN8nPendingOrdersService` 클래스 안에 추가 (기존 `test_created_at_kis_format_normalized_to_kst_iso` 바로 아래):

```python
@pytest.mark.asyncio
async def test_created_at_hhmmss_only_uses_fallback_date(self) -> None:
    """KIS sometimes returns only HHMMSS without date — must combine with as_of date."""
    from app.services.n8n_pending_orders_service import fetch_pending_orders

    as_of = datetime(2026, 3, 17, 15, 0, 0, tzinfo=KST)
    with patch(
        "app.services.n8n_pending_orders_service.get_order_history_impl",
        new_callable=AsyncMock,
        return_value=_impl_result(
            orders=[_make_kr_order(ordered_at="135334")],
            market="kr",
        ),
    ):
        result = await fetch_pending_orders(
            market="kr", include_current_price=False, as_of=as_of,
        )

    created = result["orders"][0]["created_at"]
    assert created == "2026-03-17T13:53:34+09:00"
```

이 테스트에 필요한 import는 파일 상단에서 확인:
- `datetime` — 이미 import 되어 있는지 확인, 없으면 `from datetime import datetime` 추가
- `KST` — `from app.core.timezone import KST` 추가 (없는 경우)

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_n8n_api.py::TestN8nPendingOrdersService::test_created_at_hhmmss_only_uses_fallback_date -xvs`

Expected: FAIL with `ValueError: Invalid isoformat string: '135334'`

**Step 3: Commit failing test**

```bash
git add tests/test_n8n_api.py
git commit -m "test: add failing test for HHMMSS-only timestamp parsing (#328)"
```

---

### Task 2: Failing test — HHMMSS with leading space

**Files:**
- Modify: `tests/test_n8n_api.py`

**Step 1: Write the failing test**

Sentry 이벤트의 실제 입력값(`" 135334"`, leading space)도 커버:

```python
@pytest.mark.asyncio
async def test_created_at_hhmmss_with_leading_space(self) -> None:
    """Sentry case: value=' 135334' with leading space before time."""
    from app.services.n8n_pending_orders_service import fetch_pending_orders

    as_of = datetime(2026, 3, 17, 15, 0, 0, tzinfo=KST)
    with patch(
        "app.services.n8n_pending_orders_service.get_order_history_impl",
        new_callable=AsyncMock,
        return_value=_impl_result(
            orders=[_make_kr_order(ordered_at=" 135334")],
            market="kr",
        ),
    ):
        result = await fetch_pending_orders(
            market="kr", include_current_price=False, as_of=as_of,
        )

    created = result["orders"][0]["created_at"]
    assert created == "2026-03-17T13:53:34+09:00"
```

**Step 2: Run to verify it also fails**

Run: `uv run pytest tests/test_n8n_api.py::TestN8nPendingOrdersService::test_created_at_hhmmss_with_leading_space -xvs`

Expected: FAIL with same `ValueError`

**Step 3: Commit**

```bash
git add tests/test_n8n_api.py
git commit -m "test: add failing test for HHMMSS with leading space (#328)"
```

---

### Task 3: Fix `_parse_created_at` — handle 6-digit HHMMSS

**Files:**
- Modify: `app/services/n8n_pending_orders_service.py:43-65`

**Step 1: Apply the fix**

`_parse_created_at` 함수를 다음과 같이 수정 — `for fmt` 루프와 `fromisoformat` fallback 사이에 HHMMSS 감지 로직 삽입:

```python
def _parse_created_at(value: str, market: str, fallback: datetime) -> datetime:
    text = str(value or "").strip()
    if not text:
        return fallback.replace(microsecond=0)

    if market == "crypto":
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST).replace(microsecond=0)

    for fmt in ("%Y%m%d %H%M%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=KST, microsecond=0)
        except ValueError:
            continue

    # KIS sometimes returns time-only HHMMSS (e.g. "135334") — combine with fallback date
    if text.isdigit() and len(text) == 6:
        date_prefix = fallback.astimezone(KST).strftime("%Y%m%d")
        return datetime.strptime(date_prefix + text, "%Y%m%d%H%M%S").replace(
            tzinfo=KST, microsecond=0
        )

    normalized = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST).replace(microsecond=0)
```

핵심 변경: line 60 `continue` 이후, `fromisoformat` fallback 이전에 4줄 추가.

**Step 2: Run both failing tests to verify they pass**

Run: `uv run pytest tests/test_n8n_api.py::TestN8nPendingOrdersService::test_created_at_hhmmss_only_uses_fallback_date tests/test_n8n_api.py::TestN8nPendingOrdersService::test_created_at_hhmmss_with_leading_space -xvs`

Expected: PASS (both)

**Step 3: Run full test class to verify no regression**

Run: `uv run pytest tests/test_n8n_api.py::TestN8nPendingOrdersService -xvs`

Expected: All existing tests PASS

**Step 4: Commit**

```bash
git add app/services/n8n_pending_orders_service.py
git commit -m "fix: handle KIS HHMMSS-only timestamp in pending orders

KIS domestic order API sometimes returns ordered_at as time-only
HHMMSS (e.g. '135334') without date prefix. Detect 6-digit strings
and combine with fallback (as_of) date before parsing.

Fixes #328
Fixes AUTO_TRADER-3E"
```

---

### Task 4: Lint and full test suite

**Step 1: Run linter**

Run: `make lint`

Expected: PASS (no new violations)

**Step 2: Run full test suite**

Run: `make test`

Expected: PASS

**Step 3: (Optional) Verify Sentry issue auto-close**

커밋 메시지에 `Fixes AUTO_TRADER-3E` 포함으로 merge 시 Sentry 이슈 자동 resolve.
