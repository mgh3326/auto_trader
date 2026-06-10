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
4. 후보가 0건(모든 ledger 행이 terminal)이면 `"No open candidates (all ledger rows terminal)"` 메시지를 반환한다 — `Reconciled 0`이 누락으로 오인되지 않게 구분됨 (ROB-487 UX). 증거 조회 윈도우는 각 주문의 **주문일~오늘**이라 익일 reconcile도 전일 체결을 book할 수 있다.

## Verdicts
- `filled` / `partial` — `review.trades` + journal mutation booked from broker `ccld_qty`/`ccld_unpr`.
- `pending` — accepted, no fill yet; no-op (re-run later). NXT 마감(20:00 KST) 전의 미체결 day order는 항상 pending 유지 (ROB-487).
- `cancelled` — 취소 증거(`cncl_yn` truthy, 또는 `orgn_odno`로 매칭되는 '매수취소'/'매도취소' 확인 행), 또는 주문일을 커버한 윈도우에서 일별체결 행 부재. ledger만 마킹, journal side-effect 없음. 윈도우가 주문일 커버를 증명 못 하면 noop (`noop_window_uncovered`).
- `expired` — **NXT 마감(20:00 KST) 이후** + 브로커 증거 `rjct_qty == ord_qty > 0` 인 미체결 day order (ROB-487 실측: 미체결 SOR day order는 EOD에 rjct_qty가 전량으로 채워짐). **Fail-closed**: 둘 중 하나라도 없으면 `pending` 유지(`rmn_qty > 0`이면 주문 생존). 실 TTTC8001R 행에는 `prcs_stat_name` / `rvse_cncl_dvsn_*` 키가 존재하지 않으므로(2026-06-10 라이브 프로브) 구 상태-토큰 분류는 폐기됨.
- `anomaly` — reconcile error; inspect `raw_response` / logs.

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
  (dry-run 기본), 실제 booking은 `--apply`.
- **Paused TaskIQ 태스크**: `kis_live.reconcile_periodic` — 기본 비활성.
  활성화: `KIS_LIVE_AUTO_RECONCILE_ENABLED=true` + cron 등록(robin-prefect-
  automations). 플래그 미설정 시 `{"status":"paused"}`로 inert.

> **reconcile은 로컬 부기 레이어**(trade/journal/realized_pnl)다. 실계좌 진실은
> `get_holdings` / `get_available_capital`. reconcile 미실행은 실계좌에 영향을
> 주지 않으며, 로컬 리포트/성과추적만 비게 된다.
