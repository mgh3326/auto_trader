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
