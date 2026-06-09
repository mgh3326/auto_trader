# ROB-419 — live 매수 precheck 예약-인지 orderable 정합

- **이슈**: ROB-419 (E라인 E2)
- **유형**: Bug fix
- **작성일**: 2026-06-02
- **연관**: ROB-421(오케스트레이션), ROB-409/407(live order ledger 경계 — 인접/무접근), ROB-341(mock shadow-exposure 예약차감), ROB-417(같은 precheck 표면)

## 증상 / 근본 원인

`get_available_capital(kis_live)`에서 kis_overseas `orderable=0`(대기 주문이 현금 예약)인 상태에서도 `kis_live_place_order(side=buy, dry_run=True)`가 거부 없이 success 반환 → 사용자가 실주문 단계에서야 insufficient를 발견.

근본: live 매수 precheck `_get_balance_for_order`(`order_validation.py:376-406`)는 **raw** orderable만 읽는다:
- equity_kr live: `inquire_integrated_margin` → `orderable`
- equity_us live: `inquire_overseas_margin` → `frcr_gnrl_ord_psbl_amt`

반면 `get_available_capital`의 소스 `get_cash_balance_impl`(`portfolio_cash.py`)은 동일 raw orderable에서 **대기주문 예약을 차감**한다:
- KR: `orderable = max(0, raw − _get_kis_domestic_pending_buy_amount)` (`portfolio_cash.py:203-210`)
- US: `orderable = max(0, raw − _get_kis_overseas_pending_buy_amount_usd)` (`portfolio_cash.py:274-282`)
- 둘 다 예약 차감 실패 시 raw orderable로 fallback(경고 로그).

즉 "live가 프리체크를 안 함"이 아니라(전제 정정 완료), **precheck의 잔고 소스가 pending-order 현금 예약을 미반영**하는 것이 진짜 갭. mock은 `_check_balance_and_warn`에서 ROB-341 shadow-exposure(`buy_reserved_amount`)로 예약을 별도 차감하므로 이미 예약-인지.

## 기대 동작

live 매수 precheck가 `get_available_capital`이 노출하는 것과 **동일한 예약-차감 후 orderable**을 사용해, orderable=0(예약됨)이면 dry_run에서 insufficient를 잡는다.

## 설계 (브레인스토밍 Approach A + live KR·US)

### 변경 표면 (read-only, migration 0, broker/order/watch/order-intent mutation 없음)

- `app/mcp_server/tooling/order_validation.py` — `_get_balance_for_order` live 분기 + 헬퍼

precheck는 KIS **조회만**(주문/ledger mutation 없음) → ROB-409/407 accepted-only ledger 경계에 무접근.

### 순환 import 확인

`order_validation.py`는 이미 `portfolio_cash`에서 import 중(`:22-27` extract_usd_orderable_from_row / select_usd_row_for_us_order). `portfolio_cash`는 `order_validation`을 import하지 않음 → 순환 없음, 모듈-레벨 import 안전.

### Unit 1 — precheck를 단일 소스(get_cash_balance_impl)로 정합

헬퍼:
```python
async def _live_kis_orderable(account_token: str) -> float:
    """live KIS 예약-차감 orderable (단일 소스 = get_available_capital의 소스).

    account_token: "kis_domestic" (KRW) | "kis_overseas" (USD).
    get_cash_balance_impl이 raw orderable에서 대기주문 예약을 차감(실패 시 raw
    fallback)하므로 precheck가 get_available_capital과 동일 값을 본다.
    """
    result = await get_cash_balance_impl(account=account_token, is_mock=False)
    for acc in result.get("accounts", []):
        if acc.get("account") == account_token:
            return float(acc.get("orderable") or 0.0)
    raise RuntimeError(f"{account_token} orderable not found in cash balance")
```

`_get_balance_for_order` 수정:
- `equity_kr` + **live**(`is_mock=False`) → `return await _live_kis_orderable("kis_domestic")`
- `equity_us` + **live** → `return await _live_kis_orderable("kis_overseas")`
- **mock 분기 전부 무변경**: equity_kr mock은 `inquire_domestic_cash_balance` 그대로; equity_us mock은 ROB-417 조기 가드가 `_check_balance_and_warn`에서 선제 차단(여기 미도달). mock 예약은 ROB-341 shadow-exposure가 담당 → **이중차감 회피**.
- **crypto 무변경**.

`get_cash_balance_impl(account=...)`는 strict_mode라 KIS 실패 시 RuntimeError를 raise → live `_check_balance_and_warn`는 예외를 re-raise(기존 동작과 일관).

import 추가: `from app.mcp_server.tooling.portfolio_cash import get_cash_balance_impl`.

## 테스트 (TDD)

`tests/`의 order_validation 단위 테스트(monkeypatch로 `order_validation.get_cash_balance_impl` 스텁):

1. `_get_balance_for_order("equity_kr", is_mock=False)` → get_cash_balance_impl을 `account="kis_domestic"`로 호출하고 그 accounts의 예약-차감 `orderable`(예: 0.0)을 반환.
2. `_get_balance_for_order("equity_us", is_mock=False)` → `account="kis_overseas"` 호출, USD orderable 반환.
3. **repro 재현·해소**: `_check_balance_and_warn(equity_us, side=buy, is_mock=False, order_amount>0)`에서 get_cash_balance_impl가 orderable=0(예약됨) 반환 → `dry_run=True`면 warning(insufficient USD), `dry_run=False`면 error.
4. **mock 미델리게이트**: `_get_balance_for_order("equity_kr", is_mock=True)`는 get_cash_balance_impl을 호출하지 않고 기존 `inquire_domestic_cash_balance` 경로(monkeypatch spy로 get_cash_balance_impl 미호출 검증).
5. **crypto 무변경**: `_get_balance_for_order("crypto", is_mock=False)`는 upbit 경로(기존).

## 안전 경계 / Non-goals

- precheck read-only(KIS 조회만) → ledger/주문 mutation 없음, ROB-409/407 경계 무접근.
- 예약차감 실패 시 raw orderable fallback(get_cash_balance_impl 로직 상속 — 과차단 회피).
- mock 경로 무변경(ROB-341 shadow-exposure가 mock 예약 담당, 이중차감 회피).
- dry_run이 orderable 검증 못하면 success 위장 금지(insufficient 경고/에러).
- migration 0, broker/order/watch/order-intent mutation 없음.
- `current_price`가 입력 limit price echo인지(이슈 #3 부차)는 별도 표면 — 범위 밖.
- precheck가 KIS 호출 1건(margin) → 2건(margin + pending-orders)으로 늘어남(get_cash_balance_impl 내부). 정확성 trade-off로 수용.
