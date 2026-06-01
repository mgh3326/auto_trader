# KIS Live Order Reconcile (ROB-395)

## What changed
`kis_live_place_order(dry_run=False)` no longer pre-books fills/journals/realized_pnl
at send time. A live KR order is recorded to `review.kis_live_order_ledger` as
`accepted` (or `rejected`). Response now carries `broker_status` and `fill_recorded:false`.

Fills/journals/realized_pnl are applied only by **`kis_live_reconcile_orders`**, from
order-id-keyed `inquire_daily_order_domestic` evidence.

## Scope
KR domestic live only. US/overseas live and crypto keep the legacy immediate-record
path (same defect remains; tracked as follow-up).

## Reconcile workflow
1. Place order: `kis_live_place_order(..., dry_run=False)` → note `order_id` / `ledger_id`,
   `broker_status:"accepted"`.
2. After the broker fills (or you want to settle pending), dry-run reconcile:
   `kis_live_reconcile_orders(dry_run=True)` — preview verdicts (filled/partial/pending/cancelled),
   no DB writes.
3. Apply: `kis_live_reconcile_orders(dry_run=False)` — books confirmed fills + journals,
   marks unfilled/cancelled rows. Scope to one order with `order_id=...` if needed.

## Verdicts
- `filled` / `partial` — `review.trades` + journal mutation booked from broker `ccld_qty`/`ccld_unpr`.
- `pending` — accepted, no fill yet; no-op (re-run later).
- `cancelled` — no daily-execution row; ledger marked cancelled; no journal side-effect.
- `anomaly` — reconcile error; inspect `raw_response` / logs.

## Migration
Operator applies `alembic upgrade head` in prod (creates `review.kis_live_order_ledger`).
