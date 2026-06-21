# Binance USD-M Futures Demo Smoke (ROB-298 PR 2) — Runbook

**Scope.** Operator runbook for the Binance USD-M Futures **Demo** lane
(`demo-fapi.binance.com`), which is the canonical mock-trading backend
for USD-M perpetual futures under ROB-298 PR 2. Covers all five CLI
modes — including the **confirmed** open + `reduceOnly` close
round-trip — plus post-run ledger verification and manual
reconciliation.

The Spot Demo sibling lane (`demo-api.binance.com`) is covered by
`docs/runbooks/binance-spot-demo-smoke.md`. The two lanes share the
`binance_demo_order_ledger` table via the `product` discriminator
(`spot` vs `usdm_futures`) but have independent env namespaces, host
allowlists, execution clients, and CLIs.

**Locked design decisions** for ROB-298 live in the Linear issue
comment `d258c471-3202-444b-901b-c127f3ee44af` (env namespace, 10 USDT
cap, leverage/position-mode contract, ledger lifecycle, host
allowlist, default-disabled). When in doubt, that comment wins.

---

## 1. Lane boundaries at a glance

| Lane | Host | Env namespace | Status in this repo |
|---|---|---|---|
| **USD-M Futures Demo** | **`demo-fapi.binance.com`** | **`BINANCE_FUTURES_DEMO_*`** | **Active — canonical USD-M Futures mock lane (ROB-298 PR 2)** |
| Spot Demo | `demo-api.binance.com` | `BINANCE_SPOT_DEMO_*` | Active — covered by separate runbook |
| Public market data | `api.binance.com`, `fapi.binance.com` public/unsigned read endpoints | (none — unsigned) | Active (read-only ingest) |
| USD-M Futures Testnet | `testnet.binancefuture.com` | _(removed)_ | **Refused fail-closed.** Deprecated path; transport rejects before signing. |
| Live / mainnet futures | `fapi.binance.com` (signed) | _(none)_ | **Refused fail-closed.** Live trading is not a feature of this repo. |
| Live / mainnet spot | `api.binance.com` (signed) | _(none)_ | **Refused fail-closed.** |

Hard invariants enforced at the transport / signer layer:

* The Futures Demo HTTP client only ever signs requests against hosts
  in `FUTURES_DEMO_HOSTS = {"demo-fapi.binance.com"}`. Any attempt to
  route a signed request to `fapi.binance.com`,
  `testnet.binancefuture.com`, `api.binance.com`,
  `demo-api.binance.com`, etc. raises
  `BinanceFuturesDemoCrossAllowlistViolation` /
  `BinanceLiveHostBlocked` before the socket opens.
* `FUTURES_DEMO_HOSTS` and `SPOT_DEMO_HOSTS` are **disjoint**. A Spot
  Demo client cannot accidentally talk to demo-fapi, and the Futures
  Demo client cannot accidentally talk to demo-api. Cross-allowlist
  guards assert this on every signed request.
* `BINANCE_SPOT_DEMO_*` and `BINANCE_TESTNET_*` env vars **do not**
  activate the Futures Demo lane. Setting them has zero effect on the
  futures execution client.
* No scheduler / TaskIQ / Prefect / cron / Hermes wiring touches the
  Futures Demo execution client. The smoke CLI is the **only** path
  that produces real Demo futures orders, and only when the operator
  passes `--confirm`.

---

## 2. Env variables (`BINANCE_FUTURES_DEMO_*`)

| Variable | Required when opted in? | Default | Notes |
|---|---|---|---|
| `BINANCE_FUTURES_DEMO_ENABLED` | Yes (must be `true`) | unset → disabled | Master kill-switch. Default is fail-closed. |
| `BINANCE_DEMO_API_KEY` | Yes¹ (for signed modes) | — | Canonical shared Demo key. Used by Spot AND Futures. Never logged. |
| `BINANCE_DEMO_API_SECRET` | Yes¹ (for signed modes) | — | Canonical shared Demo secret. Never logged. |
| `BINANCE_FUTURES_DEMO_API_KEY` | No (override) | — | Optional Futures-only override; wins over canonical when set. |
| `BINANCE_FUTURES_DEMO_API_SECRET` | No (override) | — | Optional Futures-only override. |
| `BINANCE_FUTURES_DEMO_BASE_URL` | No | `https://demo-fapi.binance.com` | Validated against `FUTURES_DEMO_HOSTS` at factory init; a non-demo-fapi host raises `BinanceFuturesDemoCrossAllowlistViolation`. |

¹ Credential resolution (ROB-302): set EITHER the canonical
`BINANCE_DEMO_API_KEY`/`BINANCE_DEMO_API_SECRET` pair (shared by both Demo
lanes — set once, no duplication) OR the Futures-specific override pair. The
override wins when present. A half-set pair (key without secret, or vice versa)
fails closed — it is never completed from the canonical pair (prevents pairing a
Futures key with a canonical secret).

Resolution order for the Futures lane:
`BINANCE_FUTURES_DEMO_API_*` → `BINANCE_DEMO_API_*` → `BinanceFuturesDemoMissingCredentials`.

Isolation: a Spot-specific override (`BINANCE_SPOT_DEMO_API_*`) never resolves
for Futures — crossing happens only through the canonical pair. `--readiness`
evidence reports `credential_source` (`shared_demo_env` / `futures_demo_env`)
so you can confirm which pair was used without printing any value.

---

## 3. Safety guarantees (read before `--confirm`)

These are properties of the code as it ships, not aspirational:

* **Default-disabled.** With `BINANCE_FUTURES_DEMO_ENABLED` unset or
  falsey, every mode (including `--confirm`) exits 0 with a single
  "disabled" log line and zero HTTP / DB / Sentry side effects.
* **Per-call operator gate.** `BinanceFuturesDemoExecutionClient.submit_order(...)`
  and `cancel_order(...)` require `confirm=True` on every call.
  Without it they return a dry-run result and emit no HTTP. Only the
  `--confirm` CLI branch sets `confirm=True`.
* **Host allowlist + cross-allowlist guard.** Any transport pointed at
  live (`fapi.binance.com`), futures testnet
  (`testnet.binancefuture.com`), Spot Demo (`demo-api.binance.com`),
  or any other host is rejected before signing. The allowlists are
  pairwise disjoint with `SPOT_DEMO_HOSTS` and `PUBLIC_HOSTS`.
* **10 USDT max notional cap.** The smoke CLI sizes every confirmed
  order against `--cap-usdt` (default `10`). Exchange filters
  (`LOT_SIZE`, `MIN_NOTIONAL`) are validated locally **and** by
  `POST /fapi/v1/order/test` before any real placement.
* **Leverage pinned to 1x.** The smoke contract pins leverage to `1`.
  Before the open submit, the CLI calls
  `POST /fapi/v1/leverage` and asserts the server echo matches `1`.
  Any mismatch raises `BinanceFuturesDemoLeverageMismatch` and the CLI
  exits non-zero **before** any real order is placed.
* **One-way position mode required.** The CLI calls
  `GET /fapi/v1/positionSide/dual` and refuses to proceed if the
  account is in Hedge mode — raises
  `BinanceFuturesDemoHedgeModeBlocked`. Switch the Demo account to
  One-way on the Binance Demo Futures console before running
  `--confirm`.
* **`reduceOnly` contract.** Open-side submits **never** carry
  `reduceOnly`. Close-side submits **always** carry `reduceOnly=true`.
  This is enforced at the execution client (defense in depth — the
  CLI sets it, and the client refuses to flip it).
* **Symbol allowlist.** Default-allowed: `XRPUSDT`. Fallbacks
  available via `--allow-symbol`: `DOGEUSDT`, `SOLUSDT`. `BTCUSDT` is
  **excluded** unconditionally — its MIN_NOTIONAL (~50 USDT) exceeds
  the 10 USDT cap, and `--allow-symbol BTCUSDT` is rejected.
* **Post-close reconciliation gate.** After the close, the CLI
  asserts **both** `GET /fapi/v1/openOrders?symbol=...` returns empty
  **and** `GET /fapi/v2/positionRisk?symbol=...` reports
  `positionAmt == 0`. Only when both hold is the parent row
  transitioned to `reconciled`. Either dirty → `anomaly`.
* **Ledger lifecycle writes.** `--confirm` writes a row per state
  transition into `binance_demo_order_ledger` with
  `product='usdm_futures'`:
  `planned → previewed → validated → submitted → filled → closed →
  reconciled`. Failures branch to `cancelled` or `anomaly`. The
  service is the only write surface — direct SQL writes are forbidden.
* **`status=NEW` reconciliation (ROB-305 §4).** A MARKET submit can
  return `status=NEW` even though the account later reflects the fill.
  A submit-response `NEW` is **never** treated as immediate success or
  failure, and a `submitted` row is **never** advanced straight to
  `closed` (the locked state machine forbids `submitted → closed`).
  Instead the CLI proves the fill before advancing the ledger, via —
  in order — the submit status, a **bounded** `GET /fapi/v1/order` poll
  (`_FILL_RECONCILE_MAX_POLLS`, no unbounded loop), then a non-flat
  `GET /fapi/v2/positionRisk` (the account reflecting the fill). Only
  with one of these does the row reach `filled` and then
  `closed`/`reconciled`. If a fill cannot be proven yet the account is
  flat with zero open orders, the close row is recorded as a **safe
  anomaly** and the run exits `2` — a benign final state is never
  reported as a clean success without fill evidence.
* **Secret redaction.** API key / secret never appear in logs,
  evidence lines, or ledger rows. Only `<first4>…<last2>` fingerprints
  and redacted broker payloads are emitted.

---

## 4. The six CLI modes

The entry point is `scripts/binance_futures_demo_smoke.py`. The mode
flags are mutually exclusive; omitting them all is the "default-
disabled / guidance" exit.

### 4.1 Default-disabled (no flags, no env)

```bash
uv run python -m scripts.binance_futures_demo_smoke
```

Result: exit 0, one log line ("futures demo disabled — set
`BINANCE_FUTURES_DEMO_ENABLED=true` to opt in"), zero HTTP, zero DB
writes. **This is the production-safe default.** Setting only
`BINANCE_SPOT_DEMO_*` or `BINANCE_TESTNET_*` does not change this
behavior.

### 4.2 `--plan-only` (no HTTP, no credentials)

```bash
BINANCE_FUTURES_DEMO_ENABLED=true \
  uv run python -m scripts.binance_futures_demo_smoke \
    --plan-only \
    --symbol XRPUSDT --side BUY --cap-usdt 10
```

Emits a single JSON line with `event: "futures_demo_plan"` describing
the planned order shape (symbol, side, notional, leverage=1,
reduce_only=false on open). No httpx client is constructed. No ledger
writes. Use this to verify the planning / filter pipeline without
touching the Demo server. Excluded symbols (`BTCUSDT`) are rejected
here with `event: "futures_demo_plan_rejected"`.

### 4.3 `--preflight` (signed reads, no orders)

```bash
BINANCE_FUTURES_DEMO_ENABLED=true \
  BINANCE_FUTURES_DEMO_API_KEY=$KEY \
  BINANCE_FUTURES_DEMO_API_SECRET=$SECRET \
  uv run python -m scripts.binance_futures_demo_smoke --preflight
```

Sends signed `GET /fapi/v2/account` and `GET /fapi/v1/positionSide/dual`
to `demo-fapi.binance.com`. Stdout prints a redacted summary
(`canTrade`, `totalWalletBalance` presence flag, position mode = one-way
or hedge, key fingerprint). Balance amounts are **not** logged. No DB
writes, no ledger writes, no orders.

If the server reports Hedge position mode, the CLI exits non-zero with
`BinanceFuturesDemoHedgeModeBlocked` — switch to One-way on the Demo
console before running `--confirm`.

If the server rejects auth, the CLI exits non-zero with the broker
reason. The most common cause is using a Spot Demo key against
demo-fapi (or vice versa) — Demo credentials are namespace-scoped on
Binance's side as well.

### 4.4 `--order-test` (signed validation, no real order)

```bash
BINANCE_FUTURES_DEMO_ENABLED=true \
  BINANCE_FUTURES_DEMO_API_KEY=$KEY \
  BINANCE_FUTURES_DEMO_API_SECRET=$SECRET \
  uv run python -m scripts.binance_futures_demo_smoke --order-test \
    --symbol XRPUSDT --side BUY --cap-usdt 10
```

Sizes the order against `--cap-usdt`, then sends a signed
`POST /fapi/v1/order/test` (the non-mutating validation endpoint). If
exchange filters fail, the CLI exits non-zero with the broker reason
and writes nothing to the ledger. If filters pass, the CLI prints
`event: "futures_demo_order_test"` with `ok=true` and exits 0. **No
real order is placed in this mode.**

### 4.5 `--confirm` (real BUY open + reduceOnly SELL close, ledger-recorded)

```bash
BINANCE_FUTURES_DEMO_ENABLED=true \
  BINANCE_FUTURES_DEMO_API_KEY=$KEY \
  BINANCE_FUTURES_DEMO_API_SECRET=$SECRET \
  uv run python -m scripts.binance_futures_demo_smoke --confirm \
    --symbol XRPUSDT --side BUY --cap-usdt 10 --leverage 1
```

This is the only mode that places real Demo futures orders. It runs
the full lifecycle:

1. Verify position mode = One-way (`GET /fapi/v1/positionSide/dual`).
2. Set leverage to 1 and verify echo
   (`POST /fapi/v1/leverage` → server echoes `leverage=1`).
3. Resolve / create the `crypto_instruments` row for
   `(binance, usdm_futures, symbol)`.
4. Generate a `client_order_id` (`rob-298-fut-<uuid4hex[:16]>`).
5. Write `planned` row to `binance_demo_order_ledger` with
   `product='usdm_futures'`.
6. Local preview → write `previewed`.
7. `POST /fapi/v1/order/test` (open, `reduceOnly=false`) → write
   `validated`.
8. `POST /fapi/v1/order` (open, `reduceOnly=false`, `confirm=True`)
   → write `submitted`, then **reconcile the fill** (ROB-305 §4): if
   the submit status is not `FILLED`, poll `GET /fapi/v1/order`
   (bounded) before recording `filled`.
9. `GET /fapi/v2/positionRisk?symbol=...` → verify position amount is
   non-zero in the expected direction. A non-flat position is the third
   fill-evidence source: if order status never confirmed `FILLED`, the
   open is recorded `filled` from this account-state evidence (tagged
   `fill_evidence=position_risk_nonflat`) so the close keeps the legal
   `submitted → filled → closed` chain. A flat position here → `anomaly`
   (open did not take effect).
10. Close lineage (always `reduceOnly=true`):
    * Generate child `client_order_id`.
    * Write `planned/previewed` for the close child.
    * `POST /fapi/v1/order/test` (close, `reduceOnly=true`) → write
      `validated`.
    * `POST /fapi/v1/order` (close MARKET, `reduceOnly=true`,
      `confirm=True`) → write `submitted`, then reconcile the close
      fill the same way (submit status / bounded `GET /fapi/v1/order`).
    * Mark the parent `closed`.
11. Reconciliation:
    * `GET /fapi/v1/openOrders?symbol=...` → assert empty.
    * `GET /fapi/v2/positionRisk?symbol=...` → assert
      `positionAmt == 0`.
    * If both clean **and the close fill was proven** → write
      `reconciled` on the parent (and the close child). If both clean
      but the close fill could **not** be proven, the close child is
      written `anomaly` and the run exits `2` — a flat/clean account is
      never reported as success without fill evidence. Any drift (open
      orders or non-flat position) → `anomaly` with the reason.

Exit codes: `0` on a clean reconciled run, `1` on operator
misconfiguration (missing creds, hedge mode, excluded symbol,
leverage mismatch), `2` on runtime / reconciliation failures (including
an unprovable close fill on an otherwise-flat account).

### 4.6 `--readiness` (no HTTP, no credentials)

```bash
uv run python -m scripts.binance_futures_demo_smoke --readiness
```

Result: exits 0 if the `BINANCE_FUTURES_DEMO_*` environment quartet is correctly configured (present, truthy, and base_url host is in `FUTURES_DEMO_HOSTS`), or exits 1 if missing or misconfigured. Runs cleanly without any secrets, zero HTTP requests, and does not require credentials.

It explicitly ignores `BINANCE_SPOT_DEMO_*` and `BINANCE_TESTNET_*` variables.

At the end, it emits a structured non-secret `futures_demo_env_readiness` evidence event:
```json
{
  "event": "futures_demo_env_readiness",
  "source": "futures_demo",
  "venue": "binance",
  "product": "usdm_futures",
  "enabled_present": true,
  "enabled_truthy": true,
  "api_key_present": true,
  "api_secret_present": true,
  "base_url_present": true,
  "base_url_host": "demo-fapi.binance.com",
  "base_url_host_allowed": true,
  "missing": [],
  "ready": true
}
```

---

## 5. Confirmed smoke — step-by-step operator procedure

Run this whenever you need fresh evidence that the Futures Demo lane
is healthy end-to-end (e.g., on a clean cutover, after a dependency
bump, or as a smoke before reviewing a PR that touches the adapter).

### 5.1 Pre-conditions on the Binance Demo Futures console

Before any signed run:

1. The Demo account exists and Futures is enabled on it.
2. **Position mode is set to "One-way"** (not Hedge). This is a
   per-account setting on the Binance Demo Futures UI; the CLI will
   refuse to proceed otherwise.
3. The Demo account has a small USDT balance funded
   (`totalWalletBalance > 0`).

### 5.2 Set env

```bash
export BINANCE_FUTURES_DEMO_ENABLED=true
export BINANCE_FUTURES_DEMO_API_KEY="…"     # from your Demo account
export BINANCE_FUTURES_DEMO_API_SECRET="…"  # ditto; never commit
# Optional — defaults to https://demo-fapi.binance.com:
# export BINANCE_FUTURES_DEMO_BASE_URL=https://demo-fapi.binance.com
```

> **Loading a deployed env file:** do **not** `source` a production env
> file such as `shared/.env.prod.native` — it holds JSON/list values
> (e.g. `PUBLIC_API_PATHS=["…"]`) and values with spaces (cron strings)
> that a shell mangles (quotes stripped → pydantic Settings fails; `*`
> glob-expanded). The app reads it via pydantic's `env_file`, not the
> shell. To run this CLI against a deployed env, point Settings at the
> file and export only the keys the client reads with `os.getenv`:
>
> ```bash
> EF=~/services/auto_trader/shared/.env.prod.native
> env -i HOME="$HOME" PATH="$PATH" ENV_FILE="$EF" \
>   BINANCE_FUTURES_DEMO_ENABLED=true \
>   BINANCE_DEMO_API_KEY="$(grep -E '^BINANCE_DEMO_API_KEY=' "$EF" | head -1 | cut -d= -f2-)" \
>   BINANCE_DEMO_API_SECRET="$(grep -E '^BINANCE_DEMO_API_SECRET=' "$EF" | head -1 | cut -d= -f2-)" \
>   uv run python -m scripts.binance_futures_demo_smoke --preflight
> ```

### 5.3 Verify intent (no HTTP)

```bash
uv run python -m scripts.binance_futures_demo_smoke --plan-only \
  --symbol XRPUSDT --side BUY --cap-usdt 10
```

Confirm the printed JSON has `source: "futures_demo"`,
`product: "usdm_futures"`, `leverage: 1`, and the expected symbol /
side. If anything is off, stop here.

### 5.4 Confirm credentials and position mode (signed reads)

```bash
uv run python -m scripts.binance_futures_demo_smoke --preflight
```

Expect a redacted account summary line and a 0 exit. If you get
`BinanceFuturesDemoHedgeModeBlocked`, flip the Demo account to
One-way mode on the console and re-run.

### 5.5 Confirm exchange filters pass (signed, non-mutating)

```bash
uv run python -m scripts.binance_futures_demo_smoke --order-test \
  --symbol XRPUSDT --side BUY --cap-usdt 10
```

Expect an evidence line with `event: "futures_demo_order_test"` and
`ok: true`, then exit 0.

### 5.6 Run the confirmed lifecycle

```bash
uv run python -m scripts.binance_futures_demo_smoke --confirm \
  --symbol XRPUSDT --side BUY --cap-usdt 10 --leverage 1
```

Watch for the `[rob-298-fut]` evidence lines (see §6). On a clean run
the process exits 0 after `reconciled cid=...`. Anything else needs
§7.

### 5.7 Verify the ledger rows

```sql
SELECT
  client_order_id,
  parent_client_order_id,
  product,
  lifecycle_state,
  side,
  qty,
  broker_order_id,
  notional_usdt,
  venue_host,
  planned_at,
  submitted_at,
  filled_at,
  closed_at,
  reconciled_at
FROM binance_demo_order_ledger
WHERE product = 'usdm_futures'
  AND client_order_id LIKE 'rob-298-fut-%'
ORDER BY created_at DESC
LIMIT 4;
```

You should see two rows per `--confirm` run (parent BUY +
`reduceOnly` SELL child), all with `venue_host =
'demo-fapi.binance.com'` and the parent BUY ending in
`lifecycle_state = 'reconciled'`.

### 5.8 Confirm position is flat post-run

The CLI already does this as part of reconciliation, but you can
re-check via the Binance Demo Futures console "Positions" tab — the
symbol you just traded should show zero. The ledger's `reconciled_at`
row is the source of truth; the UI is just a cross-check.

---

## 6. Expected redacted evidence shape

A clean `--confirm` run on `XRPUSDT` BUY emits these lines on stdout,
in order (UUIDs vary; secrets never appear):

```
[rob-298-fut] position_mode is_hedge=false
[rob-298-fut] leverage_set symbol=XRPUSDT leverage=1
[rob-298-fut] planned cid=rob-298-fut-<uuid> product=usdm_futures symbol=XRPUSDT side=BUY qty=<qty> venue=demo-fapi.binance.com
[rob-298-fut] previewed cid=rob-298-fut-<uuid>
[rob-298-fut] order_test_ok symbol=XRPUSDT
[rob-298-fut] validated cid=rob-298-fut-<uuid>
[rob-298-fut] submitted cid=rob-298-fut-<uuid> broker_order_id=<id> status=FILLED reduce_only=false
[rob-298-fut] filled cid=rob-298-fut-<uuid>
[rob-298-fut] position_check symbol=XRPUSDT amt=<qty>
[rob-298-fut] planned cid=rob-298-fut-<close-uuid> product=usdm_futures symbol=XRPUSDT side=SELL qty=<qty> venue=demo-fapi.binance.com
[rob-298-fut] previewed cid=rob-298-fut-<close-uuid>
[rob-298-fut] validated cid=rob-298-fut-<close-uuid>
[rob-298-fut] submitted cid=rob-298-fut-<close-uuid> broker_order_id=<close-id> status=FILLED reduce_only=true
[rob-298-fut] filled cid=rob-298-fut-<close-uuid>
[rob-298-fut] closed cid=rob-298-fut-<uuid>
[rob-298-fut] open_orders_check empty=true
[rob-298-fut] position_check symbol=XRPUSDT amt=0
[rob-298-fut] reconciled cid=rob-298-fut-<uuid>
```

Anomalies print `anomaly cid=... reason=...` and the CLI exits 2.
None of these lines contain the API key or secret.

---

## 7. Rollback / manual reconciliation

If a `--confirm` run dies between `submitted` and `reconciled`
(network blip, ctrl-C, server 5xx), the ledger may have a non-terminal
row, there may be an open order on Demo, **or** the position may be
non-flat.

### 7.1 Inspect the ledger

```sql
SELECT *
FROM binance_demo_order_ledger
WHERE product = 'usdm_futures'
  AND lifecycle_state NOT IN ('reconciled', 'cancelled', 'anomaly')
  AND created_at > NOW() - INTERVAL '1 day'
ORDER BY id DESC;
```

Grab the `client_order_id`(s) — you'll need them below.

### 7.2 Check the broker for stranded open orders

From a Python shell (uses the same env you ran the smoke with):

```python
import asyncio
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)

async def main():
    async with BinanceFuturesDemoExecutionClient.from_env() as client:
        open_orders = await client.get_open_orders(symbol="XRPUSDT")
        print(open_orders)

asyncio.run(main())
```

If `open_orders` is non-empty and includes your stranded
`client_order_id`, cancel it explicitly:

```python
await client.cancel_order(
    symbol="XRPUSDT",
    client_order_id="rob-298-fut-<your-cid>",
    confirm=True,
)
```

`confirm=True` is required — without it the call returns a dry-run
result and changes nothing.

### 7.3 Check position is flat; close manually if not

```python
async def check_and_close():
    async with BinanceFuturesDemoExecutionClient.from_env() as client:
        pos = await client.get_position(symbol="XRPUSDT")
        print(pos)  # positionAmt should be 0

        # If positionAmt != 0, you must close it with reduceOnly=True:
        # await client.submit_order(
        #     symbol="XRPUSDT",
        #     side="SELL",  # opposite of the stranded open
        #     order_type="MARKET",
        #     quantity=<abs positionAmt>,
        #     reduce_only=True,
        #     confirm=True,
        # )

asyncio.run(check_and_close())
```

`reduce_only=True` is mandatory on a manual close. The execution
client refuses to submit a close-side order without it.

### 7.4 Reconcile the ledger

Bring the stranded row to a terminal state through the service (do
**not** UPDATE the table directly):

```python
from app.services.brokers.binance.demo.ledger.service import (
    BinanceDemoLedgerService,
)
# … construct the service with an async session …

# If you cleanly cancelled the stranded order:
await ledger.record_cancelled(
    client_order_id="rob-298-fut-<your-cid>",
    broker_status="CANCELED",
    now=...,
)

# Or, if the order/position is unaccounted for after manual
# intervention:
await ledger.record_anomaly(
    client_order_id="rob-298-fut-<your-cid>",
    reason="manual reconciliation: <what happened>",
    now=...,
)
```

After the row is terminal, re-run `get_open_orders(symbol=...)` and
`get_position(symbol=...)` and confirm both are clean (empty list +
`positionAmt == 0`).

### 7.5 Document and escalate

If you had to reconcile manually:

* Post a comment on **Linear ROB-298** describing the stranded
  `client_order_id`, what state it was in, what the position amount
  was, and how you resolved it.
* If the cause looks like a code bug (not a transient network issue),
  file a follow-up Linear issue and link it from the ROB-298 comment.

---

## 8. Verification suite

```bash
# Futures Demo unit + integration tests:
uv run pytest tests/services/brokers/binance/futures_demo/ -q

# Smoke CLI tests:
uv run pytest tests/scripts/test_binance_futures_demo_smoke.py -q

# Demo ledger tests (covers product='usdm_futures' rows):
uv run pytest tests/services/brokers/binance/demo/ -q

# Cross-isolation audit (spot_demo and futures_demo do not leak
# into each other):
uv run pytest tests/services/brokers/binance/demo/test_no_testnet_imports.py -q
```

All four suites are expected to pass on every PR that touches
`app/services/brokers/binance/futures_demo/` or the unified Demo
ledger.

---

## 9. Linked decisions

Authoritative source for ROB-298 design choices (env namespace, 10
USDT cap, leverage/position-mode contract, `reduceOnly` contract,
symbol allowlist, ledger lifecycle, default-disabled, removal of
futures testnet) — Linear ROB-298 issue comment
`d258c471-3202-444b-901b-c127f3ee44af`. Defer to that comment if this
runbook ever drifts from it.

---

## 10. Out of scope

* **Multi-symbol scalping logic.** The smoke CLI is a single-symbol,
  single-position round-trip. Strategy logic (signals, sizing beyond
  the 10 USDT cap, multi-leg lifecycle) is not part of ROB-298.
* **Scheduler / TaskIQ / Prefect / cron / Hermes activation** of the
  Futures Demo execution client. Tracked under ROB-292 and remains
  paused.
* **Live / mainnet futures (`fapi.binance.com`)** and **deprecated
  futures testnet (`testnet.binancefuture.com`).** Both are
  transport-layer fail-closed. Live trading is not a feature of this
  repo.
* **Hedge mode, leverage > 1, isolated margin sweeps, cross-symbol
  netting.** The smoke contract pins one-way mode + 1x leverage.
  Changing these would require a separate Linear issue and runbook
  update.
* **Touching Upbit, Alpaca, KIS, or any real-money broker path** from
  the Binance Demo lane. Cross-broker behavior is out of scope.
