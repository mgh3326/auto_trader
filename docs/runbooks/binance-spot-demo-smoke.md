# Binance Spot Demo Mode Smoke (ROB-296) — Runbook

**Scope.** Operator runbook for the Binance Spot **Demo Mode** lane
introduced by ROB-296 (`demo-api.binance.com`). This is a **separate
adapter** from the Spot Testnet lane (ROB-286, `testnet.binance.vision`).
Both lanes coexist; do **not** repurpose either env namespace for the
other.

**This PR (ROB-296 first cut)** opens the adapter/config/preflight/
dry-run lane only. Confirmed order submission against Spot Demo is
**not** implemented here — see §6 for the operator follow-up template.

---

## 0. Lane boundaries at a glance

| Lane | Host | Env namespace | Adapter package |
|---|---|---|---|
| Spot Testnet (ROB-286) | `testnet.binance.vision` | `BINANCE_TESTNET_*` | `app/services/brokers/binance/testnet/` |
| **Spot Demo (this issue)** | **`demo-api.binance.com`** | **`BINANCE_SPOT_DEMO_*`** | **`app/services/brokers/binance/spot_demo/`** |
| USD-M Futures Demo (ROB-291) | `demo-fapi.binance.com` | — | not implemented; tracked under [ROB-291](https://linear.app/mgh3326/issue/ROB-291) |

**Hard invariants:**
* The three host allowlists (`PUBLIC_HOSTS`, `TESTNET_HOSTS`,
  `SPOT_DEMO_HOSTS`) are pairwise disjoint. Cross-environment leakage is
  refused at the transport layer with
  `BinanceSpotDemoCrossAllowlistViolation` (or
  `BinanceLiveHostBlocked`).
* `BINANCE_TESTNET_*` env vars do **not** activate the Spot Demo path,
  and vice versa.
* The Spot Demo adapter signs with HMAC-SHA256 (same as Spot Testnet).
  If the operator's Spot Demo account requires Ed25519, the preflight
  surfaces `BinanceSpotDemoUnsupportedAuth` — **do not** patch a signer
  fallback in this PR; report as ROB-296 follow-up.
* No scheduler / TaskIQ / Prefect / cron / Hermes activation in this
  issue. ROB-292 remains blocked.

---

## 1. Env variables

| Variable | Required when opted in? | Default | Notes |
|---|---|---|---|
| `BINANCE_SPOT_DEMO_ENABLED` | Yes (must be `true`) | unset → disabled | Master kill-switch. Default behavior is fail-closed. |
| `BINANCE_SPOT_DEMO_API_KEY` | Yes (for preflight) | — | API key from your Spot Demo account. |
| `BINANCE_SPOT_DEMO_API_SECRET` | Yes (for preflight) | — | API secret. Never logged. |
| `BINANCE_SPOT_DEMO_BASE_URL` | No | `https://demo-api.binance.com` | Validated against `SPOT_DEMO_HOSTS` at factory init; a testnet or live host raises `BinanceSpotDemoCrossAllowlistViolation`. |
| `BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT` | No | `10` | Per-order cap for the planned-order template. |

---

## 2. Default-disabled behavior

```bash
uv run python -m scripts.binance_spot_demo_smoke
# → exit 0; single log line:
#   "spot demo disabled — set BINANCE_SPOT_DEMO_ENABLED=true to opt in"
# → zero HTTP, zero DB writes, zero Sentry events
```

This is the safe default. Setting only `BINANCE_TESTNET_*` does **not**
activate Spot Demo.

---

## 3. Plan-only dry-run (no HTTP, no credentials needed)

```bash
BINANCE_SPOT_DEMO_ENABLED=true \
  uv run python -m scripts.binance_spot_demo_smoke \
    --plan-only \
    --symbol BTCUSDT --side BUY --order-type LIMIT \
    --quantity 0.001 --price 50000
```

Effect:
* Stdout emits a single JSON line with `event: "spot_demo_plan"`.
* No httpx client is constructed.
* No DB writes, no ledger writes, no Sentry events.
* Evidence is `source: "spot_demo"`, `venue: "binance"`, `product: "spot"`.

Use this when you want to verify the planning/filter pipeline without
touching the Spot Demo server.

---

## 4. Read-only preflight (one signed GET, no orders)

```bash
BINANCE_SPOT_DEMO_ENABLED=true \
  BINANCE_SPOT_DEMO_API_KEY=$KEY \
  BINANCE_SPOT_DEMO_API_SECRET=$SECRET \
  uv run python -m scripts.binance_spot_demo_smoke --preflight
```

Effect:
* ONE signed `GET /api/v3/account` against `demo-api.binance.com`.
* Stdout emits a single JSON line with `event: "spot_demo_preflight"`.
* No DB writes, no ledger writes, no order placement.
* `api_key_fingerprint` is `<first4>…<last2>` — full key/secret are
  never logged.
* Balance amounts are NOT logged; only the count of nonzero rows.

**Auth-rejection surface:** if the server returns Binance error codes
`-2014`, `-2008`, or `-1022`, the CLI exits non-zero and raises
`BinanceSpotDemoUnsupportedAuth`. This typically means the Spot Demo
account uses Ed25519 keys (not HMAC). **Report as ROB-296 follow-up
rather than patching the signer in this PR.**

---

## 5. Cross-environment leakage smoke (always safe)

```bash
uv run pytest tests/services/brokers/binance/spot_demo/test_cross_environment_leakage.py -q
```

Asserts:
* The Spot Testnet adapter refuses `demo-api.binance.com`.
* The Spot Demo adapter refuses `testnet.binance.vision`.
* Both adapters refuse `api.binance.com`, `fapi.binance.com`,
  `stream.binance.com`, `data-api.binance.vision`.

---

## 6. Confirmed order-submit smoke — **NOT implemented in this PR**

```bash
# This command WILL refuse with BinanceSpotDemoOrderSubmitNotImplemented:
BINANCE_SPOT_DEMO_ENABLED=true \
  BINANCE_SPOT_DEMO_API_KEY=$KEY \
  BINANCE_SPOT_DEMO_API_SECRET=$SECRET \
  uv run python -m scripts.binance_spot_demo_smoke --confirm
```

Reason: the first ROB-296 PR deliberately opens only the adapter +
preflight + dry-run lane. Order submission would require mirroring the
~620-line `BinanceTestnetExecutionClient` plus a Spot Demo ledger
decision, which the Hermes review intentionally deferred.

**Operator follow-up template** (use when authorized to expand scope):

> Implement `BinanceSpotDemoExecutionClient` under
> `app/services/brokers/binance/spot_demo/execution_client.py` mirroring
> the testnet sibling. Decide ledger policy explicitly (reuse a
> source-labeled testnet table? add a new Spot Demo ledger table? keep
> log-only?). Land behind a separate operator approval gate; do NOT
> reuse the ROB-296 PR.

---

## 7. Verification command summary

```bash
# Existing Binance safety suite (must still pass — no regressions):
uv run pytest \
  tests/services/brokers/binance/testnet/test_host_allowlist.py \
  tests/services/brokers/binance/testnet/test_audit_no_live_host.py \
  tests/services/brokers/binance/test_audit_no_signed_endpoints.py \
  -q

# New Spot Demo suite:
uv run pytest \
  tests/services/brokers/binance/spot_demo/ \
  tests/scripts/test_binance_spot_demo_smoke.py \
  -q
```

---

## 8. Out of scope (do NOT do from this issue)

* Order submission against Spot Demo (see §6).
* Persistent Spot Demo order ledger (no alembic migration in this PR).
* Ed25519 signer (report as scope expansion if preflight surfaces it).
* USD-M Futures Demo (`demo-fapi.binance.com`) — tracked under ROB-291.
* TaskIQ / Prefect / cron / Hermes automation — ROB-292 is blocked.
* Production deploy, production DB migration, or live/mainnet routing.
* Touching Upbit, Alpaca, KIS, or any real-money broker path.
* Printing, committing, persisting, or summarizing credential values.
