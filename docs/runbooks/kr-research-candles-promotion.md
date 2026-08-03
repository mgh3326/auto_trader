# KR research candle store + monthly promotion

Deep-history KR 1-minute bars live in `research.kr_candles_1m`, **not** in the
production `public.kr_candles_1m`.

Decision source: `herdr-inbox/answer-codexmock-research-db-1805.md`
(`RECOMMENDED_OPTION = HYBRID(C1+FROZEN_PARQUET+SCHEDULELESS_MONTHLY_PROMOTION)`).

## 1. Why a separate schema

Production `public.kr_candles_1m` has an active 90-day retention policy
(TimescaleDB job 1001, `drop_after = 90 days`, `scheduled = true`), as do all
four public caggs. Measured 2026-08-03: the oldest production row was
2026-04-30, i.e. ~95 days. Backfilling a year into that table would have had
roughly 75% of it dropped within a day.

The research schema therefore carries the deep history and has **no retention
policy at all**. Production keeps its retention, its caggs, and its role as the
recent-window serving cache — unchanged.

## 2. What the schema records that production cannot

| column | why it exists |
|---|---|
| `source` (`KIWOOM`/`KIS`/`TOSS`/`UNKNOWN`) | production has **no provider column**, so provider identity is unrecoverable there |
| `venue` (`KRX`/`NTX`) | the execution venue — **not** the same thing as the provider |
| `session_segment` | `KRX_REGULAR` / `NXT_PRE` / `NXT_OVERLAP` / `NXT_POST` / `UNKNOWN` |
| `retrieved_at`, `batch_id` | which collection run produced the row |

`source` and `venue` are deliberately separate: Toss-supplied data is not
automatically NTX. Conflating provider with venue is the specific mistake this
schema prevents.

Both `session_segment` and `source` **fail closed to `UNKNOWN`** rather than
guessing. In particular, rows promoted out of production are stamped
`source = UNKNOWN`, because production cannot prove which provider wrote them.
Labelling them `TOSS` would fabricate exactly the provenance this design exists
to preserve.

## 3. Research caggs are newly defined, not promoted

`research.kr_candles_{5m,15m,30m,1h}` group by
`bucket, symbol, venue, session_segment`.

The public caggs **cannot** be reused: they collapse KRX and NTX into a single
bucket (merging high/low and summing volume/value) and keep only a `venues`
array. KRX-regular-only research needs `venue = 'KRX' AND session_segment =
'KRX_REGULAR'`, which that shape cannot express.

There is **no automatic refresh policy** on the research caggs. A 2-day refresh
window does not materialise historical backfill anyway, so refresh is an
explicit operator step:

```sql
CALL refresh_continuous_aggregate('research.kr_candles_5m', '2025-08-01', '2025-09-01');
```

Refresh month by month and record how long each window takes.

## 4. Monthly promotion (operator-run, no scheduler)

Production holds a rolling 90 days. To keep research growing forward, promote
completed sessions **monthly** — before the 90-day window drops them.

```bash
export ENV_FILE=...            # never .env.prod
export RESEARCH_CANDLE_PROMOTION_ENABLED=true

# dry run (default; also sets the session read-only as a second guard)
uv run python -m scripts.promote_kr_candles_to_research --venue KRX

# apply
uv run python -m scripts.promote_kr_candles_to_research --venue KRX --confirm
```

Guarantees:

- reads `public.kr_candles_1m`, never writes it
- **completed sessions only** — today counts only after 15:30 KST
- identical `(time_utc, symbol, venue)` with identical OHLCV/value → no-op
- a disagreement is **quarantined** in `research.kr_candle_promotion_conflicts`
  and never overwritten; unresolved conflicts block snapshot sealing
- **fails closed** when promotion lag > 60 days, or when the requested range
  starts before the 90-day production retention floor. A range whose production
  origin is already gone is never reported as success — it is reported as
  needing provider backfill.

### If promotion is blocked

`STATUS = BLOCKED` means production can no longer supply the range. Do not
re-run with a wider window; re-collect the missing range from the providers
(Stage B backfill) instead.

## 5. Deliberately absent

- **no dual-write** from the live collector — a research-DB failure must not be
  able to disturb live ingestion, and a best-effort write must not be able to
  leave a silent research gap
- **no cron / TaskIQ / Prefect registration** — cadence is an operator checklist
  item. Making it automatic is a separate governance decision, not a side effect
- **no retention** on research raw or caggs
- **no change** to production tables, retention, or the public caggs

## 6. Applying the migration

`alembic upgrade head` is run by `deploy-native.sh` at cutover. Do not run it
against the production database by hand.

Requires the `timescaledb` extension (>= 2.8.1; production is on 2.26.3).
