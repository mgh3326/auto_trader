# Issue #366 Upbit Filled Days Filter Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `app/services/n8n_filled_orders_service.py`의 `_fetch_upbit_filled(days)`가 `days` 인자를 실제로 적용해서, 요청한 KST 기준 기간 안의 Upbit 체결만 반환하게 만든다.

**Architecture:** 구현은 service-local bugfix로 제한한다. Upbit closed orders를 기존처럼 먼저 normalize한 뒤, `filled_at`을 timezone-aware KST `datetime`으로 안전하게 파싱해서 `now_kst() - timedelta(days=days)` cutoff 이후 데이터만 남긴다. 파싱 실패 row는 전체 요청을 깨지 않도록 skip하고 warning log만 남긴다.

**Tech Stack:** Python 3.13+, asyncio, existing Upbit/KIS broker clients, pytest, unittest.mock, uv

---

## Verified Current State

- 이슈 본문대로 `app/services/n8n_filled_orders_service.py`의 `_fetch_upbit_filled(days)`는 `upbit_service.fetch_closed_orders(market=None, limit=100)`만 호출하고, normalize 이후 날짜 필터를 전혀 적용하지 않는다.
- 같은 파일의 KIS domestic / overseas 경로는 이미 `start_date=(now_kst() - timedelta(days=days)).strftime("%Y%m%d")` 방식으로 조회 범위를 제한하고 있어, Upbit만 동작이 불일치한다.
- Upbit normalize 결과의 `filled_at`은 현재 `created_at` 문자열 그대로 들어간다. 따라서 필터는 raw order가 아니라 normalized order 기준으로 거는 편이 KIS와 정렬/응답 계약을 그대로 유지하기 쉽다.
- 기존 테스트는 `tests/test_n8n_trade_review.py`의 `TestFilledOrdersService`에 모여 있으며, Upbit cancelled filtering / `min_amount` filtering만 검증하고 `days` 회귀는 아직 없다.
- 현재 코드베이스에는 이 서비스에서 바로 재사용 가능한 datetime helper가 없고, 유사 패턴은 `datetime.fromisoformat(text.replace("Z", "+00:00"))` 후 naive면 `KST`, aware면 `.astimezone(KST)`로 정규화하는 방식이다.

## Recommended Approach

이 버그는 작은 범위의 service-local fix라서 새로운 공용 utility를 만들지 않는 쪽이 낫다.

1. `_fetch_upbit_filled()`의 회귀 테스트를 먼저 추가한다.
2. `filled_at` parsing helper를 `n8n_filled_orders_service.py` 내부 private helper로 추가한다.
3. normalize된 Upbit orders에만 cutoff filtering을 적용한다.
4. malformed timestamp row는 skip + warning 처리한다.

이 접근이 가장 안전하다. 스키마, 라우터, KIS 경로는 건드리지 않고, 이슈가 지적한 동작 차이만 바로잡을 수 있다.

### Task 1: Upbit `days` 회귀 테스트 추가

**Files:**
- Modify: `tests/test_n8n_trade_review.py`

**Step 1: `_fetch_upbit_filled(days=1)` 직접 회귀 테스트 작성**

`TestFilledOrdersService`에 아래 두 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_fetch_upbit_filled_filters_orders_older_than_days(self):
    from datetime import datetime

    from app.core.timezone import KST
    from app.services.n8n_filled_orders_service import _fetch_upbit_filled

    mock_closed = [
        {
            "uuid": "recent-fill",
            "side": "bid",
            "price": "1000",
            "state": "done",
            "market": "KRW-XRP",
            "executed_volume": "5",
            "paid_fee": "2.5",
            "created_at": "2026-03-20T10:00:00+09:00",
        },
        {
            "uuid": "stale-fill",
            "side": "ask",
            "price": "1200",
            "state": "done",
            "market": "KRW-XRP",
            "executed_volume": "3",
            "paid_fee": "1.0",
            "created_at": "2026-03-18T09:00:00+09:00",
        },
    ]

    fixed_now = datetime(2026, 3, 21, 0, 0, 0, tzinfo=KST)

    with (
        patch(
            "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
            new_callable=AsyncMock,
            return_value=mock_closed,
        ),
        patch(
            "app.services.n8n_filled_orders_service.now_kst",
            return_value=fixed_now,
        ),
    ):
        orders, errors = await _fetch_upbit_filled(days=1)

    assert errors == []
    assert [order["order_id"] for order in orders] == ["recent-fill"]


@pytest.mark.asyncio
async def test_fetch_upbit_filled_skips_unparseable_filled_at(self, caplog):
    from datetime import datetime

    from app.core.timezone import KST
    from app.services.n8n_filled_orders_service import _fetch_upbit_filled

    mock_closed = [
        {
            "uuid": "bad-fill",
            "side": "bid",
            "price": "1000",
            "state": "done",
            "market": "KRW-XRP",
            "executed_volume": "1",
            "paid_fee": "0.5",
            "created_at": "not-a-datetime",
        }
    ]

    fixed_now = datetime(2026, 3, 21, 0, 0, 0, tzinfo=KST)

    with (
        patch(
            "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
            new_callable=AsyncMock,
            return_value=mock_closed,
        ),
        patch(
            "app.services.n8n_filled_orders_service.now_kst",
            return_value=fixed_now,
        ),
    ):
        orders, errors = await _fetch_upbit_filled(days=1)

    assert orders == []
    assert errors == []
    assert "Upbit filled order skipped due to invalid filled_at" in caplog.text
```

첫 번째 테스트는 이슈 핵심 회귀를 고정한다. 두 번째 테스트는 suggested fix의 “parse 실패 시 skip or log” 분기를 고정한다.

**Step 2: focused test를 RED로 확인**

Run:

```bash
uv run pytest --no-cov tests/test_n8n_trade_review.py -k "fetch_upbit_filled" -v
```

Expected:
- `test_fetch_upbit_filled_filters_orders_older_than_days` FAIL
- 현재 구현은 stale row도 그대로 반환하므로 assertion mismatch 발생

**Step 3: Commit**

```bash
git add tests/test_n8n_trade_review.py
git commit -m "test: cover upbit filled orders days filtering"
```

---

### Task 2: Upbit filled timestamp parse/filter helper 추가

**Files:**
- Modify: `app/services/n8n_filled_orders_service.py`

**Step 1: private datetime parsing helper 추가**

파일 상단 import에 `datetime`을 추가하고, `_normalize_upbit_filled()` 아래에 helper를 넣는다.

```python
from datetime import datetime, timedelta
```

```python
def _parse_filled_at_kst(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    else:
        parsed = parsed.astimezone(KST)

    return parsed
```

공용 util로 올리지 말고 이 파일 private helper로 둔다. 이슈 범위가 작고, 현재 재사용 지점이 이 서비스 하나뿐이기 때문이다.

**Step 2: `_fetch_upbit_filled(days)`에 cutoff filtering 추가**

기존 normalize 직후 아래 로직을 넣는다.

```python
cutoff = now_kst() - timedelta(days=days)
filtered_orders: list[dict[str, Any]] = []

for order in orders:
    parsed_filled_at = _parse_filled_at_kst(order.get("filled_at", ""))
    if parsed_filled_at is None:
        logger.warning(
            "Upbit filled order skipped due to invalid filled_at: order_id=%s filled_at=%r",
            order.get("order_id"),
            order.get("filled_at"),
        )
        continue
    if parsed_filled_at >= cutoff:
        filtered_orders.append(order)

return filtered_orders, []
```

주의:
- filtering은 raw order가 아니라 normalized order 기준으로 한다.
- `days <= 0` 특수 처리를 새로 넣지 않는다. 현재 API 계약상 기본은 positive integer이고, 이 이슈 범위는 무시된 `days` 적용 버그 수정이다.
- `filled_at` 문자열 자체는 응답 호환성을 위해 유지하고, 비교용으로만 parse한다.

**Step 3: focused test를 GREEN으로 확인**

Run:

```bash
uv run pytest --no-cov tests/test_n8n_trade_review.py -k "fetch_upbit_filled" -v
```

Expected:
- 두 신규 테스트 PASS
- 기존 `TestFilledOrdersService`의 다른 테스트는 아직 untouched

**Step 4: Commit**

```bash
git add app/services/n8n_filled_orders_service.py
git commit -m "fix: apply days filter to upbit filled orders"
```

---

### Task 3: 서비스 회귀 검증과 정렬/집계 영향 확인

**Files:**
- Verify only: `tests/test_n8n_trade_review.py`

**Step 1: 기존 filled-orders service 테스트 전체 실행**

Run:

```bash
uv run pytest --no-cov tests/test_n8n_trade_review.py -k "FilledOrdersService" -v
```

Expected:
- 기존 `test_returns_empty_when_no_orders`
- 기존 `test_filters_upbit_cancelled_orders`
- 기존 `test_min_amount_filter`
- 신규 `fetch_upbit_filled` 2건
- 전부 PASS

이 단계는 새 cutoff filtering이 cancelled filtering, min_amount filtering, enrich gating을 깨지 않았는지 확인한다.

**Step 2: 필요 시 broader n8n 회귀 1차 확인**

Run:

```bash
uv run pytest --no-cov tests/test_n8n_api.py tests/test_n8n_trade_review.py -k "filled or trade_review" -v
```

Expected:
- n8n API / trade review 관련 filled-order 소비 경로가 기존 응답 계약을 유지

브로드 회귀가 너무 무겁다면 최소한 `tests/test_n8n_trade_review.py` 전체는 반드시 실행한다.

**Step 3: lint 확인**

Run:

```bash
uv run ruff check app/services/n8n_filled_orders_service.py tests/test_n8n_trade_review.py
```

Expected:
- PASS

**Step 4: Commit**

```bash
git add app/services/n8n_filled_orders_service.py tests/test_n8n_trade_review.py
git commit -m "chore: verify upbit filled orders date-window fix"
```

---

## Risks and Review Notes

- Upbit `created_at` 포맷이 항상 ISO8601 aware string이라는 전제가 바뀌면 skip되는 row가 늘 수 있다. 그래서 parse failure는 silent drop이 아니라 warning log를 남겨야 한다.
- `fetch_closed_orders(..., limit=100)` 자체는 그대로라서 “최근 100건 중에서 days filter”라는 현재 상한은 유지된다. 이 이슈는 조회량 확대가 아니라 잘못된 기간 포함 버그 수정이다.
- sort는 여전히 `fetch_filled_orders()` 마지막의 `filled_at` string reverse 정렬에 의존한다. 현재 ISO8601 문자열이라면 안전하지만, 이후 format을 바꾸면 별도 정렬 이슈가 생길 수 있다. 이번 이슈에서는 정렬 로직을 건드리지 않는다.

## Out of Scope

- Upbit closed order pagination 추가
- KIS filtering semantics 재설계
- `filled_at`를 문자열이 아닌 `datetime`으로 응답 계약 변경
- 공용 datetime parsing utility 추출
