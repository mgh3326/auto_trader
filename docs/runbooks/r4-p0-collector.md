# R4 P0 no-regret collector

This is a manual, research-artifact-only point-in-time collector. It never
writes the production database and is not registered with TaskIQ, cron, or
Prefect.

## Hard boundary

- Public unsigned market data only.
- REST: HTTPS `GET` only, host `fapi.binance.com`, exact paths:
  `/fapi/v1/openInterest`, `/fapi/v1/premiumIndex`,
  `/fapi/v1/premiumIndexKlines`,
  `/futures/data/openInterestHist`, `/futures/data/basis`,
  `/futures/data/takerlongshortRatio`.
- Websocket: `wss://fstream.binance.com/public` for book/depth and
  `wss://fstream.binance.com/market` for aggTrade/forceOrder. The split is
  required by Binance's post-2026-04-23 routed websocket contract.
- No API key, signing, account endpoint, order endpoint, or broker mutation.
- XRP/DOGE/SOL are signal symbols. BTC is collected only as a predictor.

## Collection priority and backfill boundary

Operational attention follows this order without dropping any P0 source:

1. The four websocket sources: aggTrade, forceOrder, 1-second bookTicker
   snapshots, and top-5 depth. Their local receive time is not recoverable
   later; book snapshots and forceOrder observations are likewise not
   reconstructable from a later REST backfill. Although aggTrade content was
   observed to be REST-backfillable through roughly 390 days, that does not
   recreate the original PIT receive-time observation.
2. `openInterestHist` at 5-minute resolution. Production probing confirmed
   that this endpoint has the actual short retrospective boundary (under 30
   days), so it is collected on every open-interest poll. The instantaneous
   `/fapi/v1/openInterest` snapshot remains as a separate additive source.
3. Basis, taker ratio, and premium/funding polls. They remain in scope, but are
   lower urgency because their exchange-time content can be backfilled. In
   particular, production basis rows were observed beyond 1,000 days; the
   earlier 30-day basis assumption is not used by this collector.

Backfill reach and PIT observation are separate properties. A later REST row
must not be treated as if it had the `local_receive_time` of the live collector.

The basis endpoint can transiently return HTTP 200 with an empty JSON list.
That is **missing source data**, not a successful zero-observation state. The
collector continues polling the other symbols, reports the failed symbol, and
re-reads a bounded 100-period (8h20m) window on the next attempt. Deduplication
keeps replays idempotent, while the original collection floor prevents this
recovery path from acting as seed backfill. Any absence still unresolved at
epoch finalization is `FINAL_MISSING`; two different canonical payloads for the
same source identity are `FINAL_CONFLICT`, never last-write-wins
`FINAL_COMPLETE`.

## Modes

With no mode flag the command prints its contract and makes no network or disk
write:

```bash
uv run python -m scripts.r4_p0_collector
```

The bounded validation mode is explicit and capped at 180 seconds:

```bash
uv run python -m scripts.r4_p0_collector \
  --probe --duration 60 \
  --artifact-root /tmp/r4-p0-validation
```

The long-running operational arm requires both an environment gate and a CLI
flag:

```bash
export R4_P0_COLLECTOR_ENABLED=true
export R4_P0_ARTIFACT_ROOT="$HOME/work/herdr-artifacts/r4-p0-collector"
uv run python -m scripts.r4_p0_collector --run
```

Do not put these values in a committed `.env` file. Stop with SIGTERM or
Ctrl-C. A single-instance file lock prevents two writers sharing an artifact.

## Storage and restart contract

The artifact is `r4_p0_collector.sqlite3` in WAL mode with `synchronous=FULL`.
All persistence uses INSERT; database triggers reject UPDATE and DELETE.
`record_id` is a semantic unique key, so replayed websocket events and
unchanged REST periods are ignored after restart.

`bookTicker` is stored as one snapshot per symbol per second (the P0 contract
allows either every change or 1s snapshots); raw aggTrade, forceOrder snapshots,
and top-5 depth updates are not sampled.

The premium poll stores the live mark/index/predicted-funding snapshot and the
latest completed 1-minute premium-index kline. An in-progress kline whose close
time is after `request_completed_at` is rejected, so downstream PIT consumers
cannot accidentally use its future close.

Each source/symbol/UTC-day partition is an ordered SHA-256 chain:
`partition_sha256` covers the previous chain head plus the current PIT
envelope. `raw_payload_sha256` independently covers canonical raw JSON.
A restart/reconnect marks the first subsequent stream record per source/symbol
with `gap_detected=true`; sequence discontinuities are also surfaced. The
`forceOrder` stream remains Binance's throttled snapshot, not a complete
liquidation tape.

Run the offline audit (no network) at any time:

```bash
uv run python -m scripts.r4_p0_collector \
  --audit --artifact-root "$R4_P0_ARTIFACT_ROOT"
```

It verifies raw hashes, partition-chain links, the 13 PIT column names, and
prints one secret-free sample per collected source. The collector also logs
session insert, duplicate, failure, and cumulative source counts every 30
seconds so a zero-row/partial-source run is visible. On graceful exit, the
process returns nonzero if any required source had neither an insert nor a
deduplicated response. `forceOrder` is reported separately as a sparse,
event-driven source and is not treated as failed merely because no liquidation
occurred during a short run.
