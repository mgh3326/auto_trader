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

## Operational Hold

Keep `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=false` until ROB-539 live smoke and stronger-model/CTO review clear this path. This feature changes live-order bookkeeping and must stay under `hold_for_final_review` until cleared.
