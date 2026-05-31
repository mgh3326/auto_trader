# Runbook — US Earnings Event+Price Coverage Probe (ROB-371)

Operator runbook for `scripts/probe_us_earnings_coverage.py`, the **read-only**
coverage probe that decides whether the US-earnings event→price join meets the
ROB-367 §5 readiness thresholds. This is a **data-builder / coverage gate**, not
a strategy or backtest.

- **Issue:** ROB-371 (follow-up A of ROB-367; verdict `partial`).
- **Source audit:** `docs/runbooks/rob-367-event-driven-equity-data-feasibility.md` §5.
- **Decision rule:** the probe emits a deterministic PASS/FAIL. A bounded US
  event-response **backtest issue may be opened only if the gate PASSes** — and
  is never opened by this probe.

---

## 1. What it does (and does not do)

The probe measures, for realized Finnhub US earnings events
(`source=finnhub, category=earnings, market=us, status=released, event_date ≤ today`):

1. **Lookahead-safe labeling** — each date-only event is mapped to its first
   tradable daily bar (`before_open`→next-open, `after_close`→next-close,
   `during_market`→whole-day-uncertain *and excluded*, `unknown/None`→next
   session conservatively). Intraday (`during_market`) earnings cannot be
   labeled at daily granularity (ROB-367 boundary), so they are **excluded**
   from the eligible daily-granularity population — counted in
   `intraday_excluded_events` and reported, but never measured/joined and never
   hard-failing the gate (ROB-378).
2. **`-5..+20d` window join coverage** against `us_candles_1d` (KIS primary +
   Yahoo fallback), measured against the fail-closed NYSE (XNYS) session
   calendar — counts only, never raw bars. The window is anchored on each
   event's **lookahead-safe decision session** (the next tradable session for
   AMC/unknown), not the raw `event_date`, so date-only earnings are never
   treated as intraday-tradable on the announcement day. Join-quality counts are
   measured against the **eligible** population (`eligible_events` = realized −
   intraday-excluded). Events with no mappable decision session (out of calendar
   range) are counted as `unmappable_events` and fail closed (never joinable).
3. **Survivorship** — delisted symbols (`us_symbol_universe.is_active=false`)
   are counted; with `--measure-delisted-recoverability` a bounded Yahoo probe
   measures (does not assume) delisted-bar recoverability.
4. **Benchmark** — SPY + GICS sector SPDRs window coverage per eligible event.
   The benchmark symbol list is an explicit hard-coded constant (not resolved
   from a universe); its bars are READ from the pre-materialized `us_candles_1d`
   store — the probe does NOT live-fetch benchmark bars during the gate, so each
   benchmark symbol must already be backfilled (see §8).
5. **§5 gate** — a deterministic PASS/FAIL verdict + per-criterion breakdown.

**It never** writes to any database, mutates broker/order/watch/approval state,
activates a scheduler, or commits raw data. The only network call is the opt-in
Yahoo delisted-recoverability probe (read-only).

---

## 2. §5 readiness thresholds (the gate)

| Criterion | Threshold | Field |
|---|---|---|
| `min_realized_events` | ≥ 500 | `realized_events` |
| `min_joinable_symbols` | ≥ 200 | `joinable_symbols` (distinct symbols with ≥1 joinable event) |
| `min_joinable_event_ratio` | ≥ 0.90 | `joinable_events / eligible_events` (event joinable iff window coverage ≥ 90%) |
| `intraday_excluded` | reported, **not gated** | `intraday_excluded_events` |
| `max_dup_ambiguous` | ≤ 0.01 | `dup_ambiguous_ratio` (US Finnhub: NULL-symbol ratio) |
| `min_tradability` | ≥ 0.90 | `tradability_coverage` (joinable symbols with ≥1 `volume>0` bar) |
| `min_benchmark` | ≥ 0.90 | `benchmark_coverage` (eligible events with ≥1 benchmark window ≥ 90%) |
| `session_calendar_present` | true | XNYS calendar resolvable |

`date_only_ratio` and `unknown_time_ratio` are **recorded but not gated** — per
§5 any ratio is accepted for equities once intraday labeling is forbidden.

**Intraday-exclude policy (ROB-378).** `during_market` earnings cannot be
labeled at daily granularity, so they are **excluded** from the eligible
population rather than hard-failing the gate. They are surfaced via the
non-gating `intraday_excluded` criterion and the `intraday_excluded_events` /
`eligible_events` measurement fields. **Thresholds are unchanged** — exclusion
only moves intraday events out of the denominators (`eligible_events` = realized
− intraday-excluded), it does not relax any bar. If *every* realized event is
intraday (`eligible_events == 0`), the verdict is the dedicated `FAIL — all …
intraday/excluded` shape (a scope limit, not a quality failure).

---

## 3. Running the probe

### Dry-run (default — no DB, no secrets)

```bash
uv run python -m scripts.probe_us_earnings_coverage \
    --from-date 2024-01-01 --to-date 2025-05-30
```

Prints a `[DRY-RUN]` line and exits 0. Safe anywhere.

### Read-only measurement

```bash
uv run python -m scripts.probe_us_earnings_coverage \
    --from-date 2024-01-01 --to-date 2025-05-30 --run --out
```

- `--run` performs the read-only DB measurement (safe against production —
  reads only).
- `--out` writes the counts-only artifact to
  `$AUTO_TRADER_RESEARCH_ARTIFACT_ROOT/event_coverage/us_earnings_coverage.json`
  (or the gitignored `research/event_coverage/results/event_coverage/…`
  fallback when the env var is unset).
- `--measure-delisted-recoverability [--delisted-sample N]` adds the opt-in
  Yahoo probe (network, read-only; default sample 10).

**Exit codes:** `0` = gate PASS, `1` = gate FAIL, `2` = error.

---

## 4. Interpreting the verdict

The verdict prefix is machine-parsed (`PASS` / `FAIL`). There are four FAIL
shapes — only the last is a true data-quality failure:

1. **`FAIL — no earnings events found in the date range.`** The window has no
   realized events. Widen `--from-date`/`--to-date` or wait for ingestion. Not a
   quality failure.
2. **`FAIL — all N realized events are intraday/excluded …`** Events exist but
   every one is `during_market` (excluded from the eligible daily-granularity
   population), so `eligible_events == 0`. A scope limit (intraday labeling is
   forbidden), not a join-quality or build failure.
3. **`FAIL — coverage not materialized: N eligible events but 0 have daily
   bars.`** Eligible events exist but `us_candles_1d` has no bars for them.
   Materialize a dev-DB window (§5 below) and re-probe. A **build gap**, not a
   join failure.
4. **`FAIL — §5 thresholds not met: <criteria>.`** Genuine coverage shortfall;
   the named criteria say what is missing.
5. **`PASS — §5 thresholds met; a bounded US event-response backtest issue MAY
   be opened.`** Record the artifact in Linear; opening the backtest issue is a
   separate, explicit decision (this probe does not open it).

---

## 5. Materializing a dev-DB window (separate, operator-run)

The probe never writes. When the verdict is *coverage not materialized*, build
the window with the **existing** daily-candle backfill CLI, pointed at a **dev
database only** (`DATABASE_URL` → a dev DB — never production):

```bash
# Confirm you are on a DEV database first.
echo "$DATABASE_URL"

# Backfill the event symbols + benchmarks (US). ~60 bars covers a -5..+20d
# window with margin; raise --horizon-bars for longer measurement spans.
uv run python scripts/backfill_daily_candles.py \
    --market us --symbols AAPL,MSFT,NVDA,SPY,XLK,XLF --horizon-bars 60
```

Then re-run the read-only probe (§3) and read the new verdict. Backfilling
production is out of scope for ROB-371.

---

## 6. Safety evidence (ROB-371 AC7)

- **No mutation:** the probe issues only `SELECT`s; the new CLI has no write
  path. Backfill is a separate, pre-existing, operator-run tool.
- **No scheduler:** no TaskIQ / Prefect / cron registration is added.
- **No prod DB write:** `--run` is read-only; dev-DB backfill is explicit and
  manual.
- **No raw-data / secrets committed:** the artifact is counts-only (a frozen
  dataclass of scalars; the CLI emits `asdict`, so no symbol/bar-date arrays can
  leak) and lands in a gitignored fallback unless `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT`
  redirects it outside the tree.

---

## 7. Related code

- Calendar: `app/services/market_events/session_calendar.py`
- Labeler: `app/services/market_events/earnings_decision_time.py`
- Holiday-aware expected sources: `app/services/market_events/expected_sources.py`
- Measurement: `app/services/market_events/us_earnings_coverage.py`
- §5 gate: `app/services/market_events/coverage_gate.py`
- Artifact root: `research/event_coverage/artifact_paths.py`
- CLI: `scripts/probe_us_earnings_coverage.py`
- Finnhub ingest CLI: `scripts/ingest_market_events.py`
- Daily-candle backfill CLI: `scripts/backfill_daily_candles.py`

---

## 8. Operator checklist — ROB-378 (historical-window materialization + re-run)

ROB-371 built the probe; **ROB-378 executes it** against a historical window and
ends with a deterministic PASS/FAIL artifact + a decision on whether a bounded
backtest issue is justified. This section is the full operator journey. It is
**dev/research-DB only** — never production. The probe and this checklist make
**no** broker/order/watch/order-intent mutation and activate **no** scheduler.

> **Why this exists.** The first ROB-371 operator run FAILed only on missing
> dev/research data (benchmark ETF bars absent → benchmark join 0.0; no
> historical Finnhub earnings → only recent partially-materialized windows;
> intraday events tripping the old hard gate). None of those are infeasibility
> verdicts. ROB-378 removes the data prerequisites and re-runs.

### 8.0 Pre-flight — confirm a DEV/RESEARCH database

```bash
echo "$DATABASE_URL"   # MUST be a dev/research DB. Never production.
```

Stop here if this is not a dev/research DB. All steps below write candles/events
to whatever `DATABASE_URL` points at.

### 8.1 SPY one-symbol eligibility check (no-write, then bounded live)

Benchmark ETFs (SPY + sector SPDRs) are NYSE-Arca listed. The US backfill
defaults to `--partition NASD`; the KIS overseas fetcher only maps
`NASD/NYSE/AMEX` (unknown codes fall back to the first 3 chars, which KIS may
reject). **Validate SPY before committing to a full backfill.**

```bash
# (a) No-write dry-run: confirms the CLI builds the SyncTarget (no API, no write).
uv run python scripts/backfill_daily_candles.py \
    --market us --symbols SPY --partition NYSE --dry-run

# (b) Bounded LIVE single-symbol probe (writes ~50 SPY bars to the DEV DB only).
#     This is the real eligibility test — it proves KIS/Yahoo returns ETF bars
#     for the chosen --partition. Try NYSE first; if KIS returns 0 rows the sync
#     logs a Yahoo-fallback attempt (Yahoo is partition-agnostic for SPY).
uv run python scripts/backfill_daily_candles.py \
    --market us --symbols SPY --partition NYSE --horizon-bars 50
```

If (b) reports `upserted=0 fallback=...` for every partition tried, record that
benchmark ETF bars are not retrievable on this host and treat benchmark coverage
as a remaining prerequisite (do **not** fudge it). `ARCA` is **not** in the KIS
exchange map — prefer `NYSE` (or rely on the Yahoo fallback) over passing `ARCA`.

### 8.2 Choose a trailing `--horizon-bars` for the historical window

`backfill_daily_candles.py` is **trailing-horizon** (counts back from *today*);
it has no `--from/--to`. To cover the earliest event's `-5d` lookback through its
`+20d` lookahead, size the horizon to reach the earliest `event_date` plus
margin:

```
trading_days ≈ (today − earliest_event_date in calendar days) / 365 × 252
horizon_bars = trading_days + 25   # -5d..+20d window margin, rounded up
```

For an earliest event of **2023-01-01** measured on **~2026-05-30**
(~1,245 calendar days): `1245/365×252 ≈ 857` + margin → **use `--horizon-bars 900`**
(round up; over-fetching dev bars is harmless).

### 8.3 Ingest historical Finnhub US earnings (DEV/RESEARCH only)

Events must exist before you know which symbols to backfill. Requires the
`FINNHUB_API_KEY` env var (name only — never print the value). One Finnhub API
call per invocation; quota exhaustion fails closed (`finnhub_quota_exceeded`).

```bash
uv run python scripts/ingest_market_events.py \
    --source finnhub --category earnings --market us \
    --from-date 2023-01-01 --to-date 2025-12-31 --dry-run   # inspect first

uv run python scripts/ingest_market_events.py \
    --source finnhub --category earnings --market us \
    --from-date 2023-01-01 --to-date 2025-12-31             # writes events
```

Only rows with realized `eps_actual`/`revenue_actual` normalize to
`status="released"` (the probe filters on that), so the populated count may be
below the raw calendar count — expected.

### 8.4 Backfill event-symbol + benchmark windows (DEV/RESEARCH only)

Backfill **all 12 benchmark symbols** plus the event symbols you intend to
measure, at the §8.2 horizon:

```bash
# Benchmarks (all 12 — partition per §8.1 finding):
uv run python scripts/backfill_daily_candles.py \
    --market us --partition NYSE --horizon-bars 900 \
    --symbols SPY,XLK,XLF,XLE,XLV,XLI,XLY,XLP,XLU,XLB,XLRE,XLC

# Event symbols (derive the symbol list from the ingested events; common stocks
# default to --partition NASD, override per-symbol as needed):
uv run python scripts/backfill_daily_candles.py \
    --market us --partition NASD --horizon-bars 900 \
    --symbols <comma-separated event symbols>
```

### 8.5 Re-run the probe with UNCHANGED thresholds

```bash
uv run python -m scripts.probe_us_earnings_coverage \
    --from-date 2023-01-01 --to-date 2025-12-31 --run --out
```

**Do NOT edit `Section5Thresholds()` or the gate logic to manufacture a PASS.**
The probe always constructs the default thresholds; the gate must stay
apples-to-apples with the recorded §5 table (§2). The intraday-exclude policy is
already built in — `during_market` events are reported in
`intraday_excluded_events`, not hard-failed.

### 8.6 Record the counts-only artifact + verdict

`--out` writes `us_earnings_coverage.json` (counts-only scalars). Record in the
ROB-378 Linear issue:

- the artifact path (or its JSON — it is counts-only and safe to paste);
- the machine-parsed verdict string;
- the §5 per-criterion table (observed vs threshold);
- `eligible_events` and `intraday_excluded_events` so the intraday exclusion is
  auditable.

**Never** paste raw bars/events, symbol lists, or secret values.

### 8.7 Decision

- **PASS** → a bounded US event-response backtest issue **may** be opened as a
  separate, explicit decision. This issue does not open it.
- **FAIL** → do **not** open a backtest issue. List the remaining prerequisites
  from the verdict shape (no events / no eligible population / not materialized /
  thresholds not met) and stop. A FAIL here is a data/scope gap, not an
  infeasibility verdict for event-driven equity research.
