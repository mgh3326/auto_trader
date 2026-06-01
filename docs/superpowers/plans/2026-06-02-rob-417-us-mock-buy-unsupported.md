# ROB-417 US mock 매수 미지원 명확화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** US `kis_mock` 매수가 KIS 모의투자의 해외 orderable-cash 미지원(OPSQ0002)으로 원천 불가임을 capability_matrix 기반 조기 가드로 명확히(`mock_unsupported` 태그 + 명시 메시지) fail-closed 표면화한다.

**Architecture:** `_check_balance_and_warn` 맨 앞에 US-mock-buy 조기 가드를 추가 — `capability_matrix`의 kis_mock `account_cash_read=False`를 권위 소스로 사용해 KIS 네트워크 호출 전 결정적으로 차단. dry_run은 명확 warning + 프리뷰 유지, 실주문은 `mock_unsupported` 태그 에러. KR/live/crypto 무영향, broker/order mutation 없음.

**Tech Stack:** Python 3.13, pytest(asyncio). 순수 함수 + async 헬퍼, DB/네트워크 없음(테스트는 stub order_error_fn + monkeypatch spy).

---

## File Structure

- Modify: `app/mcp_server/tooling/order_validation.py` — `_kis_mock_us_orderable_unsupported` 헬퍼 + `_check_balance_and_warn` 조기 가드
- Create: `tests/test_order_us_mock_buy_unsupported.py` — 헬퍼 + 가드 단위 테스트

---

## Task 1: capability_matrix 헬퍼 + US mock buy 조기 가드

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py`
- Test: `tests/test_order_us_mock_buy_unsupported.py`

배경: US mock buy는 `_get_balance_for_order`가 OPSQ0002로 raise → `_check_balance_and_warn` 예외 분기가 generic 메시지로 뭉갬(`mock_unsupported` 미태깅, 구조적 미지원 vs 일시실패 미구분). capability_matrix(`account_cash_read=False`)를 권위 소스로 조기 차단.

- [ ] **Step 1: Write the failing test**

`tests/test_order_us_mock_buy_unsupported.py` 신규 생성:

```python
"""ROB-417 — US kis_mock buy is fail-closed unsupported (OPSQ0002), made explicit."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import (
    _check_balance_and_warn,
    _kis_mock_us_orderable_unsupported,
)


def _order_error(message: str) -> dict:
    return {"success": False, "error": message}


def test_kis_mock_us_orderable_unsupported_reflects_capability_matrix():
    # capability_matrix documents kis_mock account_cash_read=False (OPSQ0002).
    assert _kis_mock_us_orderable_unsupported() is True


@pytest.mark.asyncio
async def test_us_mock_buy_non_dry_run_blocked_with_mock_unsupported(monkeypatch):
    called = {"balance": False}

    async def spy_balance(*_a, **_k):
        called["balance"] = True
        return 0.0

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=True,
    )
    assert warning is None
    assert error is not None
    assert error["success"] is False
    assert error["mock_unsupported"] is True
    assert error["capability"] == "kis_mock_us_orderable_cash_unsupported"
    assert "unsupported" in error["error"].lower()
    # Early guard short-circuits BEFORE any KIS network call.
    assert called["balance"] is False


@pytest.mark.asyncio
async def test_us_mock_buy_dry_run_returns_clear_warning_keeps_preview(monkeypatch):
    async def spy_balance(*_a, **_k):
        raise AssertionError("must not be called for US mock buy guard")

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=True,
        order_error_fn=_order_error,
        is_mock=True,
    )
    assert error is None  # preview not blocked
    assert warning is not None
    assert "US mock buy unsupported" in warning


@pytest.mark.asyncio
async def test_kr_mock_buy_not_guarded_enters_balance_path(monkeypatch):
    called = {"balance": False}

    async def spy_balance(market_type, is_mock=False):
        called["balance"] = True
        return 10_000_000.0  # ample KRW

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)
    # KR mock has a DB-shadow-exposure guard; stub it to the pass-through state.
    async def fake_exposure(*_a, **_k):
        return {"confidence": "db_shadow_pending", "buy_reserved_amount": 0.0}

    monkeypatch.setattr(
        order_validation, "_get_kis_mock_shadow_exposure", fake_exposure
    )

    warning, error = await _check_balance_and_warn(
        market_type="equity_kr",
        normalized_symbol="005930",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=True,
    )
    assert error is None
    assert called["balance"] is True  # guard did NOT short-circuit KR


@pytest.mark.asyncio
async def test_us_live_buy_not_guarded(monkeypatch):
    called = {"balance": False}

    async def spy_balance(*_a, **_k):
        called["balance"] = True
        return 10_000.0

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=False,  # live
    )
    assert error is None
    assert called["balance"] is True  # live enters the real precheck
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-417 && uv run pytest tests/test_order_us_mock_buy_unsupported.py -v`
Expected: FAIL — `ImportError: cannot import name '_kis_mock_us_orderable_unsupported'`.

- [ ] **Step 3: Write minimal implementation**

`app/mcp_server/tooling/order_validation.py`:

(a) 순수 헬퍼 추가 — `_check_balance_and_warn`(라인 826) 정의 바로 앞 모듈 스코프:

```python
def _kis_mock_us_orderable_unsupported() -> bool:
    """KIS 모의투자가 해외(USD) orderable-cash 서비스를 제공하지 않는지 여부.

    OPSQ0002 "없는 서비스 코드" — 2026-05-27 live smoke로 확정. capability_matrix를
    권위 소스로 사용하므로, 미래에 US mock cash 어댑터가 생겨 account_cash_read=True가
    되면 이 가드는 자동으로 완화된다.
    """
    from app.services.us_dual_paper.capability_matrix import get_capability_matrix

    return (
        get_capability_matrix().get("kis_mock", {}).get("account_cash_read") is False
    )
```

(b) `_check_balance_and_warn` 맨 앞(docstring 직후, 라인 840 `try:` 앞)에 조기 가드 삽입:

```python
    """Pre-check cash balance for buy orders.

    Returns (warning_message_or_None, error_dict_or_None).
    If error_dict is not None, the caller should return it immediately.
    """
    # ROB-417 — KIS 모의투자는 해외 orderable-cash 서비스가 없어(OPSQ0002) US mock
    # 매수의 주문가능현금을 검증할 수 없다. capability_matrix 기반으로 KIS 호출 전
    # 결정적으로 fail-closed 처리하고, 구조적 미지원을 mock_unsupported로 명시한다.
    if (
        is_mock
        and market_type == "equity_us"
        and side == "buy"
        and _kis_mock_us_orderable_unsupported()
    ):
        message = (
            "US mock buy unsupported: KIS 모의투자 provides no overseas "
            "orderable-cash service (OPSQ0002), so orderable cash cannot be "
            "verified. Use alpaca_paper for US paper buys; kis_mock supports KR."
        )
        if dry_run:
            return f"Preview warning: {message}", None
        err = order_error_fn(message)
        err["mock_unsupported"] = True
        err["capability"] = "kis_mock_us_orderable_cash_unsupported"
        return None, err

    try:
        balance = await _get_balance_for_order(market_type, is_mock=is_mock)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-417 && uv run pytest tests/test_order_us_mock_buy_unsupported.py -v`
Expected: PASS (5건).

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-417
git add app/mcp_server/tooling/order_validation.py tests/test_order_us_mock_buy_unsupported.py
git commit -m "fix(ROB-417): US kis_mock 매수 미지원 capability 조기 가드(mock_unsupported)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: place_order 통합 경로 회귀 + lint

**Files:**
- (없음 — 검증만; Step 1에서 회귀 발견 시 갱신)

배경: 조기 가드가 `_place_order_impl` buy 경로(`order_execution.py:1052-1067`)를 통해 흐를 때 dry_run=False는 즉시 error 반환, dry_run=True는 preview+warning 반환됨을 확인. 기존 mock US 관련 테스트가 generic OPSQ0002 메시지를 단언하면 갱신 필요.

- [ ] **Step 1: 기존 OPSQ0002/balance precheck 단언 탐색**

Run: `cd /Users/mgh3326/work/auto_trader.rob-417 && grep -rn "balance precheck unavailable\|OPSQ0002\|refusing to submit" tests/`
조치: US mock **buy** 경로에서 generic 메시지를 단언하는 테스트가 있으면, 새 동작(`mock_unsupported=True` + "US mock buy unsupported")에 맞게 갱신하고 함께 커밋. KR/매도/예외 경로(진짜 일시실패) 단언은 무변경(그 경로는 가드 미적용이라 generic 유지). 매치 없으면 스킵.

- [ ] **Step 2: 인접 회귀 스위트**

Run: `cd /Users/mgh3326/work/auto_trader.rob-417 && uv run pytest tests/test_order_us_mock_buy_unsupported.py tests/test_kis_mock_routing.py tests/test_mcp_place_order.py -q 2>&1 | tail -8`
Expected: PASS — 신규 + routing + place_order 회귀 green. (로컬 공유-DB ledger 잔여물로 인한 pre-existing 실패가 있으면 단독 재현으로 내 변경 무관임을 확인하고 기록.)

- [ ] **Step 3: Lint**

Run: `cd /Users/mgh3326/work/auto_trader.rob-417 && uv run ruff check app/mcp_server/tooling/order_validation.py tests/test_order_us_mock_buy_unsupported.py && uv run ruff format --check app/mcp_server/tooling/order_validation.py tests/test_order_us_mock_buy_unsupported.py`
Expected: All checks passed / already formatted. (실패 시 `uv run ruff format <file>` 후 재확인.)

- [ ] **Step 4: ty typecheck (CI lint 일부)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-417 && uv run ty check app/mcp_server/tooling/order_validation.py --error-on-warning 2>&1 | tail -5`
Expected: All checks passed.

- [ ] **Step 5: Mutation import guard (read-only invariant)**

Run: `cd /Users/mgh3326/work/auto_trader.rob-417 && uv run pytest -k "import_guard or mutation_guard" -q 2>&1 | tail -5`
Expected: green. guard 없으면 스킵.

- [ ] **Step 6: Step 1에서 테스트 갱신했다면 커밋(없으면 스킵)**

```bash
cd /Users/mgh3326/work/auto_trader.rob-417
git add -A && git commit -m "test(ROB-417): US mock buy 미지원 메시지 단언 갱신

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review 결과

**Spec 커버리지:**
- Unit 1 (capability 헬퍼 + 조기 가드, dry_run warning / 실주문 mock_unsupported 에러) → Task 1 ✅
- KR/live/crypto 미적용 가드 → Task 1 테스트 4·5 ✅
- 네트워크 선제차단(호출 안 됨) → Task 1 테스트 2 ✅
- 회귀/lint/ty/import-guard → Task 2 ✅

**Placeholder 스캔:** 없음 — 모든 코드 step에 실제 코드 포함.

**Type 일관성:** `_kis_mock_us_orderable_unsupported() -> bool` 정의/호출 일치. 가드 조건 변수(`is_mock`/`market_type`/`side`/`dry_run`/`order_error_fn`)는 모두 `_check_balance_and_warn` 기존 파라미터. 에러 dict 키 `mock_unsupported`/`capability` 구현·테스트 일관.

**안전 경계 재확인:** fail-closed 보존, broker/order/watch/order-intent mutation 없음, migration 0, read-only. `_is_kis_mock_unsupported` 공유 마커 미변경. US 활성화는 Non-goal. 매도 라우팅은 ROB-420 소관.
