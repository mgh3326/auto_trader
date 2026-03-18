# Fix: domestic_orders.py EXCG_ID_DVSN_CD SOR 하드코딩 제거

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** NXT 비대상 종목에 SOR 주문이 거부되는 버그를 수정. `kr_symbol_universe.nxt_eligible`을 조회하여 조건부로 SOR/빈문자열을 설정.

**Architecture:** `kr_symbol_universe_service.py`에 `is_nxt_eligible(symbol)` 공개 함수를 추가하고, `domestic_orders.py`의 3개 주문 메서드에서 이를 호출하여 `EXCG_ID_DVSN_CD` 값을 동적으로 결정. DB 접근은 기존 서비스 레이어 패턴을 따라 `AsyncSessionLocal`로 자체 세션을 생성하므로 `DomesticOrderClient`에 DB 의존성을 추가하지 않음.

**Tech Stack:** Python 3.13+, SQLAlchemy async, pytest, AsyncMock

---

## Design Decisions

1. **헬퍼 위치**: `kr_symbol_universe_service.py` (NOT `DomesticOrderClient`)
   - `app/services/brokers/kis/` 디렉토리에는 현재 DB 의존성이 전혀 없음
   - `kr_symbol_universe_service.py`에 이미 `AsyncSessionLocal` import, `get_kr_symbol_by_name()` 등 동일 패턴 존재
   - 향후 다른 서비스에서도 재사용 가능

2. **캐싱**: 없음 (주문 빈도가 낮아 DB 쿼리 오버헤드 무시 가능. 필요시 추후 추가)

3. **안전 폴백**: 종목 미조회 시 `False` 반환 → 일반 KRX 주문 (`""`) → 모든 종목에서 동작

---

### Task 1: `kr_symbol_universe_service.py`에 `is_nxt_eligible()` 추가

**Files:**
- Modify: `app/services/kr_symbol_universe_service.py:350-451`
- Test: `tests/test_kr_symbol_universe_sync.py`

**Step 1: Write the failing tests**

`tests/test_kr_symbol_universe_sync.py` 파일 끝에 추가:

```python
@pytest.mark.unit
class TestIsNxtEligible:
    """Verify is_nxt_eligible returns correct NXT eligibility for symbols."""

    @pytest.mark.asyncio
    async def test_nxt_eligible_symbol_returns_true(self, db_session):
        """NXT 대상 종목(예: 005930)은 True 반환."""
        from app.models.kr_symbol_universe import KRSymbolUniverse
        from app.services.kr_symbol_universe_service import is_nxt_eligible

        db_session.add(
            KRSymbolUniverse(
                symbol="005930", name="삼성전자", exchange="KOSPI",
                nxt_eligible=True, is_active=True,
            )
        )
        await db_session.flush()

        result = await is_nxt_eligible("005930", db=db_session)
        assert result is True

    @pytest.mark.asyncio
    async def test_non_nxt_eligible_symbol_returns_false(self, db_session):
        """NXT 비대상 종목(예: 034220)은 False 반환."""
        from app.models.kr_symbol_universe import KRSymbolUniverse
        from app.services.kr_symbol_universe_service import is_nxt_eligible

        db_session.add(
            KRSymbolUniverse(
                symbol="034220", name="LG디스플레이", exchange="KOSPI",
                nxt_eligible=False, is_active=True,
            )
        )
        await db_session.flush()

        result = await is_nxt_eligible("034220", db=db_session)
        assert result is False

    @pytest.mark.asyncio
    async def test_unknown_symbol_returns_false(self, db_session):
        """DB에 없는 종목은 안전 폴백으로 False 반환."""
        from app.services.kr_symbol_universe_service import is_nxt_eligible

        result = await is_nxt_eligible("999999", db=db_session)
        assert result is False

    @pytest.mark.asyncio
    async def test_inactive_symbol_returns_false(self, db_session):
        """비활성 종목은 False 반환."""
        from app.models.kr_symbol_universe import KRSymbolUniverse
        from app.services.kr_symbol_universe_service import is_nxt_eligible

        db_session.add(
            KRSymbolUniverse(
                symbol="000000", name="상장폐지종목", exchange="KOSPI",
                nxt_eligible=True, is_active=False,
            )
        )
        await db_session.flush()

        result = await is_nxt_eligible("000000", db=db_session)
        assert result is False
```

> **Note:** `db_session` fixture는 `conftest.py`에 있는지 확인 필요. 없으면 `test_kr_symbol_universe_sync.py`의 기존 DB fixture 패턴을 확인하여 동일하게 사용. 만약 기존 테스트가 실제 DB 대신 mock/fake DB를 사용한다면, 같은 패턴을 따를 것. 기존 테스트 파일의 fixture 패턴을 반드시 먼저 확인.

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_kr_symbol_universe_sync.py::TestIsNxtEligible -v
```

Expected: FAIL — `ImportError: cannot import name 'is_nxt_eligible'`

**Step 3: Implement `is_nxt_eligible()`**

`app/services/kr_symbol_universe_service.py`에서 `get_kr_symbol_by_name()` 바로 아래에 추가:

```python
async def _check_nxt_eligible(db: AsyncSession, symbol: str) -> bool:
    stmt = select(KRSymbolUniverse.nxt_eligible).where(
        KRSymbolUniverse.symbol == symbol,
        KRSymbolUniverse.is_active.is_(True),
    )
    result = await db.execute(stmt)
    value = result.scalar_one_or_none()
    return bool(value)


async def is_nxt_eligible(
    symbol: str,
    db: AsyncSession | None = None,
) -> bool:
    """Return True if *symbol* is eligible for NXT (Smart Order Routing).

    Falls back to False (standard KRX routing) when the symbol is not
    found, inactive, or the universe table is empty.
    """
    if db is not None:
        return await _check_nxt_eligible(db, symbol)

    async with AsyncSessionLocal() as session:
        return await _check_nxt_eligible(session, symbol)
```

`__all__` 리스트에 `"is_nxt_eligible"` 추가:

```python
__all__ = [
    ...
    "is_nxt_eligible",
    ...
]
```

**Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_kr_symbol_universe_sync.py::TestIsNxtEligible -v
```

Expected: 4 PASS

**Step 5: Commit**

```bash
git add app/services/kr_symbol_universe_service.py tests/test_kr_symbol_universe_sync.py
git commit -m "feat: add is_nxt_eligible() to kr_symbol_universe_service"
```

---

### Task 2: `domestic_orders.py` 3곳 수정 — SOR 조건부 적용

**Files:**
- Modify: `app/services/brokers/kis/domestic_orders.py:1,289,452,744`
- Test: `tests/test_kis_domestic_orders_nxt.py` (새 파일)

**Step 1: Write failing tests**

새 파일 `tests/test_kis_domestic_orders_nxt.py` 생성:

```python
"""Tests for NXT-conditional EXCG_ID_DVSN_CD routing in domestic orders."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_NXT_ELIGIBLE_PATH = "app.services.brokers.kis.domestic_orders.is_nxt_eligible"


def _make_client():
    """Create DomesticOrderClient with mocked parent (same pattern as retry tests)."""
    from app.services.brokers.kis.domestic_orders import DomesticOrderClient

    parent = MagicMock()
    parent._hdr_base = {"content-type": "application/json"}
    parent._ensure_token = AsyncMock()

    settings = MagicMock()
    settings.kis_account_no = "1234567890"
    settings.kis_access_token = "test-token"
    parent._settings = settings

    return DomesticOrderClient(parent), parent


def _success_response(**extra):
    return {"rt_cd": "0", "output": {"ODNO": "00001", "ORD_TMD": "120000"}, **extra}


@pytest.mark.unit
class TestOrderKoreaStockNxtRouting:
    """order_korea_stock sets EXCG_ID_DVSN_CD based on NXT eligibility."""

    @pytest.mark.asyncio
    async def test_nxt_eligible_uses_sor(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=True)):
            await instance.order_korea_stock("005930", "buy", 10, 70000)

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == "SOR"

    @pytest.mark.asyncio
    async def test_non_nxt_uses_empty_string(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=False)):
            await instance.order_korea_stock("034220", "buy", 10, 5000)

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == ""


@pytest.mark.unit
class TestCancelKoreaOrderNxtRouting:
    """cancel_korea_order sets EXCG_ID_DVSN_CD based on NXT eligibility."""

    @pytest.mark.asyncio
    async def test_nxt_eligible_uses_sor(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=True)):
            await instance.cancel_korea_order(
                order_number="00001", stock_code="005930",
                quantity=10, price=70000, order_type="buy",
                krx_fwdg_ord_orgno="00091",
            )

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == "SOR"

    @pytest.mark.asyncio
    async def test_non_nxt_uses_empty_string(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=False)):
            await instance.cancel_korea_order(
                order_number="00001", stock_code="034220",
                quantity=10, price=5000, order_type="sell",
                krx_fwdg_ord_orgno="00091",
            )

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == ""


@pytest.mark.unit
class TestModifyKoreaOrderNxtRouting:
    """modify_korea_order sets EXCG_ID_DVSN_CD based on NXT eligibility."""

    @pytest.mark.asyncio
    async def test_nxt_eligible_uses_sor(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=True)):
            await instance.modify_korea_order(
                order_number="00001", stock_code="005930",
                quantity=10, new_price=71000,
                krx_fwdg_ord_orgno="00091",
            )

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == "SOR"

    @pytest.mark.asyncio
    async def test_non_nxt_uses_empty_string(self):
        instance, parent = _make_client()
        parent._request_with_rate_limit = AsyncMock(return_value=_success_response())

        with patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=False)):
            await instance.modify_korea_order(
                order_number="00001", stock_code="034220",
                quantity=10, new_price=5500,
                krx_fwdg_ord_orgno="00091",
            )

        body = parent._request_with_rate_limit.call_args.kwargs.get(
            "json_body"
        ) or parent._request_with_rate_limit.call_args[1].get("json_body")
        assert body["EXCG_ID_DVSN_CD"] == ""
```

> **Note:** `body` 추출 방식은 `_request_with_rate_limit` 호출이 keyword arg로 `json_body`를 받는 패턴을 따름 (기존 코드 `json_body=body` 확인됨). 실제 call_args 구조가 다를 수 있으니 테스트 실행 후 조정.

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_kis_domestic_orders_nxt.py -v
```

Expected: FAIL — `is_nxt_eligible` not imported in `domestic_orders.py`, or still hardcoded `"SOR"`.

**Step 3: Modify `domestic_orders.py`**

**3a. Import 추가** (파일 상단, line ~10 부근):

```python
from app.services.kr_symbol_universe_service import is_nxt_eligible
```

**3b. `order_korea_stock()` 수정** (line 289 부근):

변경 전:
```python
        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "PDNO": stock_code,  # 종목코드
            "ORD_DVSN": ord_dvsn,  # 주문구분 (00:지정가, 01:시장가)
            "ORD_QTY": str(quantity),  # 주문수량
            "ORD_UNPR": str(price),  # 주문단가 (시장가일 경우 0)
            "EXCG_ID_DVSN_CD": "SOR",
        }
```

변경 후:
```python
        nxt = await is_nxt_eligible(stock_code)
        excg_id_dvsn_cd = "SOR" if nxt else ""

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "PDNO": stock_code,  # 종목코드
            "ORD_DVSN": ord_dvsn,  # 주문구분 (00:지정가, 01:시장가)
            "ORD_QTY": str(quantity),  # 주문수량
            "ORD_UNPR": str(price),  # 주문단가 (시장가일 경우 0)
            "EXCG_ID_DVSN_CD": excg_id_dvsn_cd,
        }
```

**3c. `cancel_korea_order()` 수정** (line 452 부근):

변경 전:
```python
        body = {
            ...
            "EXCG_ID_DVSN_CD": "SOR",
        }
```

변경 후:
```python
        nxt = await is_nxt_eligible(stock_code)
        excg_id_dvsn_cd = "SOR" if nxt else ""

        body = {
            ...
            "EXCG_ID_DVSN_CD": excg_id_dvsn_cd,
        }
```

**3d. `modify_korea_order()` 수정** (line 744 부근):

동일 패턴 적용:
```python
        nxt = await is_nxt_eligible(stock_code)
        excg_id_dvsn_cd = "SOR" if nxt else ""

        body = {
            ...
            "EXCG_ID_DVSN_CD": excg_id_dvsn_cd,
        }
```

**Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_kis_domestic_orders_nxt.py tests/test_kis_domestic_orders_retry.py -v
```

Expected: 6 new PASS + 3 existing PASS (retry tests가 깨지지 않았는지도 확인)

> **Note:** retry 테스트에서 `is_nxt_eligible`이 mock되지 않아 DB 접근 시도할 수 있음. 이 경우 retry 테스트에도 `@patch(_NXT_ELIGIBLE_PATH, AsyncMock(return_value=False))` 데코레이터를 추가하거나, conftest에 auto-use fixture를 넣어 해결. 기존 테스트 호환성을 반드시 확인.

**Step 5: Commit**

```bash
git add app/services/brokers/kis/domestic_orders.py tests/test_kis_domestic_orders_nxt.py
git commit -m "fix: use NXT eligibility to conditionally set SOR routing in domestic orders"
```

---

### Task 3: 기존 테스트 호환성 확인 및 전체 검증

**Files:**
- Possibly modify: `tests/test_kis_domestic_orders_retry.py` (if broken by new import)
- Possibly modify: `tests/test_mcp_place_order.py`, `tests/test_kis_trading_service.py` (if they call order methods)

**Step 1: Run full related test suite**

```bash
uv run pytest tests/test_kis_domestic_orders_retry.py tests/test_kis_domestic_orders_nxt.py tests/test_kis_trading_service.py tests/test_mcp_place_order.py tests/test_mcp_order_tools.py -v
```

**Step 2: If any test fails due to `is_nxt_eligible` DB access**

기존 테스트가 `order_korea_stock` / `cancel_korea_order` / `modify_korea_order`를 호출하면서 `is_nxt_eligible`을 mock하지 않으면 DB 접근 오류 발생 가능.

수정: 해당 테스트 파일에서 `is_nxt_eligible`을 mock하거나, `conftest.py`에 자동 fixture 추가:

```python
# tests/conftest.py (or relevant conftest)
@pytest.fixture(autouse=True)
def _mock_nxt_eligible(monkeypatch):
    """Default NXT eligible to False for all domestic order tests."""
    monkeypatch.setattr(
        "app.services.brokers.kis.domestic_orders.is_nxt_eligible",
        AsyncMock(return_value=False),
    )
```

> **주의:** autouse fixture 범위를 너무 넓게 잡으면 다른 테스트에 영향. 필요한 테스트 파일 수준의 conftest에만 적용하거나, 각 테스트에서 명시적 patch 사용.

**Step 3: Run lint**

```bash
make lint
```

Expected: PASS

**Step 4: Run full test suite**

```bash
make test
```

Expected: ALL PASS

**Step 5: Commit (if any compat fixes were needed)**

```bash
git add -u
git commit -m "test: patch is_nxt_eligible in existing domestic order tests"
```

---

## Verification Checklist

- [ ] `is_nxt_eligible("005930")` → `True` (NXT 대상 종목, DB에 `nxt_eligible=True`)
- [ ] `is_nxt_eligible("034220")` → `False` (NXT 비대상 종목, DB에 `nxt_eligible=False`)
- [ ] `is_nxt_eligible("999999")` → `False` (DB에 없는 종목 → 안전 폴백)
- [ ] `is_nxt_eligible` on inactive symbol → `False`
- [ ] `order_korea_stock` with NXT eligible → `EXCG_ID_DVSN_CD: "SOR"`
- [ ] `order_korea_stock` with non-NXT → `EXCG_ID_DVSN_CD: ""`
- [ ] `cancel_korea_order` — 동일 검증
- [ ] `modify_korea_order` — 동일 검증
- [ ] 기존 retry 테스트 미파손
- [ ] `make lint` PASS
- [ ] `make test` PASS

## Reference Files

| File | Role |
|------|------|
| `app/services/brokers/kis/domestic_orders.py` | 수정 대상 — 3곳 SOR 하드코딩 |
| `app/services/kr_symbol_universe_service.py` | 수정 대상 — `is_nxt_eligible()` 추가 |
| `app/models/kr_symbol_universe.py` | 참조 — `KRSymbolUniverse` 모델 (symbol, nxt_eligible, is_active) |
| `app/core/db.py` | 참조 — `AsyncSessionLocal` |
| `tests/test_kis_domestic_orders_retry.py` | 참조 — 기존 DomesticOrderClient 테스트 패턴 |
| `tests/test_kr_symbol_universe_sync.py` | 수정 대상 — `is_nxt_eligible` 테스트 추가 |
