# KR 미체결 주문 종목명 Enrichment 구현 플랜

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `/api/n8n/pending-orders`에서 KR 미체결 주문에 종목명(`name`)을 추가하고, `summary_line`에 종목명을 포함시켜 가독성을 높인다.

**Architecture:** `kr_symbol_universe` 테이블에서 symbol→name 배치 조회 함수를 새로 만들고, pending orders 서비스의 정규화 이후 단계에서 KR 주문에 종목명을 주입한다. `build_summary_line`은 `name`이 있으면 `현대로템(064350)` 형태로 출력하도록 수정한다.

**Tech Stack:** Python 3.13+, SQLAlchemy async, Pydantic v2, pytest

---

## 변경 범위 요약

| 파일 | 변경 내용 |
|------|-----------|
| `app/services/kr_symbol_universe_service.py` | `get_kr_names_by_symbols()` 배치 조회 함수 추가 |
| `app/schemas/n8n.py` | `N8nPendingOrderItem`에 `name` 필드 추가 |
| `app/services/n8n_pending_orders_service.py` | KR 주문 종목명 enrichment 로직 추가 |
| `app/services/n8n_formatting.py` | `build_summary_line`에 name 반영 |
| `tests/test_n8n_api.py` | 기존 테스트 업데이트 + name enrichment 테스트 추가 |
| `tests/test_kr_symbol_universe_service.py` (있으면) | `get_kr_names_by_symbols` 단위 테스트 |

---

### Task 1: `get_kr_names_by_symbols` 배치 조회 함수 추가

**Files:**
- Modify: `app/services/kr_symbol_universe_service.py`
- Test: `tests/test_kr_symbol_universe_service.py` (없으면 새 파일 또는 기존 테스트 파일에 추가)

**Step 1: 테스트 작성**

`kr_symbol_universe_service`에 새 함수의 단위 테스트를 작성한다. 기존 테스트 파일이 있으면 거기에, 없으면 인라인으로 작성.

```python
@pytest.mark.unit
async def test_get_kr_names_by_symbols_returns_name_map(mocker):
    """symbol 목록으로 name 매핑을 반환한다."""
    from app.services.kr_symbol_universe_service import get_kr_names_by_symbols

    mock_row_1 = mocker.MagicMock(symbol="064350", name="현대로템")
    mock_row_2 = mocker.MagicMock(symbol="035420", name="NAVER")
    mock_scalars = mocker.MagicMock()
    mock_scalars.all.return_value = [mock_row_1, mock_row_2]
    mock_result = mocker.MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_session = mocker.AsyncMock()
    mock_session.execute.return_value = mock_result

    result = await get_kr_names_by_symbols(
        ["064350", "035420", "999999"],
        db=mock_session,
    )

    assert result == {"064350": "현대로템", "035420": "NAVER"}
    assert "999999" not in result


@pytest.mark.unit
async def test_get_kr_names_by_symbols_empty_input(mocker):
    """빈 목록이면 DB 호출 없이 빈 dict 반환."""
    from app.services.kr_symbol_universe_service import get_kr_names_by_symbols

    result = await get_kr_names_by_symbols([], db=mocker.AsyncMock())
    assert result == {}
```

**Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/ -k "test_get_kr_names_by_symbols" -v`
Expected: FAIL — `get_kr_names_by_symbols` 없음

**Step 3: 구현**

`app/services/kr_symbol_universe_service.py` 끝에 함수 추가:

```python
async def _get_kr_names_impl(
    db: AsyncSession,
    symbols: list[str],
) -> dict[str, str]:
    if not symbols:
        return {}
    unique = sorted(set(symbols))
    stmt = select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
        KRSymbolUniverse.symbol.in_(unique),
        KRSymbolUniverse.is_active.is_(True),
    )
    rows = (await db.execute(stmt)).all()
    return {row.symbol: row.name.strip() for row in rows}


async def get_kr_names_by_symbols(
    symbols: list[str],
    db: AsyncSession | None = None,
) -> dict[str, str]:
    """Return {symbol: name} for the given KR symbol codes.

    Missing or inactive symbols are silently omitted from the result.
    """
    if not symbols:
        return {}
    if db is not None:
        return await _get_kr_names_impl(db, symbols)
    async with AsyncSessionLocal() as session:
        return await _get_kr_names_impl(session, symbols)
```

`__all__`에 `"get_kr_names_by_symbols"` 추가.

**Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/ -k "test_get_kr_names_by_symbols" -v`
Expected: PASS

**Step 5: 커밋**

```bash
git add app/services/kr_symbol_universe_service.py tests/
git commit -m "feat(kr-universe): add batch symbol-to-name lookup for pending order enrichment"
```

---

### Task 2: `N8nPendingOrderItem` 스키마에 `name` 필드 추가

**Files:**
- Modify: `app/schemas/n8n.py:8-89`

**Step 1: `name` 필드 추가**

`N8nPendingOrderItem` 클래스의 `symbol` 필드 바로 다음에 추가:

```python
name: str | None = Field(
    None,
    description="Human-readable name (e.g. 현대로템 for KR, None for crypto)",
)
```

**Step 2: example 업데이트**

`json_schema_extra["example"]`에 `"name": None` 추가 (`"symbol": "BTC"` 다음).

**Step 3: 관련 스키마 확인**

`N8nPendingReviewItem`에도 동일하게 `name` 필드 추가 (일관성):

```python
name: str | None = Field(None)
```

`N8nDailyBriefPendingMarket.orders`는 `list[N8nPendingOrderItem]`를 사용하므로 자동 반영됨.

**Step 4: lint 확인**

Run: `uv run ruff check app/schemas/n8n.py`
Expected: 통과

**Step 5: 커밋**

```bash
git add app/schemas/n8n.py
git commit -m "feat(schema): add name field to N8nPendingOrderItem and N8nPendingReviewItem"
```

---

### Task 3: `build_summary_line`에 종목명 반영

**Files:**
- Modify: `app/services/n8n_formatting.py:76-90`
- Test: `tests/test_n8n_api.py` (기존 formatting 테스트가 있으면 해당 파일)

**Step 1: 테스트 작성**

```python
@pytest.mark.unit
def test_build_summary_line_with_name():
    from app.services.n8n_formatting import build_summary_line

    order = {
        "symbol": "064350",
        "name": "현대로템",
        "side": "buy",
        "order_price": 188000,
        "current_price": 175000,
        "gap_pct": -6.9,
        "amount_krw": 188000,
        "age_hours": 48,
        "currency": "KRW",
    }
    result = build_summary_line(order)
    assert result.startswith("현대로템(064350)")
    assert "buy" in result
    assert "@18.8만" in result


@pytest.mark.unit
def test_build_summary_line_without_name():
    """name이 None이면 기존처럼 symbol만 표시."""
    from app.services.n8n_formatting import build_summary_line

    order = {
        "symbol": "BTC",
        "name": None,
        "side": "buy",
        "order_price": 148500000,
        "current_price": 149200000,
        "gap_pct": 0.47,
        "amount_krw": 297000,
        "age_hours": 6,
        "currency": "KRW",
    }
    result = build_summary_line(order)
    assert result.startswith("BTC buy")
```

**Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/ -k "test_build_summary_line_with_name or test_build_summary_line_without_name" -v`
Expected: FAIL (name 없으면 기존 코드 동작이라 without_name은 PASS할 수 있음, with_name은 FAIL)

**Step 3: 구현**

`app/services/n8n_formatting.py`의 `build_summary_line` 수정:

```python
def build_summary_line(order: dict[str, Any]) -> str:
    """Build a one-line order summary.

    Format with name: "현대로템(064350) buy @18.8만 (현재 17.5만, -6.9%, 18.8만, 2일)"
    Format without name: "BTC buy @1.49억 (현재 1.49억, +0.5%, 29.7만, 6시간)"
    """
    currency = str(order.get("currency") or "KRW")
    symbol = str(order.get("symbol") or "")
    name = order.get("name")
    side = str(order.get("side") or "")
    price_str = fmt_price(order.get("order_price"), currency)
    current_str = fmt_price(order.get("current_price"), currency)
    gap_str = fmt_gap(order.get("gap_pct"))
    amount_str = fmt_amount(order.get("amount_krw"))
    age_str = fmt_age(int(order.get("age_hours") or 0))

    display_symbol = f"{name}({symbol})" if name else symbol

    return f"{display_symbol} {side} @{price_str} (현재 {current_str}, {gap_str}, {amount_str}, {age_str})"
```

**Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/ -k "test_build_summary_line" -v`
Expected: PASS

**Step 5: 커밋**

```bash
git add app/services/n8n_formatting.py tests/
git commit -m "feat(formatting): include stock name in summary_line when available"
```

---

### Task 4: `fetch_pending_orders`에 KR 종목명 enrichment 추가

**Files:**
- Modify: `app/services/n8n_pending_orders_service.py`
- Test: `tests/test_n8n_api.py`

**Step 1: 테스트 작성**

기존 pending orders 테스트 패턴을 따라 KR 주문에 name이 포함되는지 검증:

```python
@pytest.mark.unit
async def test_fetch_pending_orders_kr_name_enrichment(mocker):
    """KR 미체결 주문에 종목명이 enrichment된다."""
    from app.services.n8n_pending_orders_service import fetch_pending_orders

    mocker.patch(
        "app.services.n8n_pending_orders_service.get_order_history_impl",
        return_value={
            "orders": [
                {
                    "order_id": "KR001",
                    "symbol": "064350",
                    "side": "buy",
                    "status": "pending",
                    "ordered_price": 188000,
                    "ordered_qty": 1,
                    "remaining_qty": 1,
                    "ordered_at": "20260318 100000",
                    "currency": "KRW",
                },
            ],
            "errors": [],
        },
    )
    mocker.patch(
        "app.services.n8n_pending_orders_service.get_kr_names_by_symbols",
        return_value={"064350": "현대로템"},
    )
    mocker.patch(
        "app.services.n8n_pending_orders_service.get_quote",
        side_effect=Exception("skip"),
    )

    result = await fetch_pending_orders(
        market="kr",
        include_current_price=False,
        include_indicators=False,
    )

    order = result["orders"][0]
    assert order["name"] == "현대로템"
    assert "현대로템(064350)" in order["summary_line"]


@pytest.mark.unit
async def test_fetch_pending_orders_kr_name_lookup_failure_graceful(mocker):
    """종목명 조회 실패 시 name=None, summary_line은 symbol만 표시."""
    from app.services.n8n_pending_orders_service import fetch_pending_orders

    mocker.patch(
        "app.services.n8n_pending_orders_service.get_order_history_impl",
        return_value={
            "orders": [
                {
                    "order_id": "KR001",
                    "symbol": "064350",
                    "side": "buy",
                    "status": "pending",
                    "ordered_price": 188000,
                    "ordered_qty": 1,
                    "remaining_qty": 1,
                    "ordered_at": "20260318 100000",
                    "currency": "KRW",
                },
            ],
            "errors": [],
        },
    )
    mocker.patch(
        "app.services.n8n_pending_orders_service.get_kr_names_by_symbols",
        side_effect=Exception("DB down"),
    )

    result = await fetch_pending_orders(
        market="kr",
        include_current_price=False,
        include_indicators=False,
    )

    order = result["orders"][0]
    assert order["name"] is None
    assert order["summary_line"].startswith("064350")
```

**Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/ -k "test_fetch_pending_orders_kr_name" -v`
Expected: FAIL

**Step 3: 구현**

`app/services/n8n_pending_orders_service.py` 수정:

1. import 추가:
```python
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
```

2. `fetch_pending_orders` 함수 내, `normalized_orders` 생성 후 + `include_current_price` 블록 전에 KR name enrichment 추가:

```python
    # --- KR name enrichment ---
    kr_symbols = [
        order["symbol"]
        for order in normalized_orders
        if order["market"] == "kr" and order["symbol"]
    ]
    kr_name_map: dict[str, str] = {}
    if kr_symbols:
        try:
            kr_name_map = await get_kr_names_by_symbols(kr_symbols)
        except Exception as exc:  # noqa: BLE001
            errors.append({"market": "kr", "error": f"name lookup failed: {exc}"})

    for order in normalized_orders:
        order["name"] = kr_name_map.get(order["symbol"]) if order["market"] == "kr" else None
```

3. `_normalize_order` 반환 dict에 `"name": None` 추가 (Task 4에서 덮어쓸 기본값):

```python
    return {
        ...
        "symbol": symbol,
        "name": None,          # ← 추가
        "raw_symbol": raw_symbol,
        ...
    }
```

**Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/ -k "test_fetch_pending_orders_kr_name" -v`
Expected: PASS

**Step 5: 기존 테스트 전체 확인**

Run: `uv run pytest tests/test_n8n_api.py -v`
Expected: 기존 테스트도 PASS (name=None이 기본이므로 기존 동작 변경 없음)

**Step 6: 커밋**

```bash
git add app/services/n8n_pending_orders_service.py tests/test_n8n_api.py
git commit -m "feat(pending-orders): enrich KR orders with stock names from kr_symbol_universe"
```

---

### Task 5: 기존 테스트 호환성 확인 및 수정

**Files:**
- Modify: `tests/test_n8n_api.py` (필요 시)
- Check: `tests/test_n8n_daily_brief_service.py`

**Step 1: 전체 n8n 관련 테스트 실행**

Run: `uv run pytest tests/ -k "n8n" -v`
Expected: PASS

기존 테스트에서 `name` 필드 누락으로 Pydantic 에러가 나면 → `name=None`이 default이므로 발생하지 않아야 함.

만약 mocker에서 `get_kr_names_by_symbols`를 패치하지 않아 실제 DB 호출이 일어나면 → 해당 테스트에 mock 추가 필요:

```python
mocker.patch(
    "app.services.n8n_pending_orders_service.get_kr_names_by_symbols",
    return_value={},
)
```

**Step 2: lint + 타입 체크**

Run: `make lint`
Expected: PASS

**Step 3: 커밋 (수정이 있을 경우만)**

```bash
git add tests/
git commit -m "test: fix existing n8n tests for name enrichment compatibility"
```

---

### Task 6: 전체 검증 및 정리

**Step 1: 전체 테스트 스위트**

Run: `make test`
Expected: PASS

**Step 2: LSP 진단**

변경된 파일들에 대해 diagnostics 확인:
- `app/services/kr_symbol_universe_service.py`
- `app/schemas/n8n.py`
- `app/services/n8n_formatting.py`
- `app/services/n8n_pending_orders_service.py`

**Step 3: 최종 동작 확인**

변경 후 기대되는 KR pending order 응답:

```json
{
  "order_id": "KR001",
  "symbol": "064350",
  "name": "현대로템",
  "summary_line": "현대로템(064350) buy @18.8만 (현재 17.5만, -6.9%, 18.8만, 2일)"
}
```

Crypto 응답은 변경 없음:
```json
{
  "order_id": "C001",
  "symbol": "BTC",
  "name": null,
  "summary_line": "BTC buy @1.49억 (현재 1.49억, +0.5%, 29.7만, 6시간)"
}
```

---

## 미구현 사항 (후속 이슈)

- **US 주문 종목명**: `us_symbol_universe` 테이블에서 동일 패턴으로 enrichment 가능. 이슈에서 "고려"로 언급했으므로 별도 이슈로 분리 권장.
- **Crypto 종목명**: 이미 `BTC`, `ETH` 등 읽기 쉬운 심볼이므로 enrichment 불필요.
