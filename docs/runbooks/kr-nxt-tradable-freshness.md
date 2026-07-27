# KR NXT Tradability Freshness Runbook

This runbook keeps the data behind the fail-closed approval-window gate fresh.
It does not relax that gate, send approval cards, or place orders.

## Why there are two syncs

`nxt_tradable` is derived rather than stored:

```text
nxt_tradable = nxt_eligible AND nxt_trading_suspended IS NOT TRUE
asof = COALESCE(toss_master_updated_at, updated_at)
stale = now - asof > 2 days
```

The inputs have different owners:

| Input | Source | Operator entrypoint | What it changes |
|---|---|---|---|
| `nxt_eligible` | Korea Investment & Securities DWS static master (`new.real.download.dws.co.kr`) | `scripts/sync_kr_symbol_universe.py` | universe membership, name, exchange, `nxt_eligible`, active flag |
| `nxt_trading_suspended`, `toss_master_updated_at` | authenticated Toss Open API `/api/v1/stocks` | `scripts/sync_toss_symbol_master.py` | Toss master metadata, including NXT suspension and the freshness timestamp |

Running only `sync_kr_symbol_universe.py` does **not** refresh
`toss_master_updated_at`. Once that column is non-null, the gate prefers it to
`updated_at`, so the KR universe sync cannot clear `nxt_capability_stale`.

## 2026-07-27 incident measurement

Measurements were taken from the local production `auto_trader` database in a
read-only transaction at `2026-07-27 09:35:54+09:00`. The PostgreSQL session
timezone was `Asia/Seoul`; all timestamp columns below are `TIMESTAMPTZ`.

| Measurement | Result |
|---|---:|
| total / active rows | 3,976 / 3,919 |
| active `nxt_eligible=true` | 606 |
| active rows stale by the gate's effective as-of | 3,918 |
| stale active NXT-eligible rows | 606 |
| active rows with a stale Toss timestamp | 3,919 |
| missing effective as-of | 0 |

For `052690` (한전기술):

- `nxt_eligible=true`, `nxt_trading_suspended=false`, `is_active=true`
- `toss_master_updated_at=2026-06-15 08:00:04.772008+09:00`
  (`2026-06-14 23:00:04.772008Z`)
- `updated_at=2026-07-21 11:49:05.410071+09:00`, but it is ignored because the
  Toss timestamp is non-null
- age when proposal `0f17e182-f3f1-44a0-b2ad-6dd2199d4764` was created:
  `42 days 00:15:56.825973`

The proposal row has no dispatch attempt/state. The initial dispatch boundary
returned before minting a nonce or starting the Telegram dispatch ledger.

## Manual operation

### Ownership and cadence

- Primary: the KR trading operator; backup: the trading on-call.
- During the manual-validation phase, run before `07:30 Asia/Seoul` on every KR
  trading day, including the first trading day after a weekend or long holiday.
- Do not wait for the 48-hour gate threshold. Treat a successful refresh older
  than 24 hours as a warning and older than 36 hours as an escalation.
- Record the dry-run and commit packets in the operator log. A command exit
  code alone is not sufficient evidence.

Use only this worktree:

```bash
cd /Users/mgh3326/work/auto_trader.nxtfresh
export ENV_FILE=/Users/mgh3326/services/auto_trader/shared/.env.prod.native
```

### 1. Read-only preflight

```bash
psql -X -v ON_ERROR_STOP=1 -P pager=off -d auto_trader -c "
BEGIN TRANSACTION READ ONLY;
SELECT clock_timestamp() AS checked_at,
       count(*) FILTER (WHERE is_active) AS active_rows,
       count(*) FILTER (
         WHERE is_active
           AND (
             COALESCE(toss_master_updated_at, updated_at) IS NULL
             OR clock_timestamp()
                - COALESCE(toss_master_updated_at, updated_at)
                > interval '2 days'
           )
       ) AS gate_stale_rows,
       min(COALESCE(toss_master_updated_at, updated_at))
         FILTER (WHERE is_active) AS oldest_effective_asof,
       max(COALESCE(toss_master_updated_at, updated_at))
         FILTER (WHERE is_active) AS newest_effective_asof
FROM kr_symbol_universe;
ROLLBACK;"
```

The subtraction stays inside PostgreSQL between timezone-aware values. Do not
strip timezone information or reinterpret a naive timestamp as UTC.

### 2. Validate the KIS eligibility universe

```bash
uv run python scripts/sync_kr_symbol_universe.py --dry-run
```

On `2026-07-27 09:41+09:00`, all four downloads returned HTTP 200:

- KOSPI base: 2,096 valid rows
- KOSDAQ base: 1,823 valid rows
- NXT KOSPI: 336 symbols
- NXT KOSDAQ: 270 symbols
- merged snapshot: 3,919 rows, 606 NXT-eligible
- database diff: inserted 0, updated 0, deactivated 0

Reject the run if any download or parse fails, either NXT file unexpectedly has
zero valid rows, totals move materially without a reviewed exchange event, or
the diff is not understood. This dry-run performs SELECTs only and never
mutates or flushes ORM rows.

If a reviewed KIS universe change must be applied, the operator hands this
exact command to the orchestrator:

```bash
uv run python scripts/sync_kr_symbol_universe.py
```

That command is **not** the freshness repair.

### 3. Validate the Toss NXT capability data

```bash
uv run python -m scripts.sync_toss_symbol_master \
  --market kr --all --no-market-cap
```

The 2026-07-27 dry-run result was:

```text
requested=3919
stocks_matched=3919
stocks_missing=0
master_updates=3919
market_cap_payloads=0
```

An additional read-only coverage check found
`nxtTradingSuspended` present for all 3,919 responses: 3,918 false and 1 true.
Thirty-one stored suspension values differ from the source, but none changes
derived NXT tradability because all 31 symbols are currently ineligible. There
are 1,198 non-timestamp Toss metadata differences. The commit therefore
updates all 3,919 rows (at minimum their Toss timestamp and ORM `updated_at`);
`--no-market-cap` prevents valuation snapshot writes.

Accept only `requested == stocks_matched == active_rows` and
`stocks_missing == 0`. Any partial result requires investigation and a new
full dry-run.

### 4. Freshness commit (orchestrator only)

After the accepted dry-run, the exact command is:

```bash
uv run python -m scripts.sync_toss_symbol_master \
  --market kr --all --no-market-cap --commit
```

This is the command that refreshes the approval gate's as-of. The trading
operator must not substitute the KR universe command.

### 5. Post-commit proof

Re-run the read-only preflight, then verify the incident symbol:

```bash
psql -X -v ON_ERROR_STOP=1 -P pager=off -d auto_trader -c "
BEGIN TRANSACTION READ ONLY;
SELECT symbol, nxt_eligible, nxt_trading_suspended,
       toss_master_updated_at, updated_at,
       clock_timestamp() - toss_master_updated_at AS toss_age
FROM kr_symbol_universe
WHERE symbol = '052690';
ROLLBACK;"
```

Success requires:

- `gate_stale_rows=0`
- all active rows have a recent effective as-of
- `052690.toss_master_updated_at` is from the just-completed run
- no missing Toss stocks were reported

Do not create a live proposal or send an order merely to test freshness.

## Failure modes

### KR universe sync

- HTTP error, timeout, invalid ZIP/member, CP949 decode error, malformed row,
  empty base universe, or an NXT symbol absent from the base snapshot raises
  before database application. The normal sync transaction is atomic.
- A syntactically valid but incomplete base snapshot can deactivate omitted
  active symbols.
- Syntactically valid empty or incomplete NXT files are not protected by a
  minimum-coverage guard and can flip many/all `nxt_eligible` values to false.
- It never changes `nxt_trading_suspended` or `toss_master_updated_at`, so
  successful execution can still leave the approval gate stale.

### Toss master sync

- Authentication, transport, or parsing exceptions abort the transaction; an
  ordinary source failure does not commit partial ORM changes.
- A 200 response that omits individual stocks is reported as
  `stocks_missing`; matched rows can still be committed while omitted rows
  preserve their old values. The manual equality check is therefore mandatory.
- A missing `nxtTradingSuspended` field maps to `NULL`. Because derived
  tradability treats suspension as blocking only when it is explicitly true,
  field coverage must be reviewed before commit.
- `--all --commit` refreshes more Toss master metadata than NXT alone. The
  measured non-timestamp impact was 1,198 rows; `--no-market-cap` removes the
  separate valuation write path.

## Automation recommendation (not activated here)

Choose **TaskIQ** for the future write job:

- the provider clients, transaction boundary, models, production credentials,
  and KST scheduler already live in this application runtime;
- `symbols.kr.universe.sync` already runs at `07:10 Asia/Seoul`, proving the
  worker/scheduler path is deployed;
- Prefect would add cross-repository release/version coupling to a write path;
- launchd would add another singleton process/plist with weaker application
  result and retry semantics.

The existing `07:10` task only runs the KIS universe sync and therefore does
not solve Toss freshness. A follow-up automation change should add a separate
TaskIQ unit for the Toss NXT refresh (or explicitly orchestrate both sources),
default its schedule and commit gates off, require full response and suspension
field coverage, raise on failure so TaskIQ/Sentry sees a failed run, and enable
it only after a reviewed manual baseline. Do not silently return
`{"status": "failed"}` from an automated task.

## Freshness monitoring recommendation (not implemented here)

The existing 15-minute Prefect `Pipeline Result Freshness Monitor` can carry
this check. It already has:

- a read-only asyncpg transaction and statement timeout;
- stable `problem_keys`, alert deduplication/repeat, recovery notification, and
  Discord delivery;
- the production Auto Trader database URL resolution.

Add a database-side query that returns, at minimum:

- active row count;
- stale count using the exact gate expression
  `COALESCE(toss_master_updated_at, updated_at)`;
- missing effective as-of count;
- active/NXT-eligible stale counts;
- oldest/newest effective as-of and age;
- Toss timestamp coverage separately, so unrelated `updated_at` changes cannot
  mask a missing Toss refresh.

Use PostgreSQL `clock_timestamp() - TIMESTAMPTZ` (or epoch seconds computed in
SQL) and return timezone-aware ISO values. Do not pass a database-naive value
through the monitor's current `_as_utc`, which labels naive datetimes as UTC
and can create the known +9-hour skew.

Suggested alerting:

- warning at 24 hours since the last complete refresh;
- critical at 36 hours, leaving 12 hours before the 48-hour gate;
- immediate critical when any row is already gate-stale, coverage is
  incomplete, or the query fails;
- stable keys such as `nxt-tradable:freshness`,
  `nxt-tradable:coverage`, and `nxt-tradable:read-failed`.

Prefect is recommended for this read-only cross-pipeline observer, not for the
application-owned write job.
