# External Crypto Strategy Sieve (ROB-383)

Bounded sieve turning public crypto strategy sources into a ranked, pre-registered
shortlist for *possible* Binance Demo Spot/Futures observation. **This is candidate
discovery + scoring + classification — NOT Demo activation.** Phase 1–2 (schema,
rubric, scorer, seed catalog) is implemented here; Phase 3 validation and the
Phase 4 strategy-pack recommendation are follow-ups under the same issue.

Primary deliverable: a reusable sieve pipeline + an evidence-backed reject catalog.
Given prior negative results (ROB-382 `no_decisive_survivor`, ROB-342/353), a run
producing 0 `demo_ready` candidates is still a success.

## Safety boundaries

No live trading. No Binance Demo confirmed order placement. No
broker/order/watch/order-intent/trade-journal mutation. No scheduler / TaskIQ /
Prefect / cron / launchd / daemon activation. No prod DB writes/backfills/deletes.
No prod env/secret changes. No secret printing/copying/committing. No raw
market-data or raw web/leaderboard dumps committed. No direct import of external
strategy runtimes (Freqtrade / Pine / QuantConnect / bot) into auto_trader
execution paths. No direct GPL/unclear-license code copy — clean-room specs only.
Public ranking/popularity is never alpha proof. No Demo activation/backtest issue
opened automatically.

## Pipeline (pure stdlib, `uv run --no-project`)

Package: `research/nautilus_scalping/external_strategy_sieve/`

- `schema.py` — candidate-card fields + `validate()`.
- `rubric.py` — frozen weights/gates/thresholds + metadata→score derivation +
  `config_hash()`.
- `scorer.py` — `score_card`, `bucketize`, `freeze_shortlist`.
- `catalog.py` — `load_catalog(path)` (JSON; integrity guards).
- `candidates.json` — seed catalog (cold-start, all `unverified_seed`).

Run all tests:

```bash
cd research/nautilus_scalping
uv run --no-project pytest external_strategy_sieve/tests/ -q
```

## Frozen rubric (pre-registration record)

Recorded BEFORE any Phase-3 validation result exists. A later weight tweak changes
the hash, so an ex-post adjustment is detectable.

- `RUBRIC_VERSION`: `rob383.sieve.v1`
- `config_hash()`: `c78d65ae51a70a28864829b36ea042a78f70bd711f7f762ad9e205092d1b7937`

Weighted criteria (each derived 0–3 from card metadata):

| criterion | weight | derived from |
|-----------|:------:|--------------|
| source_hygiene_reproducibility | 3 | code_availability |
| license_safety | 3 | license class (G1 gate if ≤1) |
| faithful_port_feasibility | 3 | code_availability − complexity − repaint |
| data_availability_auto_trader | 3 | data_requirements vs {ohlcv, funding, oi} |
| cost_fee_survivability_potential | 3 | expected_cost_sensitivity (G5 gate if high) |
| market_fit_binance_demo | 2 | spot_or_futures |
| novelty_vs_failed_families | 2 | novelty field |
| expected_daily_review_usefulness | 1 | holding_horizon |
| tail_risk_dca_dependence | −3 | tail_risk_flags severity (G3 gate if severe) |

Hard gates cap disposition independent of composite: G1 license, G2 opaque code,
G3 severe tail-risk (martingale/unlimited_averaging/no_stoploss), G4 high repaint,
G5 high cost. Disposition bands: keep ≥ 65, shadow_only ≥ 45, else reject.

Integrity rules: only source-verified `verified` keep candidates are
shortlist-eligible (R1); shortlist is family-diverse (≤2/family, ≥3 families, R4);
output is bucketed verified_ranked / unverified_seed / taxonomy_only /
source_unavailable / reject (R6); popularity never enters scoring (R8).

## Candidate catalog summary

_Filled after the survey session (`docs/plans/ROB-383-external-strategy-survey-plan.md`)._
Counts-only; no raw dumps. Seed catalog ships 10 `unverified_seed` cards across 5
source buckets as cold-start pointers.

## Frozen shortlist

_Filled after verification: 6–8 candidates from the verified pool, with diversity
rationale and explicit exclusions._

## Phase 3 data reuse (no duplicate fetch)

Validation reuses existing seams; ROB-383 adds no new fetcher.

- **SEAM 2 (primary, pure):** `pit_bars.load_panel(symbols, interval, manifest)` →
  one shared panel; add a bar-based generator in `families.py` + a spec in
  `campaign_specs.py`. Runs with `uv run --no-project` (no `nautilus_trader`).
- **SEAM 1 (Nautilus tick):** register in `candidates.py` REGISTRY +
  `backtest_runner._run_single()`. Needs the `nautilus_trader` venv (ROB-316).
- Point `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT` at the root used by prior campaigns so
  `pit_klines_fetcher.fetch_months()` reuses cached klines (`if csv_path.exists():
  continue`) and downloads only missing months.
- All trades/periods recorded at `cost_model.REF_FEE_BPS=10`/leg; cost sensitivity
  must include the Demo envelope (maker 2.0 / taker 4.0 bps).
- Counts-only outputs → `resolve_artifact_path('discovery', 'rob383', ...)`
  (gitignored). Never commit raw klines / dumps.

## Disposition definitions (Phase 3–4 classes)

- `demo_ready_candidate` — small Binance Demo observation may be justified later,
  with separate operator approval.
- `shadow_candidate` — signal-only / dry-run observation candidate.
- `research_candidate` — worth preserving, not ready for Demo.
- `reject` — weak evidence, cost sensitivity, overfit, lookahead/repaint,
  tail-risk/DCA dependence, license risk, or implementation mismatch.

Counts-only, no alpha claim. Demo activation requires a separate operator-approved
issue.
