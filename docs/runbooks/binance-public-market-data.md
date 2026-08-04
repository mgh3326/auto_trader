# Binance Public Market Data Adapter — Runbook

> ROB-285 — Child B of the ROB-283 epic. Read-only public REST + WebSocket
> adapter behind `app/services/brokers/binance/`. No credentials, no signed
> endpoints, no execution. Ownership: scalping working group.

## What this is

A read-only market-data adapter for Binance spot:

- **REST** (`api.binance.com`): `exchangeInfo`, `klines`, `bookTicker`.
- **WebSocket** (`stream.binance.com:9443`): combined `kline_1m` +
  `bookTicker` streams.
- Persists closed 1m candles via Child A's
  `MinuteCandlesRepository` (`crypto_candles_1m`).
- Tracks per-instrument health in `crypto_instrument_health`.

The adapter is library + CLI only. No TaskIQ task, no Prefect deployment,
no cron. A future child issue will wire it into a scheduled task once the
MVP is reviewed in production-like environments.

## Topology

```
              ┌────────────────────────────────┐
              │  scripts/binance_public_smoke  │
              │  (manual + future scheduler)   │
              └─────┬──────────────────────────┘
                    │
                    ▼
    ┌──────────────────────────┐       ┌────────────────────────────┐
    │ BinancePublicRestClient  │       │   BinancePublicWSClient    │
    │ exchange_info / klines / │       │  combined stream subscriber│
    │ book_ticker              │       └─────────┬──────────────────┘
    └──────────┬───────────────┘                 │
               │                                 │
               │ httpx event_hooks ──────────────┘
               │  - assert_allowed_host
               │  - reject X-MBX-APIKEY
               │  - reject 30x redirects
               │
               ▼
    ┌──────────────────────────┐
    │ BinanceCandleIngester    │  ── upsert ───►  crypto_candles_1m
    │ (cache instrument_id)    │                   (Child A)
    └──────────┬───────────────┘
               │
               ▼
    ┌──────────────────────────┐
    │ CryptoInstrumentHealth   │
    │ Service                  │  ── writes ───► crypto_instrument_health
    └──────────────────────────┘
```

## Env vars

The adapter has zero credential variables and zero connection toggles —
the only knobs are the REST backfill caps:

| Variable | Default | Purpose |
|---|---|---|
| `BINANCE_KLINE_BACKFILL_MAX_CANDLES` | `5000` | Hard ceiling on the number of candles a single `RestBackfiller.backfill(...)` call may collect. |
| `BINANCE_KLINE_BACKFILL_MAX_REQUESTS` | `10` | Hard ceiling on REST page requests per backfill. |
| `BINANCE_KLINE_BACKFILL_PAGE_SIZE` | `1000` | Page size for kline pagination (max 1000 on Binance). |

> Intentional non-presence: **no** `BINANCE_API_KEY`, **no**
> `BINANCE_API_SECRET`, **no** `BINANCE_TESTNET_*`. The public adapter
> does not use credentials; Child C (ROB-286) owns the testnet
> execution surface.

## CLI commands

```bash
# Dry-run (default) — REST + WS handshake, allowlist defense-in-depth check.
uv run python -m scripts.binance_public_smoke --symbol BTCUSDT --duration 10

# Multi-symbol WS subscribe.
uv run python -m scripts.binance_public_smoke \
    --symbols BTCUSDT,ETHUSDT,SOLUSDT --duration 30
```

`--duration` is the WebSocket receive budget. The smoke client adds an explicit
1-second shutdown budget, so a silent peer cannot extend cleanup to the
`websockets` library's default 10-second closing-handshake timeout. REST phases
run before this WS budget and retain their own transport timeouts.

Exit codes are documented in the script docstring. `0` is success; `5` is
"allowlist defense-in-depth check failed" (i.e., `fapi.binance.com` was
not rejected — investigate immediately).

## Health states

The `crypto_instrument_health` table tracks per-instrument health:

| State | Meaning | How to clear |
|---|---|---|
| `healthy` | Default. No issues observed. | (no-op) |
| `degraded` | WS reconnect failed ≥3 consecutive times. | Automatic — first successful event clears it. |
| `rate_limited` | REST `429`/`418` received. | Automatic — TTL via `retry_after_at`. |
| `manual_backfill_required` | Gap > cap detected on reconnect. | **Operator-only** — see below. |

Inspect:

```sql
SELECT instrument_id, state, reason, last_state_change_at,
       attempts, retry_after_at
FROM crypto_instrument_health
ORDER BY last_state_change_at DESC
LIMIT 50;

-- Drill down into a specific instrument by venue_symbol:
SELECT i.venue_symbol, h.state, h.reason, h.last_state_change_at
FROM crypto_instrument_health h
JOIN crypto_instruments i ON i.id = h.instrument_id
WHERE i.venue_symbol = 'BTCUSDT';
```

Service-side reads:

```python
from app.services.instrument_health.service import (
    CryptoInstrumentHealthService, InstrumentHealthState,
)
svc = CryptoInstrumentHealthService(session=session)
state = await svc.get_state(instrument_id=123)
if state is InstrumentHealthState.MANUAL_BACKFILL_REQUIRED:
    ...
```

## Manual backfill recovery

An instrument lands in `manual_backfill_required` when:

1. A WS reconnect detects a gap that exceeds the configured caps
   (default: 5000 candles or 10 requests).
2. `RestBackfiller` raises `BinanceBackfillCapExceeded`.
3. The Task 12 orchestration layer flags the instrument via
   `CryptoInstrumentHealthService.record_manual_backfill_required(...)`.

Recovery procedure for the operator:

1. **Diagnose the gap.** Query `crypto_candles_1m` for the most recent
   row for that instrument, and compare with `now()`. If the gap is
   weeks/months, widen caps for a one-off run.
2. **(Optional) Widen caps for a one-off backfill run.** Set environment
   variables before invoking the smoke or a one-off backfill script:
   ```bash
   export BINANCE_KLINE_BACKFILL_MAX_CANDLES=200000
   export BINANCE_KLINE_BACKFILL_MAX_REQUESTS=300
   ```
3. **Run REST backfill manually.** A one-off script using
   `BinancePublicRestClient` + `RestBackfiller` against the gap range,
   then `MinuteCandlesRepository.upsert_rows(...)` to persist.
4. **Clear the flag.** Once the gap is filled and the operator has
   verified continuity, call from a one-off script:
   ```python
   await svc.clear_manual_backfill(instrument_id=123, operator="alice")
   ```
   The audit trail (`metadata.cleared_by`, `metadata.cleared_at`) is
   persisted automatically.

> The service refuses `record_recovered(...)` on a
> `manual_backfill_required` row — operators MUST call
> `clear_manual_backfill(...)` explicitly with an identifier.

## Rate-limit weight reference

Binance spot REST publishes per-minute weight headers
(`X-MBX-USED-WEIGHT-1M`). As of 2025 the default cap is **1200 weight/min**.

- **Telemetry** (`app/services/brokers/binance/rate_limit_telemetry.py`):
  every successful REST response logs at INFO with structured fields. When
  the ratio crosses 50% of the declared limit, a Sentry tag
  (`binance.rate_limit_weight_pct`) is set.
- **Soft-throttle** (`BinancePublicRestClient._maybe_soft_throttle`): when
  the last-seen ratio is ≥80%, the next REST call sleeps to the end of
  the current minute window (Binance counters reset on minute boundaries).
- **Hard-stop** (`BinancePublicRestClient._send`): `429`/`418` responses
  raise `BinanceRateLimited(retry_after_seconds=...)`. The caller decides
  whether to retry; the adapter never auto-retries.

## Allowlist hosts

The transport allowlist (`app/services/brokers/binance/host_allowlist.py`)
is **frozen** to these four production hosts:

- `api.binance.com`
- `data-api.binance.vision`
- `stream.binance.com`
- `data-stream.binance.vision`

Anything else raises `BinanceLiveHostBlocked` at the httpx event hook
boundary. Subdomain spoofs (`stream.binance.com.evil.example`) are
rejected via strict equality.

> Testnet hosts (`testnet.binance.vision`, `stream.testnet.binance.vision`,
> `testnet.binancefuture.com`) are **not** in this allowlist. Child C
> (ROB-286) introduces a separate execution-adapter allowlist for testnet
> trading.

## Production cutover checklist

This PR ships the `crypto_instrument_health` Alembic migration
(`4facd9697962_add_crypto_instrument_health`) but does **not** run
`alembic upgrade head` on production. Production cutover is a separate
operator step. Pre-cutover:

1. **Pre-cutover backup.** `pg_dump` (or vendor equivalent) of the target
   DB so the migration is reversible without code rollback.
2. **Apply migration on non-prod first.** `alembic upgrade head` on the
   staging/test DB. Verify the table exists with the CHECK constraint
   and starts empty.
3. **Round-trip sanity check.** `alembic downgrade -1 && alembic upgrade head`
   to verify the downgrade path.
4. **No scheduler activation.** Confirm via
   `grep -rn "binance" app/core/scheduler.py app/core/taskiq_broker.py app/tasks/`
   — the public adapter is library + CLI only at this point.
5. **Seed `crypto_instruments` rows for Binance spot symbols.** The
   adapter does NOT auto-create instrument rows. Operators must seed
   `(venue='binance', product='spot', venue_symbol=..., base, quote, status='active')`
   for every symbol they want to subscribe.
6. **Smoke against the production host.** Run
   `uv run python -m scripts.binance_public_smoke --symbol BTCUSDT --duration 30`
   and tail logs for the first 30 minutes. Verify rate-limit usage stays
   well below 50% and that the WS produces both `kline_1m` and
   `bookTicker` events.
7. **Manually clear any pre-existing `manual_backfill_required` rows**
   before any scheduler activation in a follow-up PR.

After validation in non-prod, schedule production cutover separately —
not as part of this PR's merge.

## What's NOT in this adapter (boundary with Child C)

| Out of scope | Owner | Why |
|---|---|---|
| Testnet hosts (`testnet.binance.vision`, etc.) | Child C (ROB-286) | Public adapter is production-only. |
| Order / trade endpoints (`/api/v3/order`, etc.) | Child C | No signed-endpoint surface in this package. |
| `X-MBX-APIKEY` header | Child C | Public adapter has no credentials. |
| `binance_testnet_order_ledger` table | Child C | Ledger lives next to the execution surface. |
| Scalper / state machine | Child C | `app/services/scalping/*` doesn't exist yet. |
| Futures SDK | Child C (if needed) | `binance-sdk-derivatives-trading-usds-futures` is not in this PR. |
| TaskIQ / Prefect / cron activation | Future PR | Adapter starts only via CLI. |
| `app/jobs/*` modification | (unchanged) | Snapshot builder and other jobs untouched. |

If a code change needs anything in the right column to be testable,
**stop and re-scope** — don't reach into Child C.
