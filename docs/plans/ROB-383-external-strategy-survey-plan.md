# ROB-383 — External Strategy Survey Plan (web-survey session handoff)

**Read-only web survey for codex / gemini / agy workers.** This session does NOT
edit the repo. Workers produce structured candidate-card proposals (JSON objects
matching `research/nautilus_scalping/external_strategy_sieve/schema.py`). Repo
edits — merging into `candidates.json`, running the scorer, freezing the shortlist
— are done ONLY by the Claude Code main/integrator.

## Objective

1. Verify the 8–12 cold-start seeds in `external_strategy_sieve/candidates.json`.
2. Per-bucket broad live survey to ≥15 (ideally 25–40) structured candidates.
3. Cover ≥4 source buckets.
4. Separate verified vs unverified / taxonomy-only / reject.
5. The integrator freezes a 6–8 shortlist from verified candidates only.

## Card status discipline (critical)

- Seeds ship as `source_verified=false` / `score_status="unverified_seed"`.
- Promote a card to `score_status="verified"` (+ `source_verified=true`) ONLY after
  you have actually confirmed, from the source: `source_url`, `license`,
  `code_availability`, and `strategy_family`.
- If you cannot reach/confirm a source: set `score_status="source_unavailable"` or
  `"code_not_confirmed"`. Never fabricate or estimate metadata.
- Taxonomy/trend-only finds (no reproducible source): `score_status="taxonomy_only"`.
- The scorer treats only `verified` cards as shortlist-eligible — unverified seeds
  cannot enter the shortlist no matter how good their metadata looks.

## Per-bucket survey

| bucket | where | what to record / cautions |
|--------|-------|---------------------------|
| `freqtrade_github` | freqtrade/freqtrade-strategies, other open Freqtrade repos, strat.ninja leaderboard | family, timeframe, entry/exit, license (most are GPL → clean-room only). Don't re-test ROB-382's ichi/vwap/elliot/cluc as new. |
| `large_public_bot` | iterativv/NostalgiaForInfinity | tail-risk/DCA audit FIRST; treat as black-box feasibility, not code adoption. |
| `tradingview` | top/trending open-source Pine | distinguish `indicator()` vs `strategy()`; flag repaint / lookahead / HTF `request.security` risk; license is often unclear. |
| `quantconnect` | community research | taxonomy / clean-room idea extraction; crypto-relevant only if practical. |
| `commercial_marketplace` | Cryptohopper / 3Commas | taxonomy/trend scanning only unless source + reproducible assumptions exist. |

## Verification protocol

- Default: gstack `/browse` or plain fetch.
- **Chrome remote-debug fallback (read-only)** — only for JS-rendered pages,
  TradingView code panels, or accessibility issues:

  ```bash
  open -na "Google Chrome" --args \
    --remote-debugging-address=127.0.0.1 \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.hermes/chrome-toss-debug"
  ```

  Use it strictly to read metadata. Do not log in to or mutate any site.

## Hard rules

- No raw HTML / raw Pine / raw leaderboard dumps saved or committed.
- No full source-code copy. Record metadata only: URL, license, code availability,
  indicator-vs-strategy, repaint/lookahead risk, family, timeframe, data needs,
  tail-risk flags.
- Public ranking / GitHub stars / marketplace popularity is candidate-universe
  input only — never alpha proof.
- Never touch broker / order / DB / scheduler / secret paths. No prod anything.

## Output format

Emit a JSON array of card objects (same fields as `schema.py`), e.g.:

```json
{
  "candidate_id": "freqtrade_<name>",
  "source_url": "https://github.com/...",
  "source_bucket": "freqtrade_github",
  "license": "GPL-3.0",
  "code_availability": "open",
  "strategy_family": "breakout",
  "spot_or_futures": "both",
  "long_short": "both",
  "timeframe": "15m",
  "holding_horizon": "intraday",
  "entry_exit_summary": "...",
  "data_requirements": ["ohlcv"],
  "tail_risk_flags": [],
  "lookahead_repaint_risk": "low",
  "implementation_complexity": "low",
  "novelty_vs_failed_families": "adjacent",
  "expected_cost_sensitivity": "medium",
  "source_verified": true,
  "score_status": "verified",
  "recommended_disposition_pre_validation": "keep"
}
```

Hand the array to the integrator. The integrator validates with `load_catalog`,
merges into `candidates.json`, runs the scorer, and freezes the shortlist.
