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
4. 후보가 0건(모든 ledger 행이 terminal)이면 `"No open candidates (all ledger rows terminal)"` 메시지를 반환한다 — `Reconciled 0`이 누락으로 오인되지 않게 구분됨 (ROB-487 UX). 증거 조회 윈도우는 각 주문의 **주문일~오늘**(최대 90일 캡)이라 익일 reconcile도 전일 체결을 book할 수 있다.

## Verdicts
- `filled` / `partial` — `review.trades` + journal mutation booked from broker `ccld_qty`/`ccld_unpr`. **Delta-idempotent (ROB-487)**: booking은 브로커 누적 체결량과 ledger의 기booking 수량의 델타만 기장하며, 델타가 0 이하이면 `noop_already_booked` (저널 재생성/이중 close 없음).
- `pending` — accepted, no fill yet; no-op (re-run later). NXT 마감(20:00 KST) 전의 미체결 day order는 항상 pending 유지 (ROB-487).
- `cancelled` — 취소 증거(`cncl_yn` truthy, 또는 `orgn_odno`로 매칭되는 '매수취소'/'매도취소' 확인 행)가 있을 때만. ledger만 마킹, journal side-effect 없음. 행 부재(증거 없음)는 취소 증거가 아니다 — 아래 `none` 참고.
- `none` → `noop_no_evidence` — **(ROB-487 변경)** lookback 윈도우(주문일~오늘, 최대 90일)에서 체결 증거가 없으면 더 이상 `cancelled`로 마킹하지 않는다. 행은 open으로 남고 `requires_manual_review:true`가 표기된다. 증거 부재는 취소 증거가 아니다(fail-closed) — 전일 NXT 체결이 익일에 정상 booking되도록 보장.
- `expired` — **NXT 마감(20:00 KST) 이후** + 브로커 증거 `rjct_qty == ord_qty > 0` 인 미체결 day order (ROB-487 실측: 미체결 SOR day order는 EOD에 rjct_qty가 전량으로 채워짐). **Fail-closed**: 둘 중 하나라도 없으면 `pending` 유지(`rmn_qty > 0`이면 주문 생존). 실 TTTC8001R 행에는 `prcs_stat_name` / `rvse_cncl_dvsn_*` 키가 존재하지 않으므로(2026-06-10 라이브 프로브) 구 상태-토큰 분류는 폐기됨.
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
- `expected_expiry`: 주문일 NXT 마감(20:00 KST) ISO 시각 (ROB-487 — SOR day order는 NXT 세션까지 유효).
- `broker_exchange`: 브로커가 거래소 필드를 반환할 때만 표기(없으면 `null`, 날조 없음).

> **NXT 세션 이월 (ROB-487 실측 확정)**: SOR-routed day order는 KRX 마감 후에도
> NXT 세션(~20:00 KST)에서 체결될 수 있다 — 2026-06-09 KAI(047810) 15:31 주문이
> NXT 야간 체결로 booking됨. 미체결 SOR day order는 EOD에 `rjct_qty == ord_qty`
> (`tot_ccld_qty=0`)로 나타나고, 체결은 **주문일 윈도우**에서만 `tot_ccld_qty >
> 0`으로 나타난다(TTTC8001R은 주문일 기준 윈도우). 만료 해소는 여전히
> fail-closed(브로커 증거 + 20:00 시간 가드 둘 다 필요). ROB-463(NXT venue
> 파라미터 추가)과 보완관계.

## Migration
Operator applies `alembic upgrade head` in prod (creates `review.kis_live_order_ledger`).
Migration for ROB-476 is 0 (non-breaking, backward compatible).

## Auto-reconcile (ROB-475)

수동 `kis_live_reconcile_orders(dry_run=False)` 반복을 피하려면 주기 자동 정산을
활성화한다. 둘 다 동일한 증거-게이트 커널을 호출하며 새 mutation 경로는 없다.

- **CLI (온디맨드/cron)**: `uv run python -m scripts.kis_live_auto_reconcile`
  (dry-run 기본), 실제 booking은 `--apply` — 단 `--apply`는 아래 2개 플래그가
  모두 켜져 있어야 동작(exit 2로 거부, 게이트 우회 불가).
- **Paused TaskIQ 태스크**: `kis_live.reconcile_periodic` — worker에 등록되지만
  코드 내 `schedule=`은 없다. 외부 recurrence는 robin-prefect-automations에서
  등록한다. 활성화에는 **(ROB-487/ROB-574) 2개 플래그가 모두** 필요:
  `KIS_LIVE_AUTO_RECONCILE_ENABLED=true` **그리고**
  `KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED=true`. 하나라도 미설정 시
  `{"status":"paused"}`로 inert.
  SAFETY_REVIEW 플래그는 ROB-487 fail-closed semantics + delta-idempotent
  booking이 배포에 포함됐음을 operator가 확인한 뒤에만 켠다.

> **reconcile은 로컬 부기 레이어**(trade/journal/realized_pnl)다. 실계좌 진실은
> `get_holdings` / `get_available_capital`. reconcile 미실행은 실계좌에 영향을
> 주지 않으며, 로컬 리포트/성과추적만 비게 된다.

## realized_pnl 기준(basis) 라벨링 (ROB-544)

sell reconcile 응답의 `realized_pnl_pct`(별칭 `journal_pnl_pct`)는
**per-lot / journal-entry 기준(FIFO)** 이다. `realized_pnl_basis: "journal_entry"`
라벨이 함께 표면화되어 이를 명시한다.

- **FIFO lot 귀속 규칙**: `_close_journals_on_sell`은 active 저널을
  `created_at` 오름차순으로 가장 **오래된 lot부터** 소진한다. 매도는 더 새로
  추가한 물타기(averaging-down) lot보다 먼저 oldest lot에 귀속되며, PnL은 그
  lot의 `entry_price`에 대해 계산된다(계좌 평균단가가 아님).
  - 예: lot A(entry 100, 오래됨) + lot B(entry 90, 물타기)를 보유한 상태에서
    1주를 97.39에 매도 → FIFO가 lot A를 닫고 `realized_pnl_pct == -2.61%`
    (손실)를 보고한다. 계좌 평균이나 더 싼 lot B 기준(+8.21%)이 아니다.
- **계좌 평균 기준은 별도 진실**: `place_order` 프리뷰의 손익/평단,
  `get_holdings`, `get_available_capital`은 계좌 평균단가(`pchs_avg_pric`)
  기준이며 reconcile의 lot 기준과 의도적으로 다르다. reconcile은
  `account_avg_pnl_pct`를 계산하지 않는다(매도 전 계좌 평균이
  `kis_live_order_ledger`에 저장돼 있지 않고, 매도 후 `pchs_avg_pric`를 읽으면
  오해를 부른다 — fail-closed로 계산하지 않음).
- US/crypto `live_reconcile_orders`도 동일 라벨(`realized_pnl_basis` +
  `journal_pnl_pct` 별칭)을 surface한다(parity).

### ROB-474 retrospective와의 기준 일관성 (감사 노트)

`app/services/trade_journal/trade_retrospective_service.py`의
`_derive_realized_pnl_from_journal`(~:159)과 `build_retrospective_aggregate`
(~:342)는 **동일한 journal-entry lot 기준**을 소비한다. 현재 reconcile은
retrospective를 자동 기록하지 않으며, `save_trade_retrospective`는 수동 도구
(`trade_retrospective_tools.py:44`)다. 향후 reconcile→retrospective auto-emit을
도입한다면 반드시 `realized_pnl_basis: "journal_entry"` 라벨을 함께 carry하여
계좌 평균 기준과의 혼동을 방지해야 한다.

## Day-order expiry semantics (ROB-671)

`kis_live_place_order` 응답의 `expected_expiry`/`expiry_reason` 및 `kis_live_get_order_history` 행의 `expiry_reason` 은 접수 세션 × 매매구분으로 결정됩니다:

- **정규장 SELL 은 NXT 세션으로 연장**: 20:00 KST 까지 유효(SOR 현금매도 NXT carry). reason: `nxt_carry`.
- **정규장 BUY 는 보수적 기본값 20:00 KST**: reason: `regular_buy_conservative_20_00`. 정규장 매수 주문이 15:30에 사멸하는 현상은 세션 만료가 아니라 D+2 미결제(현금) 취소일 수 있어 원인이 미확정 상태입니다.
- **공격적 15:30 다운그레이드**: `unsettled_regular_buy_downgrade=True` 구현은 되어 있으나, operator 환경 변수 `KIS_REGULAR_BUY_UNSETTLED_EXPIRY_1530=true` (기본 off) 게이트 뒤에 있습니다. 라이브 측정으로 미결제 취소 원인이 확정된 후 활성화할 수 있습니다.
- **기타 세션**: premarket/nxt_after 주문은 20:00 KST (`nxt_carry`), off 창 접수 주문은 20:00 KST (`unknown_session`)으로 처리됩니다.
- **해외/US**: US history 행의 `expiry_reason` 은 `us_day_order` placeholder(NXT 없음)로 기록됩니다.

