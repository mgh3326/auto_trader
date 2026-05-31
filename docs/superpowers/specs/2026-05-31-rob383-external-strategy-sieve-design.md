# ROB-383 — External Crypto Strategy Sieve for Binance Demo Strategy Pack v0

**Design spec — Phase 1–2 (this PR). Phase 3–4 = follow-up under the same issue.**

Date: 2026-05-31 · Branch: `rob-383` · Status: design approved, proceeding to plan.

---

## 1. Context & goal

Build a bounded **external crypto strategy sieve** that turns public strategy
sources (Freqtrade/GitHub, large public bots, TradingView, QuantConnect,
commercial marketplaces) into a ranked, pre-registered shortlist, and validates a
small subset for *possible* Binance Demo Spot/Futures strategy-pack inclusion.

This issue is **candidate discovery + scoring + classification**, NOT Demo
activation. Final candidate classes: `demo_ready_candidate`, `shadow_candidate`,
`research_candidate`, `reject`.

### Primary goal framing (decided with operator)

Given the accumulated negative evidence on short-horizon crypto strategies in this
repo — ROB-382 `no_decisive_survivor` (ichi/vwap/elliot/cluc), ROB-342 close-book
on short-horizon crypto reversal (negative even at 0 bps), ROB-353 all three
families screened out on **gross** ("fees not the bottleneck"), and
ROB-316/320/324/339 net-negative — the realistic primary deliverable is **a
reusable sieve pipeline + a clean, evidence-backed reject catalog**, not a
demo-ready survivor. A run that produces 0 `demo_ready` candidates is still a
success if the pipeline and reject reasoning are sound. Expected Phase 4 output is
0–2 `demo_ready`, 1–3 `shadow`, the rest `research`/`reject`.

## 2. Scope split

- **This PR (Phase 1–2):** deterministic, web-free infrastructure — candidate-card
  schema, frozen scoring rubric, pure stdlib scorer (score → disposition →
  shortlist freeze), seed catalog, tests, runbook skeleton, and a **survey-plan
  handoff file** for a separate web-survey session.
- **Follow-up (Phase 3–4):** verified-catalog survey (separate session), then
  validation of 3–5 candidates reusing existing data seams, then the Demo
  strategy-pack v0 recommendation.

If scope grows, Phase 1–2 lands first; Phase 3 validation is a follow-up
PR/comment under the same issue.

## 3. File layout (this PR)

```
research/nautilus_scalping/external_strategy_sieve/
├── __init__.py
├── schema.py        # candidate-card dataclass + enums + validation
├── rubric.py        # frozen scoring rubric: weights, gates, RUBRIC_VERSION + config_hash()
├── scorer.py        # pure stdlib: cards → scores → disposition → bucketed output + shortlist freeze
├── catalog.py       # YAML loader + integrity guards over the card list
├── candidates.json  # seed 8–12 cards, all source_verified=false / score_status=unverified_seed
└── tests/
    ├── __init__.py
    ├── test_schema.py
    ├── test_scorer.py      # weighted-sum boundaries, determinism, rubric-hash pin
    └── test_integrity.py   # R1–R8 integrity rules
docs/plans/ROB-383-external-strategy-survey-plan.md   # handoff for codex/gemini/agy (read-only web survey)
docs/runbooks/external-crypto-strategy-sieve.md       # human report + safety boundaries + pipeline usage
```

All code is pure stdlib (no `app` import, no network), runnable with
`uv run --no-project` — matching the ROB-351/353 research-venv boundary
(`nautilus_trader` is NOT in the research venv).

## 4. Candidate card schema (`schema.py`)

Captures every Phase-1 field from the issue plus integrity-status fields.

**Identity / source**
- `candidate_id: str`
- `source_url: str` (pointer only — never a raw page dump)
- `source_bucket: enum` — `freqtrade_github | large_public_bot | tradingview | quantconnect | commercial_marketplace`

**Metadata**
- `license: str`
- `code_availability: enum` — `open | partial | opaque | code_not_confirmed`
- `strategy_family: enum` — `trend | mean_reversion | breakout | atr_trail | grid_dca | market_making | volatility | regime_filter | other`
- `spot_or_futures: enum` — `spot | futures | both`
- `long_short: enum` — `long_only | short_only | both`
- `timeframe: str`, `holding_horizon: str`
- `entry_exit_summary: str`
- `data_requirements: list[enum]` — `ohlcv | funding | oi | orderbook | liquidation | fundamentals | other`

**Risk**
- `tail_risk_flags: list[enum]` — `dca | martingale | grid | unlimited_averaging | leverage | no_stoploss`
- `lookahead_repaint_risk: enum` — `none | low | medium | high`
- `implementation_complexity: enum` — `low | medium | high`
- `novelty_vs_failed_families: enum` — `duplicate | adjacent | novel`
- `expected_cost_sensitivity: enum` — `low | medium | high`

**Integrity status (core)**
- `source_verified: bool` (default `false`)
- `score_status: enum` — `unverified_seed | verified | taxonomy_only | source_unavailable | code_not_confirmed | reject`
- `recommended_disposition_pre_validation: enum` — `keep | shadow_only | reject`

Validation: enum membership, required non-null fields per status (see R2), and a
`validate()` that returns structured errors rather than raising on the first
problem.

## 5. Frozen scoring rubric (`rubric.py`)

Hybrid: weighted-sum composite **+ hard gates/caps**. (Pure lexicographic
cascade is too rigid; pure weighted-sum lets a high composite "launder" a
martingale into the shortlist — hence the independent caps.)

### Weighted criteria (each scored 0–3 integer)

| # | criterion | weight | direction |
|---|-----------|:------:|-----------|
| 1 | `source_hygiene_reproducibility` | ×3 | + |
| 2 | `license_safety` | ×3 | + (also G1 gate) |
| 3 | `faithful_port_feasibility` | ×3 | + |
| 4 | `data_availability_auto_trader` | ×3 | + |
| 5 | `cost_fee_survivability_potential` | ×3 | + |
| 6 | `market_fit_binance_demo` | ×2 | + |
| 7 | `novelty_vs_failed_families` | ×2 | + |
| 8 | `expected_daily_review_usefulness` | ×1 | + |
| 9 | `tail_risk_dca_dependence` | ×3 | − (penalty: subtracts) |

`composite_raw = Σ(wᵢ·scoreᵢ) − 3·tail_risk_severity`, normalized to 0–100.

### Hard gates / caps (applied independently of composite)

- **G1 license:** GPL/unclear + direct code adoption needed → cannot be `keep`;
  `shadow_only` if a clean-room spec is feasible, else `reject`.
- **G2 code opaque / `code_not_confirmed`** → faithful-port capped → at most
  `research`/`taxonomy_only`.
- **G3 severe tail-risk** (`martingale` / `unlimited_averaging` / `no_stoploss`) →
  cannot be `keep`/demo-leaning; at most `shadow_only`, else `reject`.
- **G4 `lookahead_repaint_risk == high`** → capped.
- **G5 `expected_cost_sensitivity == high`** → cannot be `keep` (ROB-353 "fees not
  the bottleneck" lesson).

### Pre-validation disposition mapping

`keep` (shortlist-eligible) / `shadow_only` / `reject`, from composite bands +
gate caps. Band thresholds live in the frozen rubric.

### Freeze enforcement

Weights, gates, and thresholds carry a `RUBRIC_VERSION` and a `config_hash()`
(dataclass → canonical json → sha256), mirroring
`research/nautilus_scalping/frozen_config.py`. A test pins the hash so any
post-hoc weight tweak is detectable, not silent. The rubric is documented in the
runbook **before** any Phase-3 validation result exists (pre-registration).

## 6. Scorer integrity rules (`scorer.py`)

| rule | requirement |
|------|-------------|
| **R1 unverified ⇒ not shortlist-eligible** | `source_verified=false` or `score_status=unverified_seed` → only a `provisional_score`, `eligible_for_shortlist=false`. Scorer **refuses** to freeze a shortlist containing any non-verified candidate. Promotion happens only after a survey session sets `source_verified=true` + `score_status=verified`. |
| **R2 no fabrication / evidence required** | `verified` requires non-null `source_url`, `license`, `code_availability`, `strategy_family`. Missing → forced to `code_not_confirmed`/`source_unavailable`, never silently scored. A card claiming `verified` without the evidence fields is flagged. |
| **R3 determinism + freeze** | Same catalog → identical scores, ranking, shortlist. Stable sort, tie-break on `candidate_id`. No clock/random. Rubric hash pinned by test. |
| **R4 family diversity** | Shortlist 6–8: ≤2 per `strategy_family`, ≥K distinct families. If the verified pool cannot meet diversity, surface the gap rather than pad with near-duplicates (no all-RSI/MACD/Bollinger shortlist). |
| **R5 tail-risk/cost hard cap** | Severe tail-risk / high cost cap the disposition independently of composite — a high composite cannot launder a martingale into the shortlist (G3/G5 applied outside the sum). |
| **R6 bucketed output** | Scorer emits `verified_ranked / unverified_seed / taxonomy_only / source_unavailable / reject`. Shortlist drawn **only** from `verified_ranked`. |
| **R7 pre-registration ordering** | Scorer input is catalog metadata only — no Phase-3 validation results feed it. Rubric frozen + documented before validation exists. |
| **R8 popularity ≠ alpha** | Leaderboard rank / GitHub stars feed only `source_bucket`/`novelty`, explicitly excluded from any edge score. Scorer never emits an "alpha" claim. |

## 7. Data flow — reuse existing cache, zero duplicate fetch (Phase 3)

Phase 1–2 fetches no data. Phase 3 reuses the two existing data seams; ROB-383
adds **no new fetcher**.

**Existing reuse / dedup mechanisms (code-confirmed):**

| data | cache location | duplicate-fetch guard |
|------|----------------|-----------------------|
| Klines (OHLCV) | `pit_data_root()/klines/{interval}/{symbol}/*.csv` | `pit_klines_fetcher.fetch_months()` — `if csv_path.exists(): continue` (line 89); cached per symbol·interval·month |
| Funding/OI | `results/discovery/rob356/features/` | resumable `_progress.jsonl` + `--resume` |
| Agg-trades | `data/{market}/{symbol}/*.csv` | per-day existence check + SHA-256 CHECKSUM |
| PIT universe | `data_manifests/pit_universe.v1.json` (committed, 843 symbols) | static manifest loaded once (ROB-349) |

**Artifact root:** all on-disk artifacts root at `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT`
(`artifact_paths.py`, ROB-339); unset → repo-internal `results/`/`data/`
(gitignored). Pointing this env at the root prior campaigns used means Phase-3
validation reads already-downloaded klines and fetches only missing months
(skip-if-exists) → zero duplication.

**Signal-injection seams (data loaded once, no re-fetch):**

- **SEAM 2 — pure bar panel (PRIMARY, ROB-351/353):** `pit_bars.load_panel(symbols,
  interval, manifest)` → one shared `{symbol: [(ts, close)]}` panel consumed by all
  families. Add candidate = new bar-based generator in `families.py` + spec in
  `campaign_specs.py` + one line in `run_rob353_campaign`. **Pure stdlib, runs
  with `uv run --no-project`, no `nautilus_trader`.** `load_panel` is
  interval-generic, so 5m/15m/1h intraday is supported on cached klines.
- **SEAM 1 — Nautilus tick-level (ROB-320):** register in `candidates.py REGISTRY`
  + `backtest_runner._run_single()` dispatch; shared `ParquetDataCatalog`. **Needs
  the `nautilus_trader` venv (ROB-316)** — only for candidates that genuinely need
  tick microstructure and only when the operator has that venv.

**Cost consistency:** all trades/periods recorded at `cost_model.REF_FEE_BPS=10`
(per leg); `validated_gate.metrics_at_fee()` rescales via `net_at_fee()`. ROB-383
candidates must emit `(net_ref_pnl, commission_ref)` at REF_FEE_BPS so the same
cost model and gate apply. Cost sensitivity must include the Binance Demo
achievable envelope (maker 2.0 / taker 4.0 bps; `frozen_config.py` fee grid
`(10, 7.5, 5, 2, 0)`).

**Commit policy:** `data/`, `results/`, `catalog/`, `*.csv`, `*.zip`, `*.parquet`
gitignored. ROB-383 counts-only outputs use
`resolve_artifact_path('discovery', 'rob383', ...)` (namespace must be
`discovery` or `gate`) → gitignored path, no raw-dump commit.

## 8. Survey-plan handoff (`docs/plans/ROB-383-external-strategy-survey-plan.md`)

Read-only instruction set for a separate session (codex / gemini / agy worker).

- **Objective:** (1) verify the 8–12 seed cards; (2) per-bucket broad live survey
  to ≥15 (ideally 25–40) structured candidates; (3) cover ≥4 source buckets; (4)
  separate verified vs unverified/taxonomy-only/reject; (5) shortlist 6–8 frozen
  only from verified (or clearly-statused) candidates.
- **Per-bucket survey:** freqtrade_github / large_public_bot (NFI = tail-risk
  audit only, no code adoption) / tradingview / quantconnect (taxonomy/clean-room)
  / commercial_marketplace (taxonomy only).
- **Verification protocol:** default `/browse` or plain fetch; **Chrome
  remote-debug fallback** (read-only) only for JS-rendered pages / TradingView code
  panels / accessibility issues:
  ```bash
  open -na "Google Chrome" --args \
    --remote-debugging-address=127.0.0.1 \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.hermes/chrome-toss-debug"
  ```
- **Status transition:** `unverified_seed` → `verified` only after URL, license,
  code availability, and strategy family are actually confirmed; otherwise
  `source_unavailable` / `code_not_confirmed` (never fabricate/estimate).
- **Hard rules:** no raw HTML / raw Pine / raw leaderboard dump saved; no full
  source-code copy; metadata only in cards; popularity ≠ alpha; never touch
  broker/order/DB/scheduler/secret paths.
- **Role separation:** workers (incl. agy) perform read-only survey only and
  produce structured card proposals. Repo edits, `candidates.json` merges, scorer
  runs, and shortlist freeze are done **only by Claude Code main/integrator**.

## 9. Runbook skeleton (`docs/runbooks/external-crypto-strategy-sieve.md`)

- Purpose / scope (Phase 1–2 this PR; Phase 3–4 follow-up).
- Safety boundaries (full no-list; see §10).
- Frozen rubric documentation (weight table + `RUBRIC_VERSION` + hash) —
  pre-registration, before validation results.
- Pipeline usage: how to run the scorer, where the catalog lives, how to read the
  bucketed output.
- Candidate-catalog summary + frozen shortlist (filled after the survey session).
- Phase-3 data reuse: SEAM 2 + `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT` shared root.
- Disposition definitions (`demo_ready` / `shadow` / `research` / `reject`);
  counts-only, no alpha claim.

## 10. Safety boundaries

No live trading. No Binance Demo confirmed order placement. No
broker/order/watch/order-intent/trade-journal mutation. No scheduler / TaskIQ /
Prefect / cron / launchd / daemon activation. No prod DB writes/backfills/deletes.
No prod env/secret changes. No secret printing/copying/committing. No raw
market-data or raw web/leaderboard dumps committed. No direct import of Freqtrade /
TradingView Pine runtime / QuantConnect runtime / external bot runtime into
auto_trader execution paths. No direct GPL/unclear-license code copy — clean-room
specs only. Public ranking/popularity is never presented as alpha proof. No Demo
activation/backtest issue opened automatically.

## 11. Tests

Pure stdlib, `uv run --no-project`:
- `test_schema.py` — enum membership, per-status required fields, `validate()`
  error reporting.
- `test_scorer.py` — weighted-sum boundary values, determinism (same input → same
  output), rubric `config_hash()` pin.
- `test_integrity.py` — R1 (refuse non-verified in shortlist), R4 (diversity
  violation detected), R5 (martingale cap proven), R6 (bucket separation), R8
  (popularity excluded from edge score).

## 12. Acceptance criteria (maps to issue)

- [ ] Candidate catalog ≥15 well-described candidates across ≥4 buckets *(survey
  session; this PR delivers schema + seed 8–12 + survey plan to reach it).*
- [ ] Deterministic scoring rubric committed/documented before validation results
  are used. *(this PR)*
- [ ] Frozen shortlist of 6–8 with diversity rationale + explicit exclusions
  *(scorer produces it once the verified catalog exists).*
- [ ] 3–5 candidates validated or explicitly marked unvalidatable *(Phase 3
  follow-up).*
- [ ] Per-validated-candidate counts-only result fields *(Phase 3).*
- [ ] Final Demo strategy-pack v0 recommendation *(Phase 4).*
- [ ] Runbook under `docs/runbooks/` with no raw dumps/secrets. *(this PR
  skeleton)*
- [ ] Tests/smokes for new parser/scoring code pass locally. *(this PR)*
- [ ] PR description summarizes safety boundaries. *(this PR)*

## 13. Out of scope

Demo activation, confirmed orders, scheduler wiring, production writes, and the
Phase-3 validation runs themselves are out of scope for this PR. The Phase-3 data
seams are documented here but exercised only in the follow-up.
