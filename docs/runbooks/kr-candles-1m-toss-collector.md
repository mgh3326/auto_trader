# KR 1-minute event collector — Toss → research.kr_candles_1m_toss (#1086)

## 1. What it is

A DART filing of a #1054 target type triggers collection of the filer's
1-minute bars for D0 (first KRX session on or after publication) and D+1 (the
next session), from Toss `GET /api/v1/candles?interval=1m`, into
`research.kr_candles_1m_toss`.

| part | location |
|---|---|
| strict client method | `TossReadClient.minute_candles` / `collect_minute_candles` (`app/services/brokers/toss/client.py`) |
| DTO | `TossMinuteCandle`, `parse_minute_candle_page` (`app/services/brokers/toss/dto.py`) |
| collector + writer | `app/services/research_candles/toss_minute_collector.py` |
| trigger | `app/services/research_candles/dart_minute_trigger.py` |
| backfill CLI | `scripts/backfill_kr_candles_1m_toss.py` |

Nothing is scheduled by the code. Wiring is a desk step (§5).

## 2. Data contract (read this before using the bars)

- **Table**: `research.kr_candles_1m_toss`, never `research.kr_candles_1m`. Toss
  returns a combined KRX+NXT product and cannot support that table's KRX|NTX venue key.
- **`time_utc` is the Toss bar END.** openapi.json v1.2.19: a 1m bar covers
  `[timestamp - 1 min, timestamp)`. Research that labels bars by start (the #1054
  v1.2 N4 entry rule `bar start >= rcept_dt + 6 min`) must read
  `time_utc - interval '1 minute' AS bar_start`.
- `session_segment` (NXT_PRE / KRX_REGULAR / NXT_POST) is the Phase-2 KST
  clock-time label of `time_utc`. It is not a venue claim.
- Prices are **unadjusted** (`adjusted=false`), so a re-collection is stable.
- `value` = close × volume (synthetic). `is_padding` = volume 0. `pre_nxt` = NULL.
- `batch_id` = `t1086-candles-1m:<run_id>`.
- Open question for the first collected day: whether the KRX closing
  single-price print sits in the bar stamped 15:30 or 15:31. The Phase-2 README
  says Toss's 15:30 candle omits closing-auction volume. Check one symbol-day
  against a daily close before relying on "the 15:30 bar is the closing print".

## 3. Behaviour

- Pagination: `before` (inclusive) → `nextBefore`, newest first. It stops on an
  empty page, a null `nextBefore`, or once past D0 00:00 KST. An identical
  repeated boundary bar is dropped. A conflicting duplicate or a non-advancing
  cursor raises a contract error. The page cap raises `TossPaginationCapExceeded`.
- Rate: every page goes through the shared `MARKET_DATA_CHART` limiter
  (`from_settings`, local 5/s). The CLI adds a stricter pace (`--max-tps`,
  default 2, capped at the group limit).
- Writes: `ON CONFLICT (time_utc, symbol) DO NOTHING`. Existing rows win. A
  differing re-fetch is counted as `existing_conflict` and never overwritten.
- Maturity: nothing is fetched before D+1 20:10 KST (`immature`, not a gap).
- Gaps (JSONL via `--gap-log`, one line per symbol-day; never silent):
  `toss_api_error`, `toss_transport_error`, `toss_contract_error`,
  `pagination_cap`, `unclassifiable_session_segment`, `no_bars`,
  `no_regular_bars`. There is no KIS fallback: the table admits `source='TOSS'` only.
- Type mapping = #1054 r3 `cohort_of`. Collected cohorts: SUPPLY_MAND, SUPPLY_VOL,
  BUYBACK_DIRECT, BUYBACK_TRUST_NEW, EARNINGS, RIGHTS. Listed = `corp_cls` 유/코.
  Symbol = exact `company_name` match to `kr_symbol_universe.name` among common
  shares. Ambiguous names are skipped.

## 4. Backfill (desk, after merge)

Pre-check, once: the DB role the CLI runs as needs INSERT on the table. `--commit`
checks `has_table_privilege` and stops before any Toss call if it is missing.

```bash
# plan only (default): prints symbols and D0/D+1, no Toss call, no write
uv run python -m scripts.backfill_kr_candles_1m_toss --from-date 2026-07-22 --to-date 2026-09-30

# collect the 07-22..09-30 candidates
uv run python -m scripts.backfill_kr_candles_1m_toss --from-date 2026-07-22 --to-date 2026-09-30 \
    --commit --gap-log <dir>/kr_candles_1m_toss_gaps_0722_0930.jsonl

# optional: back to 2026-01 (DART rows before 07-22 are the P1-b silent-empty
# partitions, so this finds little until that backfill lands)
uv run python -m scripts.backfill_kr_candles_1m_toss --from-date 2026-01-02 --to-date 2026-07-21 \
    --commit --gap-log <dir>/kr_candles_1m_toss_gaps_0102_0721.jsonl
```

Re-running is safe. Requests whose D0 and D+1 already hold KRX_REGULAR bars are
skipped (`already_collected`, no Toss call) unless `--refetch` is given.

## 5. Wiring (approved as Q-122 = A, hk 1084; desk registers after merge)

Recommended: one daily run of the same CLI after NXT close and after the day's
DART ingestion, over a rolling window.

```bash
# daily, 20:40 KST (after D+1 maturity at 20:10), rolling 10 calendar days
uv run python -m scripts.backfill_kr_candles_1m_toss \
    --from-date "$(TZ=Asia/Seoul date -d '10 days ago' +%F)" \
    --to-date "$(TZ=Asia/Seoul date +%F)" \
    --commit --gap-log <dir>/kr_candles_1m_toss_gaps.jsonl
```

This is the trigger. The CLI reads newly ingested `market_events` rows, runs
`plan_collection_requests` (the trigger's type filter), collects every matured
request, and skips what is already stored. A filing ingested late (the 18:30 or
next-morning DART rounds) is picked up by the next run inside the window.

Alternative (not recommended): call
`schedule_collections_for_dart_rows(rows, symbol_index=..., enqueue=...)` right
after `ingest_kr_disclosures_for_date` succeeds. It needs a delayed-execution
transport for `enqueue` (a request is only collectable after
`request.ready_at`), which the repo does not have today.

The confirmation sample (#1054 §4) starts on the day the daily run goes live.
