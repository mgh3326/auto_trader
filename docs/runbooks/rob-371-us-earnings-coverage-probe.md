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
   `during_market`→whole-day-uncertain *and rejected*, `unknown/None`→next
   session conservatively). Intraday labeling is forbidden and counted; any
   intraday-labeled event hard-fails the gate.
2. **`-5..+20d` window join coverage** against `us_candles_1d` (KIS primary +
   Yahoo fallback), measured against the fail-closed NYSE (XNYS) session
   calendar — counts only, never raw bars.
3. **Survivorship** — delisted symbols (`us_symbol_universe.is_active=false`)
   are counted; with `--measure-delisted-recoverability` a bounded Yahoo probe
   measures (does not assume) delisted-bar recoverability.
4. **Benchmark** — SPY + GICS sector SPDRs window coverage per event.
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
| `min_joinable_event_ratio` | ≥ 0.90 | `joinable_events / realized_events` (event joinable iff window coverage ≥ 90%) |
| `no_intraday_labeling` | == 0 | `intraday_labeled_events` |
| `max_dup_ambiguous` | ≤ 0.01 | `dup_ambiguous_ratio` (US Finnhub: NULL-symbol ratio) |
| `min_tradability` | ≥ 0.90 | `tradability_coverage` (joinable symbols with ≥1 `volume>0` bar) |
| `min_benchmark` | ≥ 0.90 | `benchmark_coverage` (events with ≥1 benchmark window ≥ 90%) |
| `session_calendar_present` | true | XNYS calendar resolvable |

`date_only_ratio` and `unknown_time_ratio` are **recorded but not gated** — per
§5 any ratio is accepted for equities once intraday labeling is forbidden (which
`no_intraday_labeling` enforces directly).

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

The verdict prefix is machine-parsed (`PASS` / `FAIL`). There are three FAIL
shapes — only the third is a true data-quality failure:

1. **`FAIL — no earnings events found in the date range.`** The window has no
   realized events. Widen `--from-date`/`--to-date` or wait for ingestion. Not a
   quality failure.
2. **`FAIL — coverage not materialized: N realized events but 0 have daily
   bars.`** Events exist but `us_candles_1d` has no bars for them. Materialize a
   dev-DB window (§5 below) and re-probe. A **build gap**, not a join failure.
3. **`FAIL — §5 thresholds not met: <criteria>.`** Genuine coverage shortfall;
   the named criteria say what is missing.
4. **`PASS — §5 thresholds met; a bounded US event-response backtest issue MAY
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
