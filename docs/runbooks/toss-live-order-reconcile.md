# Toss Live Order Reconcile (ROB-538)

## Contract

Toss live KR/US orders are recorded in `review.toss_live_order_ledger` at send time as accepted/rejected only. Send-time order placement never books `review.trades`, trade journals, or realized PnL.

The local bookkeeping layer is `toss_reconcile_orders`. The live-account source of truth remains Toss holdings, cash, and order detail.

## Workflow

1. Place the order with `toss_place_order(..., dry_run=False, confirm=True)`.
2. Confirm the response includes `ledger_id`, `broker_status="accepted"`, and `fill_recorded=false`.
3. Preview reconcile:

```bash
toss_reconcile_orders(dry_run=True)
```

4. Apply confirmed fills:

```bash
toss_reconcile_orders(dry_run=False)
```

5. Scope a single order when needed:

```bash
toss_reconcile_orders(order_id="ORDER_ID", dry_run=True)
toss_reconcile_orders(order_id="ORDER_ID", dry_run=False)
```

## Auto-reconcile (ROB-574)

수동 `toss_reconcile_orders(dry_run=False)` 반복을 피하려면 주기 자동 정산을
활성화한다. TaskIQ wrapper는 기존 증거-게이트 커널만 호출하며 새 booking 로직은
없다.

- **Paused TaskIQ 태스크**: `toss_live.reconcile_periodic` — worker에 등록되지만
  코드 내 `schedule=`은 없다. 외부 recurrence는 robin-prefect-automations에서
  등록한다.
- **Activation gates**: `TOSS_LIVE_AUTO_RECONCILE_ENABLED=true` **그리고**
  `TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED=true`가 모두 필요하다. 하나라도
  미설정 시 `{"status":"paused"}`로 inert.
- **권장 external cadence**: 장중 수 분 간격 reconcile + 장마감 후 sweep.
  정확한 cron은 운영 자동화 레포에서 관리한다.
- **Safety**: 배포만으로 자동 booking은 시작되지 않는다. cron 등록과 env flip은
  high-risk final review 이후 별도 operator 후속으로 진행한다.

## Status Semantics

- `PENDING`: no local booking.
- `PARTIAL_FILLED`: book the new filled delta and keep the row `partial`.
- `FILLED`: book the new filled delta and mark `filled`.
- `CANCELED` with `filledQuantity > 0`: book the new filled delta and mark `cancelled`.
- `CANCELED` with `filledQuantity == 0`: mark `cancelled`, no journal side effects.
- `REPLACED` with `filledQuantity > 0`: book the new filled delta and mark the original row `replaced`; the replacement row remains reconcilable.
- `CANCEL_REJECTED` / `REPLACE_REJECTED`: record the rejected operation row and keep the original order open.

## Replacement Chain Notes

Successful modify/cancel requests record the replacement `orderId` in
`replaced_by_order_id`, but the original order row is not locally marked
terminal at request time. Reconcile must still fetch the original order detail
because Toss can report partial fills on the original order before it reaches
`REPLACED` or `CANCELED`.

Cancel-operation rows are audit rows, but they stay reconcilable until their
single-order detail resolves. If Toss returns `CANCEL_REJECTED` or
`REPLACE_REJECTED`, reconcile marks the replacement operation row rejected and
clears the original row's replacement link so the original order remains open.

## 403 / non-JSON Manual Review

`toss_reconcile_orders` fetches broker evidence with `GET /orders/{orderId}`.
When a GET order lookup returns `403` with a non-JSON body, the Toss client
force-reissues the OAuth token once and retries the same GET. If the retry still
fails, reconcile fails closed:

- the tool response returns `verdict="anomaly"`, `action="requires_manual_review"`,
  and structured `error_details`;
- `review.toss_live_order_ledger.status` becomes `anomaly`;
- `requires_manual_review=true`, `manual_review_reason`, and
  `last_reconcile_error` are persisted for operator lookup.

Mutation POSTs (`place`, `modify`, `cancel`) do not use this new 403 retry path.
They must not be repeated implicitly because a retry can create duplicate live
order side effects. Rate-limit (`429`) responses continue to use backoff and do
not trigger token reissue loops.

## Manual Review Query

```sql
SELECT
    id,
    market,
    symbol,
    broker_order_id,
    operation_kind,
    status,
    manual_review_reason,
    last_reconcile_error,
    updated_at
FROM review.toss_live_order_ledger
WHERE requires_manual_review IS TRUE
ORDER BY updated_at DESC, id DESC;
```

For each row, verify the Toss broker UI/API order detail before booking a fill,
closing the row, or resetting it for another reconcile attempt. Do not infer a
cancel or fill from a missing/failed order-detail response.

## US FX PnL Split

Toss `GET /orders/{orderId}` execution does not include fill-time FX fields. For
US orders only, reconcile captures the current USD/KRW quote from
`exchange_rate_service` when the fill is booked:

- buy reconcile stores `buy_fx_rate`;
- sell reconcile stores `sell_fx_rate`;
- closed FIFO journal lots store `security_pnl_usd`, `security_pnl_krw`, `fx_pnl_krw`, and `total_pnl_krw`;
- automatic values use `fx_rate_source='reconcile_spot'` and `fx_pnl_accuracy='approximate'`.

Legacy lots with no captured buy FX cannot produce automatic FX PnL. They remain
`fx_pnl_accuracy='unavailable'` with null FX PnL fields until the operator
supplies exact values through
`modify_journal_entry(..., fx_rate_source='manual', fx_pnl_accuracy='exact')`.

```text
security_pnl_usd = sell_notional_usd - buy_notional_usd
security_pnl_krw = security_pnl_usd * sell_fx_rate
fx_pnl_krw = buy_notional_usd * (sell_fx_rate - buy_fx_rate)
total_pnl_krw = security_pnl_krw + fx_pnl_krw
```

## Operational Hold

Keep `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=false` until ROB-539 live smoke and stronger-model/CTO review clear this path. This feature changes live-order bookkeeping and must stay under `hold_for_final_review` until cleared.

## Fill Notifications (ROB-576)

When `TOSS_FILL_NOTIFY_ENABLED=true`, `toss_reconcile_orders(dry_run=False)` sends a fill notification after a new fill delta is durably booked. Dry runs never notify. Re-running reconcile for an already-booked quantity does not notify because the existing delta-idempotency guard returns `noop_already_booked`.

Notification routing:

- `market="kr"` → `DISCORD_WEBHOOK_KR`
- `market="us"` → `DISCORD_WEBHOOK_US`
- Telegram fallback uses the existing `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` settings.

Toss fill notifications intentionally use `enrichment=None`. The existing KR/US fill enrichment reads KIS account state and can display the wrong position/PnL for Toss fills if the same symbol is also held in KIS.

## Auto-Reconcile (ROB-576 PR2)

The optional TaskIQ task `toss_live.reconcile_periodic` is shipped without an in-repo schedule and returns `{"status": "paused"}` until both gates are true:

- `TOSS_LIVE_AUTO_RECONCILE_ENABLED=true`
- `TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED=true`

When enabled, the task calls `toss_reconcile_orders_impl(dry_run=False)`. This still does not place, modify, or cancel live orders; it only books confirmed broker evidence into local trades/journals and triggers fill notifications if `TOSS_FILL_NOTIFY_ENABLED=true`.

Recommended initial external cadence: 1-5 minutes. Start at 5 minutes unless there is an operator need for faster Discord latency, then tighten after watching Toss API rate-limit and OAuth behavior.
