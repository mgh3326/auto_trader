# Toss Live Smoke Runbook (ROB-539)

This runbook is the operator gate for activating Toss Securities live order
support. Toss has no mock or sandbox lane, so the integration smoke is a real
small order lifecycle: preflight, dry-run preview, confirmed one-share limit
buy, immediate cancel, and ledger reconcile.

Do not use this runbook to clear production activation by itself. ROB-539 is
under `high_risk_change`, `needs_stronger_model_review`, and
`hold_for_final_review`. A stronger-model or CTO review must clear the code,
the operator evidence, and the account setup before live operational use.

## Safety Boundaries

- No command in this runbook should print Toss secrets. Record only variable
  names, order ids, ledger ids, statuses, and redacted broker errors.
- `--order-test` is dry-run only. It calls the Toss order tool with
  `dry_run=True` and emits a JSON preview. It does not submit a broker order.
- `--confirm` is the only CLI mode that submits a Toss order. It requires both
  `TOSS_API_ENABLED=true` and `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true`.
- `--confirm` always sends a one-side `BUY LIMIT` order with operator-supplied
  `--market`, `--symbol`, `--quantity`, and `--price`. The CLI has no default
  live symbol, quantity, or price.
- The confirm flow retries the same `clientOrderId` once to verify broker
  idempotency. If Toss returns a different `orderId`, the CLI reports an
  anomaly, attempts to cancel every opened order id, and exits `2`.
- The ledger is idempotent on `client_order_id` (ROB-545). An idempotent retry
  that returns the same `orderId` replays the existing ledger row instead of
  hitting a UNIQUE violation, so the normal place → retry → cancel → reconcile
  sequence can reach exit `0`. A retry that returns a *different* `orderId`
  surfaces the new live order id even on the failure response, so finally-cancel
  can still cancel the duplicate.
- The confirm flow uses a finally-cancel pattern. If an order id is observed,
  cancel is attempted even when the idempotency retry or reconcile step fails.
- Reconcile is local bookkeeping. The live account source of truth remains Toss
  holdings, cash, and broker order detail.

## Prerequisites

1. Confirm the account is configured in Toss as:

   ```text
   투자자지시 거래소 = 통합(SOR)
   ```

   Without this, KR orders can fail with Toss 422
   `investor-exchange-not-integrated`.

2. Confirm the code branch includes ROB-531 and ROB-538:

   ```bash
   rg -n "toss_reconcile_orders|toss_live_order_ledger|TOSS_LIVE_ORDER_MUTATIONS_ENABLED" \
     app/mcp_server docs/runbooks scripts
   ```

3. Apply DB migrations before any live confirm:

   ```bash
   uv run alembic upgrade head
   ```

4. Confirm Toss API credentials are present in the operator environment. Print
   only the key NAMES that are set — never the values:

   ```bash
   env | cut -d= -f1 | rg '^TOSS_API_(ENABLED|CLIENT_ID|CLIENT_SECRET|ACCOUNT_SEQ|BASE_URL)$'
   ```

5. Confirm buying power is sufficient for the chosen one-share order. The
   account previously had KRW buying power `0`, so fund the account or choose a
   valid US one-share order only after checking available cash.

6. Avoid the 09:00-09:10 KST opening burst. Toss order rate limit is lower
   during peak load (`ORDER` group 3 TPS), and this smoke should not compete
   with open auction traffic.

## CLI Modes

### Default-disabled

```bash
uv run python -m scripts.toss_live_smoke
```

Expected:

```text
Toss live smoke disabled: pass --preflight, --order-test, or --confirm
```

Exit code: `0`. No HTTP order mutation and no DB mutation.

### Preflight

```bash
TOSS_API_ENABLED=true \
  uv run python -m scripts.toss_live_smoke --preflight --symbol 005930
```

Expected:

```text
Toss preflight ok: accounts=<n> holdings=<n> prices=<n>
```

This performs signed read calls for account, holdings, and prices. It is the
first check that credentials and account routing work.

### Order Test

Use the exact order candidate intended for confirm:

```bash
TOSS_API_ENABLED=true \
  uv run python -m scripts.toss_live_smoke --order-test \
    --market kr \
    --symbol 005930 \
    --quantity 1 \
    --price 50000
```

Expected JSON event:

```json
{"event":"toss_order_test_preview","success":true}
```

The actual output includes the full `payload_preview`. Confirm:

- `payload_preview.side` is `BUY`.
- `payload_preview.orderType` is `LIMIT`.
- `payload_preview.quantity` and `payload_preview.price` match the operator
  decision.
- The notional is comfortably inside available cash.

### Confirm

Run only after the hold is cleared for the smoke attempt and a human operator
has checked account cash, symbol, quantity, price, and market session:

```bash
TOSS_API_ENABLED=true \
TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true \
  uv run python -m scripts.toss_live_smoke --confirm \
    --market kr \
    --symbol 005930 \
    --quantity 1 \
    --price 50000
```

Expected event sequence:

```text
toss_confirm_place
toss_confirm_idempotency_retry
toss_confirm_cancel
toss_confirm_reconcile_preview
toss_confirm_reconcile_apply
```

Exit codes:

| Code | Meaning | Operator action |
|---|---|---|
| `0` | Place/idempotency/cancel/reconcile completed without anomaly | Continue verification below |
| `1` | Initial place failed before an accepted order was observed | Inspect JSON error, no successful order id was recorded by the CLI |
| `2` | Cleanup, idempotency, cancel, or reconcile anomaly | Stop activation and perform manual broker cleanup |

## Ledger Verification

After `--confirm`, identify the emitted `order_id`, `client_order_id`, and
`ledger_id`. Then inspect the Toss ledger:

```sql
SELECT
  id,
  operation_kind,
  market,
  symbol,
  side,
  order_type,
  quantity,
  price,
  client_order_id,
  broker_order_id,
  original_order_id,
  replaced_by_order_id,
  status,
  broker_status,
  filled_qty,
  avg_fill_price,
  commission,
  tax,
  reconciled_at
FROM review.toss_live_order_ledger
WHERE broker_order_id = '<ORDER_ID>'
   OR original_order_id = '<ORDER_ID>'
   OR replaced_by_order_id = '<ORDER_ID>'
ORDER BY id;
```

Expected:

- The place row exists with `operation_kind='place'`.
- Send-time place did not create `review.trades`, trade journals, or realized
  PnL before reconcile evidence.
- A cancel audit row exists if Toss returned a replacement cancel order id.
- Reconcile moved rows according to broker evidence:
  - unfilled canceled order: `cancelled`, no journal side effect.
  - partial fill before cancel: filled delta booked, then `cancelled`.
  - anomaly: stop and inspect broker UI plus raw response.

For reconcile semantics, see
[`docs/runbooks/toss-live-order-reconcile.md`](./toss-live-order-reconcile.md).

## MCP Restart and Tool Visibility

After code deploy and env changes, restart the MCP process and confirm the
default profile exposes the Toss tools:

```bash
uv run python -m app.mcp_server.main
```

Expected tools in the default profile:

- `toss_preview_order`
- `toss_place_order`
- `toss_modify_order`
- `toss_cancel_order`
- `toss_get_order_history`
- `toss_get_positions`
- `toss_get_orderable_cash`
- `toss_reconcile_orders`

Keep `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=false` outside the controlled smoke
window until final review clears ongoing operational use.

## Portfolio Read Verification

When `TOSS_API_ENABLED=true`, confirm Toss holdings and cash appear in read
surfaces before treating the account as operational:

- MCP `get_holdings(account="toss")` returns `source="toss_api"`.
- MCP cash balance includes Toss KRW/USD buying power.
- Morning report cash summary no longer reports Toss as `API 미사용` when the
  API read succeeds.

If Toss API reads fail, keep order activation paused. Manual holdings are a
fallback for visibility, not proof that live order routing is safe.

## Troubleshooting

### `TOSS_API_ENABLED is not truthy`

The CLI exits `0` and does nothing. Set `TOSS_API_ENABLED=true` only in the
operator shell or process environment intended for the smoke.

### `TOSS_LIVE_ORDER_MUTATIONS_ENABLED is not truthy`

`--confirm` exits `0` and does nothing. This is the normal production-safe
state. Enable it only for the controlled smoke window.

### `investor-exchange-not-integrated`

The Toss account is not set to integrated SOR trading. Fix the account setting
in Toss, then rerun `--order-test` before any new `--confirm`.

### Stock warning guard blocks the order

Confirmed KR buys block on active liquidation-trading warnings. Choose a
different symbol or defer activation. Do not bypass the warning guard for the
smoke.

### Opposite pending order exists

Cancel or resolve the existing opposite-side open order in Toss first. The tool
blocks before POST because relying on broker 422 alone makes cleanup ambiguous.

### Idempotency anomaly

If the same `clientOrderId` returns a different `orderId`, the CLI exits `2`
and attempts to cancel all observed order ids. Verify in Toss UI that no order
remains open, record both order ids in Linear, and stop activation.

### Cancel failed or unknown

Use Toss UI or broker order history to cancel the open order manually. Record
the order id, broker status, and cleanup evidence. Do not rerun `--confirm`
until the account has zero unexpected open orders.

### Reconcile anomaly

Run the scoped reconcile preview again through MCP:

```text
toss_reconcile_orders(order_id="<ORDER_ID>", dry_run=True)
```

If it still reports `anomaly`, inspect `raw_response` in
`review.toss_live_order_ledger` and the Toss broker order detail. Do not mark
ROB-539 complete until the ledger accurately reflects broker evidence.

## Evidence Template

Paste this into the Linear issue or PR after the smoke. Do not include secrets.

```markdown
## ROB-539 Toss live smoke evidence

- Date/time (KST):
- Operator:
- Review clearance:
- Account SOR setting confirmed: yes/no
- Migration applied: `alembic upgrade head` yes/no
- Market/symbol/quantity/price:
- Preflight result:
- Order-test result:
- Confirm exit code:
- client_order_id:
- place order_id:
- cancel replacement_order_id:
- ledger rows checked:
- reconcile preview summary:
- reconcile apply summary:
- open orders after cleanup: 0 / not 0
- holdings/cash read verification:
- MCP restart/tool visibility:
- Follow-ups:
```

## ROB-668 NXT preflight

- **Read exposure (KR only):** `get_quote`, `search_symbol`, `analyze_stock_batch`
  each carry `nxt_tradable` (bool), `nxt_tradable_source` (`kr_symbol_universe`),
  `nxt_tradable_asof` (ISO, `toss_master_updated_at` else `updated_at`), and
  `nxt_tradable_stale` (bool). US/crypto payloads carry none of these.
- **Staleness:** the flag is refreshed by the operator sync
  (`scripts/sync_kr_symbol_universe.py`). `nxt_tradable_stale=true` means asof is
  missing or older than 2 days (`NXT_FLAG_STALE_AFTER`); re-run the sync before
  trusting eligibility during an NXT window.
- **Rollout gate `TOSS_NXT_PREFLIGHT_MODE ∈ {off, optional, warn, required}`**
  (default `warn`):
  - `off` — no preflight anywhere.
  - `optional`/`warn` — `toss_preview_order` appends `nxt_session_not_tradable`
    to `order_warnings` and returns a structured `nxt_preflight`; `toss_place_order`
    logs but does NOT block (`warn` logs a live-send advisory).
  - `required` — `toss_place_order` fail-closes with
    `{success:false, error_code:"nxt_session_not_tradable", session, alternatives}`
    before `client.place_order`.
- **Fail-open:** when the Toss market calendar is unavailable
  (`TOSS_API_ENABLED` off or fetch failure), `get_kr_toss_session_from_toss`
  returns `None`, the verdict is advisory (`block=false, advisory=true`), and
  KR trading is never frozen.
- **Alternatives** on a block: `retry_at_regular` (KRX regular session) and
  `route_via_kis` (KIS domestic order sets `EXCG_ID_DVSN_CD='SOR'` for
  NXT-eligible symbols; see `app/services/brokers/kis/domestic_orders.py`).
- **Belt-and-suspenders:** any preflight miss still surfaces the broker 422
  `market-not-supported-for-stock` as a typed
  `error_code:"nxt_session_not_tradable"` with the same alternatives.
- `route_request` is intentionally NOT session-aware (deterministic contract
  preserved).
