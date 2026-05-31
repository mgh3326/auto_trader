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
- `candidates.json` — the catalog. Shipped as 10 cold-start `unverified_seed`
  pointers, now superseded by the verified survey output (27 cards).

Run all tests:

```bash
cd research/nautilus_scalping
uv run --no-project pytest external_strategy_sieve/tests/ -q
```

## Frozen rubric (pre-registration record)

Recorded BEFORE any Phase-3 validation result exists. A later weight tweak changes
the hash, so an ex-post adjustment is detectable.

- `RUBRIC_VERSION`: `rob383.sieve.v2`
- `config_hash()`: `83a46512eef82b7fecb9865b296fe916632f376778976b66c05f97e0ba6aff9a`

**v1 → v2 change (transparent, before any Phase-3 validation):** v1 made only
`keep` disposition shortlist-eligible. The survey showed every source-verified
candidate is capped to `shadow_only` because public crypto strategies are almost
all GPL/unclear-license (G1) or cost-sensitive (G5) — so a keep-only shortlist is
structurally empty for this domain, and the issue explicitly sanctions clean-room
handling of GPL. v2 fixes that category error: shortlist eligibility is
`verified + disposition ∈ {keep, shadow_only}` (excludes only `reject`). No weight
was changed to admit a specific candidate; the version bump + new hash make the
change detectable, not silent. v1 hash was
`c78d65ae51a70a28864829b36ea042a78f70bd711f7f762ad9e205092d1b7937`.

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

Integrity rules: only source-verified `verified`, non-rejected candidates are
shortlist-eligible (R1, v2); shortlist is family-diverse (≤2/family, ≥3 families,
R4); output is bucketed verified_ranked / unverified_seed / taxonomy_only /
source_unavailable / reject (R6); popularity never enters scoring (R8).

## Candidate catalog summary

Survey session (per `docs/plans/ROB-383-external-strategy-survey-plan.md`) produced
**27 candidates** (counts-only; no raw dumps). Validated clean via `load_catalog`
(0 errors).

- **Source buckets (5):** freqtrade_github 7 · tradingview 8 · large_public_bot 4 ·
  quantconnect 4 · commercial_marketplace 4.
- **Families (9):** trend 6 · grid_dca 4 · market_making 4 · atr_trail 3 ·
  volatility 3 · mean_reversion 2 · other 2 · regime_filter 2 · breakout 1.
- **Status:** 23 `verified` · 4 `taxonomy_only` (all four commercial-marketplace
  DCA/grid/MM bots — opaque source, `source_verified=false` → excluded from the
  shortlist by R1).
- **Disposition (scorer, authoritative):** all 23 verified candidates land
  `shadow_only` — none reaches `keep` because every one trips G1 (GPL/unclear
  license) or G5 (cost), confirming the v2 rationale above. The worker's advisory
  `recommended_disposition_pre_validation` sometimes differs (e.g. it tagged
  `freqtrade_pattern_recognition` as reject); the deterministic scorer is the
  authority and the field is advisory only.

## Frozen shortlist

8 candidates, drawn only from the verified pool, composite-ranked, family-diverse
(≤2/family, ≥3 distinct → here 5 families). No gaps. **Selection is deterministic
pre-validation, not an alpha claim** — Phase 3 winnows these to 3–5.

| # | candidate_id | family | composite | gates |
|---|--------------|--------|:---------:|-------|
| 1 | `tv_squeeze_momentum` | volatility | 82.6 | G1 |
| 2 | `tv_chandelier_exit` | atr_trail | 79.7 | G1 |
| 3 | `freqtrade_pattern_recognition` | other | 78.3 | G1 |
| 4 | `freqtrade_supertrend` | atr_trail | 78.3 | G1 |
| 5 | `tv_squeeze_momentum_strategy` | volatility | 73.9 | G1 |
| 6 | `freqtrade_bandtastic` | mean_reversion | 72.5 | G1, G5 |
| 7 | `freqtrade_bbrsi_naive` | mean_reversion | 72.5 | G1, G5 |
| 8 | `tv_range_filter` | trend | 72.5 | G1 |

Every shortlisted candidate is `shadow_only` (G1 license cap → clean-room path
required for Phase-3 validation; no GPL/Pine code is copied). Diversity: volatility
2, atr_trail 2, mean_reversion 2, other 1, trend 1.

**Explicit exclusions:** the 4 `taxonomy_only` commercial-marketplace bots
(`marketplace_3commas_dca_template`, `marketplace_3commas_grid_bot`,
`marketplace_cryptohopper_market_making`, `marketplace_cryptohopper_dca`) —
opaque source / not source-verified, and three carry severe tail-risk
(DCA/martingale/grid). Verified candidates that scored `reject` are likewise out of
the shortlist.

**Phase-3 selection note:** the 3 `market_making` Hummingbot candidates
(Apache-2.0, escape G1 but hit G5) and other orderbook-dependent entries are
unlikely to be faithfully validatable in SEAM 2 (auto_trader has only
klines/aggTrades, not L2 orderbook) — expect them to land `research_candidate`
(data gap) rather than be force-ported.

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
