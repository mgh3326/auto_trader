# ROB-419 live 매수 precheck 예약-인지 orderable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** live 매수 precheck `_get_balance_for_order`가 raw orderable 대신 `get_cash_balance_impl`의 예약-차감된 orderable(= `get_available_capital`과 동일)을 사용하도록 정합해, 대기주문이 현금을 예약한 경우 dry_run에서 insufficient를 잡게 한다.

**Architecture:** `_get_balance_for_order`의 live(equity_kr/equity_us, is_mock=False) 분기를 `get_cash_balance_impl(account="kis_domestic"|"kis_overseas")` 위임으로 교체(단일 소스). mock/crypto 분기 무변경(mock 예약은 ROB-341 shadow-exposure가 담당). read-only(ledger 무접근), migration 0.

**Tech Stack:** Python 3.13, pytest(asyncio + monkeypatch). 순수 위임 + async, 테스트는 get_cash_balance_impl 스텁.

---

## File Structure

- Modify: `app/mcp_server/tooling/order_validation.py` — `_live_kis_orderable` 헬퍼 + `_get_balance_for_order` live 분기 + import
- Create: `tests/test_order_live_precheck_reservation.py` — 위임/예약-차감/mock-미델리게이트 단위 테스트

---

## Task 1: live precheck를 get_cash_balance_impl 예약-차감 orderable로 위임

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py`
- Test: `tests/test_order_live_precheck_reservation.py`

배경: `_get_balance_for_order`(`order_validation.py:376-406`) live 분기는 raw orderable만 읽어 대기주문 예약을 미반영. `get_cash_balance_impl`(get_available_capital의 소스)은 `max(0, raw − pending_buy_amount)`로 차감하므로 그 값을 재사용해 precheck == get_available_capital을 보장.

- [ ] **Step 1: Write the failing test**

`tests/test_order_live_precheck_reservation.py` 신규 생성:

```python
"""ROB-419 — live buy precheck uses reservation-aware orderable (== get_available_capital)."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import (
    _check_balance_and_warn,
    _get_balance_for_order,
)


def _order_error(message: str) -> dict:
    return {"success": False, "error": message}


@pytest.mark.asyncio
async def test_live_kr_precheck_uses_reservation_adjusted_orderable(monkeypatch):
    seen = {}

    async def fake_cash(account=None, *, is_mock=False):
        seen["account"] = account
        seen["is_mock"] = is_mock
        # raw orderable was higher, but pending orders reserved it to 0.
        return {"accounts": [{"account": "kis_domestic", "currency": "KRW", "orderable": 0.0}]}

    monkeypatch.setattr(order_validation, "get_cash_balance_impl", fake_cash)

    balance = await _get_balance_for_order("equity_kr", is_mock=False)
    assert balance == 0.0
    assert seen == {"account": "kis_domestic", "is_mock": False}


@pytest.mark.asyncio
async def test_live_us_precheck_uses_reservation_adjusted_orderable(monkeypatch):
    seen = {}

    async def fake_cash(account=None, *, is_mock=False):
        seen["account"] = account
        return {"accounts": [{"account": "kis_overseas", "currency": "USD", "orderable": 0.0}]}

    monkeypatch.setattr(order_validation, "get_cash_balance_impl", fake_cash)

    balance = await _get_balance_for_order("equity_us", is_mock=False)
    assert balance == 0.0
    assert seen["account"] == "kis_overseas"


@pytest.mark.asyncio
async def test_live_us_buy_blocked_when_orderable_reserved_to_zero(monkeypatch):
    # repro: pending orders reserved all cash → orderable=0 → buy must NOT pass.
    async def fake_cash(account=None, *, is_mock=False):
        return {"accounts": [{"account": "kis_overseas", "currency": "USD", "orderable": 0.0}]}

    monkeypatch.setattr(order_validation, "get_cash_balance_impl", fake_cash)

    # dry_run: insufficient warning (no error, preview still returned upstream).
    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=True,
        order_error_fn=_order_error,
        is_mock=False,
    )
    assert error is None
    assert warning is not None and "Insufficient" in warning

    # non-dry_run: hard error.
    warning2, error2 = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=False,
    )
    assert error2 is not None
    assert error2["success"] is False


@pytest.mark.asyncio
async def test_mock_kr_precheck_does_not_delegate_to_cash_balance(monkeypatch):
    called = {"cash": False}

    async def fake_cash(account=None, *, is_mock=False):
        called["cash"] = True
        return {"accounts": []}

    monkeypatch.setattr(order_validation, "get_cash_balance_impl", fake_cash)

    class FakeKIS:
        def __init__(self, is_mock: bool = False):
            self.is_mock = is_mock

        async def inquire_domestic_cash_balance(self, *, is_mock: bool = False):
            return {"stck_cash_ord_psbl_amt": 5_000_000}

    monkeypatch.setattr(order_validation, "KISClient", FakeKIS)

    balance = await _get_balance_for_order("equity_kr", is_mock=True)
    assert balance == 5_000_000.0
    assert called["cash"] is False  # mock path must NOT use get_cash_balance_impl
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-419 && uv run pytest tests/test_order_live_precheck_reservation.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'get_cash_balance_impl'`(아직 import 안 함) 또는 live 분기가 위임 안 해 raw 경로로 빠짐.

- [ ] **Step 3: Write minimal implementation**

`app/mcp_server/tooling/order_validation.py`:

(a) import 추가 — 기존 portfolio_cash import 블록(`:22-27`) 뒤에:

```python
from app.mcp_server.tooling.portfolio_cash import (
    get_cash_balance_impl,
)
```

(b) 헬퍼 추가 — `_get_balance_for_order`(`:376`) 정의 바로 앞:

```python
async def _live_kis_orderable(account_token: str) -> float:
    """live KIS 예약-차감 orderable (단일 소스 = get_available_capital의 소스).

    ``account_token``: "kis_domestic" (KRW) | "kis_overseas" (USD).
    ``get_cash_balance_impl``이 raw orderable에서 대기주문 예약을 차감(실패 시 raw
    fallback)하므로 precheck가 get_available_capital과 동일 orderable을 본다.
    """
    result = await get_cash_balance_impl(account=account_token, is_mock=False)
    for acc in result.get("accounts", []):
        if acc.get("account") == account_token:
            return float(acc.get("orderable") or 0.0)
    raise RuntimeError(f"{account_token} orderable not found in cash balance")
```

(c) `_get_balance_for_order` live 분기 교체. 현재(`:384-406`):

```python
    if market_type == "equity_kr":
        kis = _create_kis_client(is_mock=is_mock)
        if is_mock:
            cash_summary = await _call_kis(
                kis.inquire_domestic_cash_balance,
                is_mock=is_mock,
            )
            return float(cash_summary.get("stck_cash_ord_psbl_amt") or 0)
        margin_data = await _call_kis(
            kis.inquire_integrated_margin,
            is_mock=is_mock,
        )
        domestic_cash = extract_domestic_cash_summary_from_integrated_margin(
            margin_data
        )
        return float(domestic_cash.get("orderable") or 0)

    kis = _create_kis_client(is_mock=is_mock)
    margin_data = await _call_kis(kis.inquire_overseas_margin, is_mock=is_mock)
    usd_row = _select_usd_row_for_us_order(margin_data)
    if usd_row is None:
        raise RuntimeError("USD margin data not found in KIS overseas margin")
    return _extract_usd_orderable_from_row(usd_row)
```

교체:

```python
    if market_type == "equity_kr":
        if is_mock:
            kis = _create_kis_client(is_mock=is_mock)
            cash_summary = await _call_kis(
                kis.inquire_domestic_cash_balance,
                is_mock=is_mock,
            )
            return float(cash_summary.get("stck_cash_ord_psbl_amt") or 0)
        # ROB-419 — live: reservation-aware orderable (== get_available_capital),
        # so pending-order cash reservations are not treated as spendable.
        return await _live_kis_orderable("kis_domestic")

    if not is_mock:
        # ROB-419 — live US: reservation-aware orderable via the single source.
        return await _live_kis_orderable("kis_overseas")

    # mock US: KIS 모의투자엔 해외 orderable-cash 서비스가 없음(OPSQ0002). ROB-417
    # 조기 가드가 _check_balance_and_warn에서 선제 차단하므로 여기 도달은 방어적.
    kis = _create_kis_client(is_mock=is_mock)
    margin_data = await _call_kis(kis.inquire_overseas_margin, is_mock=is_mock)
    usd_row = _select_usd_row_for_us_order(margin_data)
    if usd_row is None:
        raise RuntimeError("USD margin data not found in KIS overseas margin")
    return _extract_usd_orderable_from_row(usd_row)
```

> 참고: `extract_domestic_cash_summary_from_integrated_margin` import가 다른 곳에서 더 쓰이지 않으면 ruff가 unused로 잡을 수 있음 — Step 4 lint에서 확인 후 미사용 시 import 제거.

- [ ] **Step 4: Run test + lint to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-419 && uv run pytest tests/test_order_live_precheck_reservation.py -v && uv run ruff check app/mcp_server/tooling/order_validation.py`
Expected: PASS (4건). ruff에서 unused import(`extract_domestic_cash_summary_from_integrated_margin` 등) 보고 시 해당 import 제거 후 재실행.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-419
git add app/mcp_server/tooling/order_validation.py tests/test_order_live_precheck_reservation.py
git commit -m "fix(ROB-419): live 매수 precheck를 예약-차감 orderable(get_available_capital 소스)로 정합

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 회귀 + lint/ty 검증

**Files:**
- (없음 — 검증만; Step 1에서 회귀 발견 시 갱신)

- [ ] **Step 1: 기존 precheck balance 단언 탐색**

Run: `cd /Users/mgh3326/work/auto_trader.rob-419 && grep -rn "inquire_integrated_margin\|inquire_overseas_margin\|_get_balance_for_order\|stck_cash_ord_psbl_amt" tests/`
조치: live KR/US 매수 precheck가 `inquire_integrated_margin`/`inquire_overseas_margin`을 직접 stub하고 raw orderable을 단언하는 테스트가 있으면, 이제 live는 `get_cash_balance_impl`로 위임하므로 해당 테스트를 `get_cash_balance_impl` 스텁 또는 그 내부 호출(inquire_korea_orders/inquire_overseas_orders 포함)에 맞게 갱신하고 함께 커밋. mock 경로 단언은 무변경. 매치 없으면 스킵.

- [ ] **Step 2: 인접 회귀 스위트**

Run: `cd /Users/mgh3326/work/auto_trader.rob-419 && uv run pytest tests/test_order_live_precheck_reservation.py tests/test_order_us_mock_buy_unsupported.py tests/test_kis_mock_routing.py tests/test_mcp_place_order.py -q 2>&1 | tail -10`
Expected: PASS. (로컬 공유-DB `uq_live_ledger_order` 잔여물로 인한 pre-existing 실패는 단독 재현으로 내 변경 무관임을 확인하고 기록 — 깨끗한 main도 동일 실패.)

- [ ] **Step 3: Lint + format + ty**

Run: `cd /Users/mgh3326/work/auto_trader.rob-419 && uv run ruff check app/mcp_server/tooling/order_validation.py tests/test_order_live_precheck_reservation.py && uv run ruff format --check app/mcp_server/tooling/order_validation.py tests/test_order_live_precheck_reservation.py && uv run ty check app/mcp_server/tooling/order_validation.py --error-on-warning 2>&1 | tail -5`
Expected: All checks passed / already formatted. (포맷 실패 시 `uv run ruff format <file>`.)

- [ ] **Step 4: Mutation import guard (read-only invariant)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-419 && uv run pytest -k "import_guard or mutation_guard" -q 2>&1 | tail -5`
Expected: green(order_validation에 broker order-mutation 미도입; KIS 조회만). guard 없으면 스킵.

- [ ] **Step 5: Step 1/3에서 파일 갱신했다면 커밋(없으면 스킵)**

```bash
cd /Users/mgh3326/work/auto_trader.rob-419
git add -A && git commit -m "test(ROB-419): live precheck 위임 회귀 갱신/포맷

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review 결과

**Spec 커버리지:**
- Unit 1 (`_live_kis_orderable` 위임 + live 분기 교체) → Task 1 ✅
- live KR·US 예약-차감, mock 미델리게이트, crypto 무변경 → Task 1 테스트 1·2·4 ✅
- repro 재현·해소(orderable=0 → dry_run warning / 실주문 error) → Task 1 테스트 3 ✅
- 회귀/lint/ty/import-guard → Task 2 ✅

**Placeholder 스캔:** 없음 — 모든 코드 step에 실제 코드 포함.

**Type 일관성:** `_live_kis_orderable(account_token: str) -> float` 정의/호출 일치. `get_cash_balance_impl(account=..., is_mock=False)` 시그니처는 portfolio_cash 정의(`account: str | None, *, is_mock: bool`)와 일치. live equity_kr→"kis_domestic", equity_us→"kis_overseas" 토큰 일관.

**안전 경계 재확인:** precheck read-only(KIS 조회만, ledger/주문 mutation 없음) → ROB-409/407 경계 무접근. 예약차감 실패 fallback(get_cash_balance_impl 상속). mock 무변경(ROB-341 이중차감 회피). migration 0. current_price echo는 Non-goal.
