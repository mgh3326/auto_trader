# Binance Spot Demo Smoke (ROB-298) — Runbook

**Scope.** Operator runbook for the Binance Spot **Demo** lane
(`demo-api.binance.com`), which is the canonical mock-trading backend
for Spot crypto under ROB-298. Covers all five CLI modes — including
the **confirmed** order submission that ROB-298 PR 1 wires up — plus
post-run ledger verification and manual reconciliation.

**Locked design decisions** for ROB-298 live in the Linear issue
comment `d258c471-3202-444b-901b-c127f3ee44af` (env namespace, 10 USDT
cap, LOT_SIZE floor sizing, ledger lifecycle, host allowlist, default-
disabled). When in doubt, that comment wins.

**Sibling lane.** Binance USD-M Futures Demo
(`demo-fapi.binance.com`) is now available under ROB-298 PR 2 — see
`docs/runbooks/binance-futures-demo-smoke.md`. The futures lane has an
independent env namespace (`BINANCE_FUTURES_DEMO_*`), host allowlist
(`FUTURES_DEMO_HOSTS`), and execution client
(`BinanceFuturesDemoExecutionClient`); the two lanes share only the
`binance_demo_order_ledger` table via the `product` discriminator
(`spot` vs `usdm_futures`).

---

## 1. Lane boundaries at a glance

| Lane | Host | Env namespace | Status in this repo |
|---|---|---|---|
| **Spot Demo** | **`demo-api.binance.com`** | **`BINANCE_SPOT_DEMO_*`** | **Active — canonical Spot mock lane (ROB-298)** |
| Public market data | `api.binance.com`, `data-api.binance.vision`, etc. | (none — unsigned) | Active (read-only ingest) |
| Spot Testnet | `testnet.binance.vision` | _(removed)_ | **Removed in ROB-298.** Runtime + tests + runbook deleted. `BINANCE_TESTNET_*` env vars are inert. |
| USD-M Futures Demo | `demo-fapi.binance.com` | `BINANCE_FUTURES_DEMO_*` | Active — sibling lane added in ROB-298 PR 2; see `docs/runbooks/binance-futures-demo-smoke.md`. |
| Live / mainnet trading | `api.binance.com`, `fapi.binance.com` | _(none)_ | **Refused fail-closed.** Live trading is not a feature of this repo. |

Hard invariants enforced at the transport / signer layer:

* The Spot Demo HTTP client only ever talks to hosts in
  `SPOT_DEMO_HOSTS`. Any attempt to route a signed request to
  `api.binance.com`, `fapi.binance.com`, `testnet.binance.vision`, etc.
  raises `BinanceSpotDemoCrossAllowlistViolation` /
  `BinanceLiveHostBlocked` before the socket opens.
* `BINANCE_TESTNET_*` env vars **do not** activate the Spot Demo lane.
  Setting them has zero runtime effect (the testnet runtime no longer
  exists). A regression test asserts this.
* No scheduler / TaskIQ / Prefect / cron / Hermes wiring touches the
  Spot Demo execution client. The smoke CLI is the **only** path that
  produces real Demo orders, and only when the operator passes
  `--confirm`.

---

## 2. Env variables (`BINANCE_SPOT_DEMO_*`)

| Variable | Required when opted in? | Default | Notes |
|---|---|---|---|
| `BINANCE_SPOT_DEMO_ENABLED` | Yes (must be `true`) | unset → disabled | Master kill-switch. Default is fail-closed. |
| `BINANCE_SPOT_DEMO_API_KEY` | Yes (for signed modes) | — | API key from your Spot Demo account. Never logged. |
| `BINANCE_SPOT_DEMO_API_SECRET` | Yes (for signed modes) | — | API secret. Never logged. |
| `BINANCE_SPOT_DEMO_BASE_URL` | No | `https://demo-api.binance.com` | Validated against `SPOT_DEMO_HOSTS` at factory init; a non-demo host raises `BinanceSpotDemoCrossAllowlistViolation`. |
| `BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT` | No | `10` | Per-order notional cap. Hard enforced in sizing; you cannot override this above the env value at the CLI for `--confirm`. |

`env.example` carries these already (`BINANCE_SPOT_DEMO_*` block).
`BINANCE_TESTNET_*` are intentionally absent — they are inert.

---

## 3. Safety guarantees (read before `--confirm`)

These are properties of the code as it ships, not aspirational:

* **Default-disabled.** With `BINANCE_SPOT_DEMO_ENABLED` unset or
  falsey, every mode (including `--confirm`) exits 0 with a single
  "disabled" log line and zero HTTP / DB / Sentry side effects.
* **Per-call operator gate.** `BinanceSpotDemoExecutionClient.submit_order(...)`
  and `cancel_order(...)` require `confirm=True` on every call.
  Without it they return a `SpotDemoDryRunResult` and emit no HTTP.
  Only the `--confirm` CLI branch sets `confirm=True`.
* **Host allowlist + cross-allowlist guard.** Any transport pointed at
  testnet, public, or live hosts is rejected before signing. The
  allowlists `PUBLIC_HOSTS` / `SPOT_DEMO_HOSTS` are pairwise disjoint;
  there is no testnet allowlist anymore.
* **10 USDT max notional cap.** The smoke CLI sizes every confirmed
  order against `--cap-usdt` (default `10`), bounded above by
  `BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT`. Exchange filters
  (`LOT_SIZE`, `MIN_NOTIONAL`) are validated locally **and** by
  `POST /api/v3/order/test` before any real placement.
* **LOT_SIZE floor sizing.** Quantity is **floored** to `stepSize`,
  never rounded up. If flooring drops the order below `MIN_NOTIONAL`,
  sizing **blocks** with `SizingBlocked` and the CLI exits 1 — it does
  not silently grow the order.
* **Ledger lifecycle writes.** `--confirm` writes a row per state
  transition into `binance_demo_order_ledger`:
  `planned → previewed → validated → submitted → filled → closed →
  reconciled`. Failures branch to `cancelled` or `anomaly`. The
  service is the only write surface — direct SQL writes are forbidden.
* **Secret redaction.** API key / secret never appear in logs,
  evidence lines, or ledger rows. Only `<first4>…<last2>` fingerprints
  and redacted broker payloads are emitted.

---

## 4. The five CLI modes

The entry point is `scripts/binance_spot_demo_smoke.py`. The mode
flags are mutually exclusive; omitting them all is the "default-
disabled / guidance" exit.

### 4.1 Default-disabled (no flags, no env)

```bash
uv run python -m scripts.binance_spot_demo_smoke
```

Result: exit 0, one log line ("spot demo disabled — set
`BINANCE_SPOT_DEMO_ENABLED=true` to opt in"), zero HTTP, zero DB
writes. **This is the production-safe default.** Setting only
`BINANCE_TESTNET_*` does not change this behavior.

### 4.2 `--plan-only` (no HTTP, no credentials)

```bash
BINANCE_SPOT_DEMO_ENABLED=true \
  uv run python -m scripts.binance_spot_demo_smoke \
    --plan-only \
    --symbol BTCUSDT --side BUY --order-type LIMIT \
    --quantity 0.0001 --price 50000
```

Emits a single JSON line with `event: "spot_demo_plan"` describing
the planned order shape. No httpx client is constructed. No ledger
writes. Use this to verify the planning / filter pipeline without
touching the Demo server.

### 4.3 `--preflight` (one signed GET, no orders)

```bash
BINANCE_SPOT_DEMO_ENABLED=true \
  BINANCE_SPOT_DEMO_API_KEY=$KEY \
  BINANCE_SPOT_DEMO_API_SECRET=$SECRET \
  uv run python -m scripts.binance_spot_demo_smoke --preflight
```

Sends exactly one signed `GET /api/v3/account` to
`demo-api.binance.com`. Stdout prints a redacted balance summary
(nonzero-asset count, account `canTrade` flag, key fingerprint).
Balance amounts are **not** logged. No DB writes, no ledger writes.

If the server rejects auth (`-2014`, `-2008`, `-1022`), the CLI exits
non-zero with `BinanceSpotDemoUnsupportedAuth`. The most common cause
is an Ed25519-only Demo account; report as a follow-up rather than
patching the signer.

### 4.4 `--order-test` (signed validation, no real order)

```bash
BINANCE_SPOT_DEMO_ENABLED=true \
  BINANCE_SPOT_DEMO_API_KEY=$KEY \
  BINANCE_SPOT_DEMO_API_SECRET=$SECRET \
  uv run python -m scripts.binance_spot_demo_smoke --order-test \
    --symbol BTCUSDT --order-type MARKET --cap-usdt 10
```

Sizes the order against `--cap-usdt`, then sends a signed
`POST /api/v3/order/test` (the Binance non-mutating validation
endpoint). If exchange filters fail, the CLI exits non-zero with the
broker reason and writes nothing to the ledger. If filters pass,
the CLI prints `order_test_ok` and exits 0. **No real order is
placed in this mode.**

### 4.5 `--confirm` (real BUY + close, ledger-recorded)

```bash
BINANCE_SPOT_DEMO_ENABLED=true \
  BINANCE_SPOT_DEMO_API_KEY=$KEY \
  BINANCE_SPOT_DEMO_API_SECRET=$SECRET \
  uv run python -m scripts.binance_spot_demo_smoke --confirm \
    --symbol BTCUSDT --order-type MARKET \
    --cap-usdt 10 --close-with SELL
```

This is the only mode that places real Demo orders. It runs the
full lifecycle:

1. Resolve / create the `crypto_instruments` row for the symbol.
2. Generate a `client_order_id` (`rob298-<uuid4hex[:24]>`).
3. Write `planned` row to `binance_demo_order_ledger`.
4. Local preview → write `previewed`.
5. `POST /api/v3/order/test` → write `validated`.
6. `POST /api/v3/order` with `confirm=True` for BUY → write
   `submitted` and (if the server replies `FILLED`) `filled`.
7. Close-side:
   * `--close-with SELL` (default; valid for MARKET and LIMIT):
     submit a market SELL with a fresh child `client_order_id`,
     write its own `planned/previewed/validated/submitted/filled`
     lineage, then mark the parent `closed`.
   * `--close-with CANCEL` (LIMIT-only): call `cancel_order(...)`
     against the BUY, write `cancelled` on the parent.
8. `GET /api/v3/openOrders?symbol=...` → empty check → write
   `reconciled` (or `anomaly` if residual open orders exist).

Exit codes: `0` on a clean reconciled run, `1` on operator
misconfiguration (missing creds, invalid `--close-with CANCEL` with
non-LIMIT, etc.), `2` on runtime / reconciliation failures.

---

## 5. Confirmed smoke — step-by-step operator procedure

Run this whenever you need fresh evidence that the Spot Demo lane is
healthy end-to-end (e.g., on a clean cutover, after a dependency bump,
or as a smoke before reviewing a PR that touches the adapter).

### 5.1 Set env

```bash
export BINANCE_SPOT_DEMO_ENABLED=true
export BINANCE_SPOT_DEMO_API_KEY="…"        # from your Demo account
export BINANCE_SPOT_DEMO_API_SECRET="…"     # ditto; never commit
# Optional — defaults to https://demo-api.binance.com / 10:
# export BINANCE_SPOT_DEMO_BASE_URL=https://demo-api.binance.com
# export BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT=10
```

### 5.2 Verify intent (no HTTP)

```bash
uv run python -m scripts.binance_spot_demo_smoke --plan-only \
  --symbol BTCUSDT --order-type MARKET --quantity 0.0001
```

Confirm the printed JSON has `source: "spot_demo"`, `product: "spot"`,
and the expected symbol / side / qty. If anything is off, stop here.

### 5.3 Confirm credentials work (one signed GET)

```bash
uv run python -m scripts.binance_spot_demo_smoke --preflight
```

Expect a redacted balance summary line and a 0 exit. If you get
`BinanceSpotDemoUnsupportedAuth`, your Demo account likely uses
Ed25519 — file a follow-up rather than running `--confirm`.

### 5.4 Confirm exchange filters pass (signed, non-mutating)

```bash
uv run python -m scripts.binance_spot_demo_smoke --order-test \
  --symbol BTCUSDT --order-type MARKET --cap-usdt 10
```

Expect `order_test_ok symbol=BTCUSDT side=BUY qty=...` and exit 0.

### 5.5 Run the confirmed lifecycle

```bash
uv run python -m scripts.binance_spot_demo_smoke --confirm \
  --symbol BTCUSDT --order-type MARKET \
  --cap-usdt 10 --close-with SELL
```

Watch for the `[rob-298]` evidence lines (see §6). On a clean run the
process exits 0 after `reconciled cid=...`. Anything else needs §7.

### 5.6 Verify the ledger row

```sql
SELECT
  id,
  client_order_id,
  parent_client_order_id,
  side,
  order_type,
  lifecycle_state,
  broker_order_id,
  qty,
  price,
  venue_host,
  planned_at,
  previewed_at,
  validated_at,
  submitted_at,
  filled_at,
  closed_at,
  cancelled_at,
  reconciled_at
FROM binance_demo_order_ledger
WHERE client_order_id LIKE 'rob298-%'
ORDER BY id DESC
LIMIT 10;
```

You should see two rows per `--confirm SELL` run (parent BUY +
child SELL) or one row per `--confirm CANCEL` run, all with
`venue_host = 'demo-api.binance.com'` and the parent BUY ending in
`lifecycle_state = 'reconciled'`.

### 5.7 Confirm no stale open orders

The CLI already does this as the last step, but you can re-check
manually if you suspect drift:

```bash
uv run python -m scripts.binance_spot_demo_smoke --plan-only \
  --symbol BTCUSDT --order-type MARKET --quantity 0.0001 \
  >/dev/null    # only here to confirm the env still loads
```

…and then inspect Binance Demo's Order History UI for the symbol. The
ledger's `reconciled_at` row is the source of truth; the UI is just a
cross-check.

---

## 6. Expected redacted evidence shape

A clean `--confirm --close-with SELL` run emits these lines on stdout,
in order (UUIDs vary; secrets never appear):

```
[rob-298] planned cid=rob298-<uuid> product=spot symbol=BTCUSDT side=BUY qty=0.0001 venue=demo-api.binance.com
[rob-298] previewed cid=rob298-<uuid>
[rob-298] order_test_ok symbol=BTCUSDT
[rob-298] validated cid=rob298-<uuid>
[rob-298] submitted cid=rob298-<uuid> broker_order_id=<id> status=FILLED
[rob-298] filled cid=rob298-<uuid>
[rob-298] planned cid=rob298-<sell-uuid> product=spot symbol=BTCUSDT side=SELL qty=0.0001 venue=demo-api.binance.com
[rob-298] previewed cid=rob298-<sell-uuid>
[rob-298] validated cid=rob298-<sell-uuid>
[rob-298] submitted cid=rob298-<sell-uuid> broker_order_id=<sell-id> status=FILLED
[rob-298] filled cid=rob298-<sell-uuid>
[rob-298] closed cid=rob298-<uuid>
[rob-298] open_orders_check empty=true
[rob-298] reconciled cid=rob298-<uuid>
```

For `--close-with CANCEL` (LIMIT only), the SELL block is replaced by:

```
[rob-298] cancelled cid=rob298-<uuid> broker_status=CANCELED
[rob-298] open_orders_check empty=true
[rob-298] reconciled cid=rob298-<uuid>
```

Anomalies print `anomaly cid=... reason=...` and the CLI exits 2.
None of these lines contain the API key or secret.

---

## 7. Rollback / manual reconciliation

If a `--confirm` run dies between `submitted` and `reconciled` (network
blip, ctrl-C, server 5xx), the ledger will have a non-terminal row and
there may be an open order on Demo.

### 7.1 Inspect the ledger

```sql
SELECT *
FROM binance_demo_order_ledger
WHERE lifecycle_state NOT IN ('reconciled', 'cancelled', 'anomaly')
  AND created_at > NOW() - INTERVAL '1 day'
ORDER BY id DESC;
```

Grab the `client_order_id`(s) — you'll need them below.

### 7.2 Check the broker for open orders

From a Python shell (uses the same env you ran the smoke with):

```python
import asyncio
from app.services.brokers.binance.spot_demo import BinanceSpotDemoExecutionClient

async def main():
    async with BinanceSpotDemoExecutionClient.from_env() as client:
        open_orders = await client.get_open_orders(symbol="BTCUSDT")
        print(open_orders)

asyncio.run(main())
```

If `open_orders` is non-empty and includes your stranded
`client_order_id`, cancel it explicitly:

```python
await client.cancel_order(
    symbol="BTCUSDT",
    client_order_id="rob298-<your-cid>",
    confirm=True,
)
```

`confirm=True` is required — without it the call returns a
`SpotDemoDryRunResult` and changes nothing.

### 7.3 Reconcile the ledger

Bring the stranded row to a terminal state through the service (do
**not** UPDATE the table directly):

```python
from app.services.brokers.binance.demo.ledger.service import (
    BinanceDemoLedgerService,
)
# … construct the service with an async session …

await ledger.record_cancelled(
    client_order_id="rob298-<your-cid>",
    broker_status="CANCELED",
    now=...,
)
# Or, if you couldn't cleanly cancel and the order is unaccounted for:
await ledger.record_anomaly(
    client_order_id="rob298-<your-cid>",
    reason="manual reconciliation: <what happened>",
    now=...,
)
```

After the row is terminal, re-run `get_open_orders(symbol=...)` and
confirm it returns `[]`.

### 7.4 Document and escalate

If you had to reconcile manually:

* Post a comment on **Linear ROB-298** describing the stranded
  `client_order_id`, what state it was in, and how you resolved it.
* If the cause looks like a code bug (not a transient network issue),
  file a follow-up Linear issue and link it from the ROB-298 comment.

---

## 8. Verification suite

```bash
# Spot Demo unit + integration tests:
uv run pytest tests/services/brokers/binance/spot_demo/ -q

# Smoke CLI tests:
uv run pytest tests/scripts/test_binance_spot_demo_smoke.py -q

# Demo ledger tests:
uv run pytest tests/services/brokers/binance/demo/ -q

# Audit: no testnet imports remain anywhere:
uv run pytest tests/services/brokers/binance/demo/test_no_testnet_imports.py -q
```

All four suites are expected to pass on every PR that touches
`app/services/brokers/binance/`.

---

## 9. Linked decisions

Authoritative source for ROB-298 design choices (env namespace, 10
USDT cap, LOT_SIZE floor sizing, ledger lifecycle table, host
allowlist, default-disabled, removal of testnet) — Linear ROB-298
issue comment `d258c471-3202-444b-901b-c127f3ee44af`. Defer to that
comment if this runbook ever drifts from it.

---

## 10. Out of scope (deferred to ROB-298 follow-ups and beyond)

* **USD-M Futures Demo** (`demo-fapi.binance.com`) — landed as a
  sibling lane in ROB-298 PR 2. See
  `docs/runbooks/binance-futures-demo-smoke.md` for that runbook;
  there is nothing further to do from the Spot Demo side.
* **Scheduler / TaskIQ / Prefect / cron / Hermes activation** of the
  Spot Demo execution client. Tracked under ROB-292 and remains
  paused.
* **Production deploy, production DB migration, or live/mainnet
  routing.** Live trading is not a feature of this repo.
* **Touching Upbit, Alpaca, KIS, or any real-money broker path** from
  the Binance Demo lane. Cross-broker behavior is out of scope.
