# KIS Live Order Reconcile (ROB-395)

## What changed
`kis_live_place_order(dry_run=False)` no longer pre-books fills/journals/realized_pnl
at send time. A live KR order is recorded to `review.kis_live_order_ledger` as
`accepted` (or `rejected`). Response now carries `broker_status` and `fill_recorded:false`.

Fills/journals/realized_pnl are applied only by **`kis_live_reconcile_orders`**, from
order-id-keyed `inquire_daily_order_domestic` evidence.

## Cancel / modify keep the ledger truthful
- `kis_live_cancel_order` (live KR, success) marks the matching ledger row
  `cancelled` immediately — a cancelled order never stays `accepted/pending`, so a
  later reconcile cannot re-book it.
- `kis_live_modify_order` (live KR, success) re-points the ledger row to the new
  odno issued by KIS 정정주문 (and updates price/quantity), so reconcile tracks the
  replacement instead of orphaning it.

These run only for live KR (`is_mock=False`); mock/US/crypto paths are untouched.

## Scope
KR domestic live only. US/overseas live and crypto keep the legacy immediate-record
path (same defect remains; tracked as follow-up — ROB-407).

## Reconcile workflow
1. Place order: `kis_live_place_order(..., dry_run=False)` → note `order_id` / `ledger_id`,
   `broker_status:"accepted"`.
2. After the broker fills (or you want to settle pending), dry-run reconcile:
   `kis_live_reconcile_orders(dry_run=True)` — preview verdicts (filled/partial/pending/cancelled),
   no DB writes.
3. Apply: `kis_live_reconcile_orders(dry_run=False)` — books confirmed fills + journals,
   marks unfilled/cancelled rows. Scope to one order with `order_id=...` if needed.

## Verdicts
- `filled` / `partial` — `review.trades` + journal mutation booked from broker `ccld_qty`/`ccld_unpr`. **Delta-idempotent (ROB-487)**: booking은 브로커 누적 체결량과 ledger의 기booking 수량의 델타만 기장하며, 델타가 0 이하이면 `noop_already_booked` (저널 재생성/이중 close 없음).
- `pending` — accepted, no fill yet; no-op (re-run later).
- `none` → `noop_no_evidence` — **(ROB-487 변경)** lookback 윈도우(주문일~오늘, 최대 90일)에서 체결 증거가 없으면 더 이상 `cancelled`로 마킹하지 않는다. 행은 open으로 남고 `requires_manual_review:true`가 표기된다. 증거 부재는 취소 증거가 아니다(fail-closed) — 전일 NXT 체결이 익일에 정상 booking되도록 보장.
- `expired` — KRX 마감을 지난 미체결 day order. reconcile이 `status="expired"`로 해소(영구 pending 방지). **Fail-closed**: 브로커가 주문을 live(접수/정상)로 보고하면 `expired`로 넘기지 않고 `pending` 유지(SOR 주문이 NXT 세션에서 살아있을 수 있음). 정확한 KIS 상태 문자열은 operator read-only smoke로 확정.
- `anomaly` — reconcile error; inspect `raw_response` / logs.

### requires_manual_review 행의 operator 종결 절차
90일 lookback에도 증거가 없는 행은 영구 open으로 남는다(정직한 미해소 표면).
operator가 브로커 HTS/체결내역에서 해당 odno의 최종 상태를 확인한 뒤에만 수동 종결:
체결 확인 시 `kis_live_reconcile_orders(order_id=...)` 재실행, 취소 확인 시 DB에서
`status='cancelled'` 수동 마킹(증거 스크린샷/사유를 Linear에 기록).

### ROB-487 false-cancel 행 복구 (1회성 backfill)
2026-06-10 이전의 today-only 윈도우 + NONE→cancelled 결함으로 잘못 취소 처리된 행 식별:
```sql
SELECT id, order_no, symbol, side, trade_date, reconciled_at
FROM review.kis_live_order_ledger
WHERE status = 'cancelled' AND reconciled_at IS NOT NULL AND filled_qty IS NULL
  AND trade_date < reconciled_at::date;  -- 익일 reconcile로 취소된 것
```
실제 체결 여부를 확인할 행들을 `status='accepted'`로 재개방 → `kis_live_reconcile_orders(dry_run=True)`
preview → `dry_run=False` 재실행 (90일 윈도우가 전일 체결 증거를 찾아 booking; trades insert는
`uq_review_trades_account_order`로 멱등).

## Routing / lifecycle visibility (ROB-476)

`place_order` 응답은 라우팅/만료 컨텍스트를 surface한다:
- `order_validity`: 항상 `"day"` (현재 day order만 지원; NXT/TIF는 ROB-463).
- `routing.requested_venue`/`note`: SOR auto-route (KRX; NXT-eligible).
- `expected_expiry`: 주문일 KRX 마감(15:30 KST) ISO 시각.
- `broker_exchange`: 브로커가 거래소 필드를 반환할 때만 표기(없으면 `null`, 날조 없음).

> **NXT 세션 이월**: SOR-routed day order가 KRX 마감 후 NXT에서 살아있는지는 KIS 동작에 의존하며 **operator 확정 필요**(미상). 그래서 만료 해소는 fail-closed. ROB-463(NXT venue 파라미터 추가)과 보완관계.

## Migration
Operator applies `alembic upgrade head` in prod (creates `review.kis_live_order_ledger`).
Migration for ROB-476 is 0 (non-breaking, backward compatible).

## Auto-reconcile (ROB-475)

수동 `kis_live_reconcile_orders(dry_run=False)` 반복을 피하려면 주기 자동 정산을
활성화한다. 둘 다 동일한 증거-게이트 커널을 호출하며 새 mutation 경로는 없다.

- **CLI (온디맨드/cron)**: `uv run python -m scripts.kis_live_auto_reconcile`
  (dry-run 기본), 실제 booking은 `--apply` — 단 `--apply`는 아래 2개 플래그가
  모두 켜져 있어야 동작(exit 2로 거부, 게이트 우회 불가).
- **Paused TaskIQ 태스크**: `kis_live.reconcile_periodic` — 기본 비활성.
  활성화에는 **(ROB-487) 2개 플래그가 모두** 필요:
  `KIS_LIVE_AUTO_RECONCILE_ENABLED=true` **그리고**
  `KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED=true` + cron 등록(robin-prefect-
  automations). 하나라도 미설정 시 `{"status":"paused"}`로 inert.
  SAFETY_REVIEW 플래그는 ROB-487 fail-closed semantics + delta-idempotent
  booking이 배포에 포함됐음을 operator가 확인한 뒤에만 켠다.

> **reconcile은 로컬 부기 레이어**(trade/journal/realized_pnl)다. 실계좌 진실은
> `get_holdings` / `get_available_capital`. reconcile 미실행은 실계좌에 영향을
> 주지 않으며, 로컬 리포트/성과추적만 비게 된다.
