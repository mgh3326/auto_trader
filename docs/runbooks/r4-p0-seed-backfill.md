# R4 P0 seed backfill

This one-shot collector obtains 42 days (252 complete UTC 4-hour epochs) of
USD-M klines and premium-index klines, plus the full observable
`openInterestHist` 5-minute retention window, for XRPUSDT, DOGEUSDT, SOLUSDT,
and BTCUSDT.

It is research-only. It sends unsigned public `GET` requests only to
`https://fapi.binance.com`; account, signing, order, demo-fapi, production DB,
and live artifact paths are not reachable from its allowlist.

## Storage and provenance boundary

The artifact is:

```text
~/work/herdr-artifacts/r4-p0-seed-backfill/r4_p0_backfill.sqlite3
```

It reuses the live collector's append-only `pit_records` schema and 13 PIT
columns, but it never opens `r4_p0_collector.sqlite3`. Consumer-visible
separation is redundant by design:

- the filename is `r4_p0_backfill.sqlite3`;
- sources end in `.backfill`;
- `collector_version` is `r4-p0-seed-backfill.v1`;
- `run_id` starts with `backfill:`;
- immutable `artifact_metadata.artifact_kind` is `historical_rest_backfill`.

For these rows, `local_receive_time` means the completion time of the
historical backfill HTTP response. It is **not** the historical live receive
time. The exact semantic warning is sealed in `artifact_metadata`.

## Frozen feature contract

OFI uses T2.5 §3.2 exactly: complete 4-hour `/fapi/v1/klines` base volume,
where taker buy is row `[9]`, total is row `[5]`, and taker sell is
`total - taker buy`. Coverage requires both buy and sell to be positive.
`aggTrades` is not requested.

OI uses the endpoint's 5-minute grid. A boundary observation must be within
±5 minutes inclusive. When live and backfill observations are later combined,
the nearest live poll wins; only when no live poll qualifies does the nearest
`openInterestHist` backfill point win. Overlap relative difference greater
than 1% raises an integrity flag while retaining the live value.

No forward return, PnL, or directional-hit metric is produced.

## Run and audit

The default command is a no-network/no-write contract print:

```bash
uv run python -m scripts.r4_p0_seed_backfill
```

The one-shot run is explicitly armed:

```bash
export R4_P0_BACKFILL_ENABLED=true
uv run python -m scripts.r4_p0_seed_backfill --run
```

Do not put the gate in a committed `.env` file. The run writes
`coverage_report.json` beside the DB. Re-running is safe: semantic record IDs
deduplicate rows.

Offline audit and coverage regeneration:

```bash
uv run python -m scripts.r4_p0_seed_backfill --audit
```

The report contains symbol/source row and epoch periods, 252-epoch OFI and
premium acceptance, boundary-pair OI coverage, measured shared OI retention,
SQLite/hash-chain audit, endpoint request counts, official request-weight
consumption, observed weight headers, and 418/429 counts.

Rate-limit protection is conservative: requests are serialized with a
350-millisecond delay. Each 252-row kline request has official IP weight 2;
`openInterestHist` has official IP weight 0 and a separate limit of 1,000
requests per 5 minutes. Any 418 or 429 aborts immediately without retry.
