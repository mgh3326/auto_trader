# Upbit public read-model cache (ROB-232)

## What this is

A read-only Redis-cached service layer wrapping Upbit official public data for KRW pairs: ticker, orderbook, recent trades, closed candles, and market warnings. It is consumed by `/trading/api/invest/crypto/dashboard` through the crypto dashboard service and can be reused by later coin-detail work.

## Cache keys and TTLs

| Block | Redis key | TTL | Stale window |
| --- | --- | ---: | ---: |
| ticker | `upbit:public:read:ticker:v1:<sha1>` | 5 s | 60 s |
| orderbook | `upbit:public:read:orderbook:v1:<market>` | 3 s | 30 s |
| trades | `upbit:public:read:trades:v1:<market>:<count>` | 5 s | 30 s |
| candles | reuses existing `upbit_ohlcv_cache` keys | day/week/month bucketed | n/a |
| market warnings detail | `upbit:public:read:warnings:v1` | 300 s | 1800 s |

Set `UPBIT_PUBLIC_READ_MODEL_CACHE_ENABLED=false` only for local/debug bypass. Bypassing Redis makes every request call Upbit public endpoints directly and can increase rate-limit risk.

## State model

Each block returns metadata with `{source, fetchedAt, state, errorReason}`-style fields.

- `fresh`: upstream call succeeded or cached payload is still inside its TTL.
- `stale`: upstream refresh failed, but a stale cached payload is still inside the stale window.
- `unavailable`: upstream failed and no cached payload is available.
- `missing`: no upstream call was attempted, such as an empty market list.

When adapted into dashboard `meta.sources`, `fresh` and `stale` are reported as `supported`; `unavailable` and `missing` are reported as `unavailable`.

## Operations

Inspect dashboard source freshness locally:

```bash
curl -sS 'http://localhost:8000/trading/api/invest/crypto/dashboard?limit=5' | jq '.meta.sources'
```

Flush all Upbit public read-model cache keys during an Upbit incident:

```bash
redis-cli --scan --pattern 'upbit:public:read:*' | xargs -r redis-cli del
```

For Docker-based local Redis, run the same scan/delete through the Redis container shell. Do not run this against production without separate operator approval.

## Safety boundary

This package is read-only. It must not submit, cancel, replace, or modify broker orders, must not import `app.services.brokers.upbit.orders`, and must not add migrations, backfills, schedulers, or production write paths. The safety-import test covers the mutation-import boundary.
