# Watch Order Intent Ledger Runbook (ROB-103)

## Purpose

`review.watch_order_intent_ledger` is an audit ledger for **approval-required order-intent previews** emitted by the watch scanner. It records every intent the scanner attempts to build for `account_mode='kis_mock'` watches — successful previews, dedupe hits, and failures alike.

It is **append-only**, written exclusively by `app.services.watch_order_intent_service.WatchOrderIntentService`, and never triggers a broker call. The `account_mode` value is pinned to `kis_mock` via a CHECK constraint so a stray write cannot escalate to a live order.

ROB-103 introduces this ledger as the audit half of the watch order-intent MVP; the policy half lives in the existing Redis `watch:alerts:{market}` hash payload.

---

## Lifecycle states

| State | Meaning |
|---|---|
| `previewed` | The scanner built a ROB-100 `OrderPreviewLine` from the watch policy and persisted it. The watch hash field was deleted. |
| `failed` | The scanner attempted to build the intent but rejected it (cap, fx, qty_zero, validation). The watch hash field is **kept** so the operator can adjust the policy and retry. |

There is no other state in this MVP — broker submit, reconcile, fill, etc. are all out of scope and live on other ledgers (Alpaca paper, KIS mock execution).

### Idempotency model

A `previewed` row is unique per `(market, target_kind, symbol, condition_type, threshold_key, action, side, kst_date)` — enforced by the partial unique index `uq_watch_intent_previewed_idempotency` (PostgreSQL `WHERE lifecycle_state = 'previewed'`). The same `idempotency_key` may appear on any number of `failed` rows; failures do not block dedupe.

When a second trigger in the same KST day would write a duplicate `previewed` row, the service catches the `IntegrityError`, reads back the existing row, and returns `dedupe_hit` to the scanner — no new ledger row is created. The watch hash field is still deleted, since the existing row remains the source of truth for the day.

---

## Adding a watch with action policy

Use the extended `manage_watch_alerts add` MCP tool. Omitting the intent kwargs preserves the legacy notify-only behavior.

```text
manage_watch_alerts \
  action=add \
  market=kr symbol=005930 metric=price operator=below threshold=70000 \
  intent_action=create_order_intent side=buy quantity=1 max_notional_krw=1500000
```

Constraints:

- `intent_action=create_order_intent` is allowed only for `market=kr` or `market=us` and `condition_type ∈ {price_above, price_below}`. RSI / `trade_value` triggers stay notify-only.
- `notional_krw` is supported only for `market=kr`; for `market=us`, use `quantity` (positive integer shares).
- `max_notional_krw` is the **KRW-denominated** safety cap. For US watches the service multiplies `quantity * limit_price * usd_krw` and compares to the cap.
- `account_mode` is implicitly `kis_mock` for this MVP. There is no kwarg to widen it; future PRs will widen the matching rule.

---

## Reading the ledger

### MCP tools (read-only)

- `watch_order_intent_ledger_list_recent(market="kr", lifecycle_state="previewed", kst_date="2026-05-04", limit=20)`
- `watch_order_intent_ledger_get(correlation_id="...")`

`limit` is clamped to `[1, 100]` (default 20). Filters are optional and AND-combined.

### HTTP (read-only)

- `GET /trading/api/watch/order-intent/ledger/recent?market=&lifecycle_state=&kst_date=&limit=`
- `GET /trading/api/watch/order-intent/ledger/{correlation_id}`

Both surfaces require an authenticated user (the same auth dependency the rest of the trading API uses) and return the same row shape — the router `serialize_ledger_row` is the single source of truth, imported by the MCP tooling.

---

## Failure triage

If a row appears with `lifecycle_state='failed'`, the watch was **not** deleted and the operator is expected to act. Check `blocked_by`:

| `blocked_by` | What happened | Action |
|---|---|---|
| `max_notional_krw_cap` | `qty × limit_price × (FX for US)` exceeded the cap. | Lower `quantity` or raise `max_notional_krw`, then re-add the watch. |
| `fx_unavailable` | USD/KRW quote service was down at trigger time. | Retry next scan; investigate `app/services/exchange_rate_service.py` if the failure persists. |
| `qty_zero` | `notional_krw / limit_price` floored below 1 share. | Raise `notional_krw` or specify `quantity` explicitly. |
| `validation_error` | Should not happen at scan time. | Inspect the Redis payload for the watch — the policy is malformed. |

`detail` and `blocking_reasons` carry the structured context (input notional, evaluated KRW, FX rate, etc.).

---

## Hard rules

- Direct SQL `INSERT/UPDATE/DELETE` against `review.watch_order_intent_ledger` is forbidden. All writes go through `WatchOrderIntentService`. This mirrors the Alpaca paper ledger discipline.
- This ledger never authorizes a broker submit. ROB-103 explicitly excludes broker mutation. `account_mode='kis_mock'` and `execution_source='watch'` are pinned via CHECK constraints.
- Live-account intents are not supported in this MVP. Future PRs may widen the matching rule for additional `(market, account_mode)` combinations without changing the contract.

---

## Related references

- Spec: `docs/superpowers/specs/2026-05-04-rob-103-watch-order-intent-mvp-design.md`
- Initial plan: `docs/superpowers/plans/2026-05-04-rob-103-watch-order-intent-mvp.md`
- Finish plan: `docs/superpowers/plans/2026-05-04-rob-103-watch-order-intent-mvp-finish.md`
- ROB-100 contracts: `app/schemas/execution_contracts.py`
- Sibling ledger runbook: `docs/runbooks/alpaca-paper-ledger.md`
