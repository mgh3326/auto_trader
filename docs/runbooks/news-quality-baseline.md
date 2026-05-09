# ROB-155 News Quality Baseline Runbook

Read-only diagnostic tool that computes before/after quality metrics for
`/invest/api/feed/news` without mutating any DB rows or touching broker/order paths.

## Purpose

Establishes the baseline for:
- US article scope noise: broad-market vs symbol-specific classification rate, big-tech false-positive rate.
- Crypto feed relevance: category distribution, AI/semiconductor noise rejection %, supported-universe coverage.

## Safety guarantees

- **Read-only**: SELECT queries only; no INSERT/UPDATE/DELETE.
- **No LLM**: all classification is deterministic (alias dicts + term lists).
- **No secrets printed**: env/DSN values are never echoed to stdout.
- **No broker/order paths**: touches only `news_articles` and `news_article_related_symbols`.

## Usage

```bash
# Minimal run (US + crypto, last 7 days, 500 articles each)
uv run python scripts/news_quality_baseline.py --markets us,crypto --window-hours 168 --limit 500

# Custom output dir
uv run python scripts/news_quality_baseline.py \
  --markets us,crypto \
  --window-hours 168 \
  --limit 500 \
  --output-dir /tmp/rob155_baseline_before
```

Output files are written under `/tmp/rob155_news_quality_<timestamp>/` (or `--output-dir`):

| File | Contents |
|---|---|
| `baseline_report.json` | Full metrics JSON (see schema below) |

## Output schema

```json
{
  "as_of": "ISO-8601",
  "window_hours": 168,
  "limit": 500,
  "markets": ["us", "crypto"],
  "safety": { "read_only": true, "llm_disabled": true, "db_mutations": false, "broker_order_watch_paths": false },
  "us": {
    "sample_count": 0,
    "scope_distribution": { "symbol_specific": 0, "market_wide": 0, "mixed": 0 },
    "big_tech_fp_rate_before": 0.0,
    "broad_market_flag_rate": 0.0,
    "top_sources": {},
    "fp_examples": []
  },
  "crypto": {
    "sample_count": 0,
    "include_count": 0,
    "category_distribution": {},
    "noise_reason_distribution": {},
    "ai_semi_noise_rejection_pct": 0.0,
    "supported_universe_coverage_pct": 0.0,
    "fp_examples": []
  }
}
```

## Interpreting results

- `broad_market_flag_rate` > 0.30 suggests many US articles are macro/index-wide — expected for broad news feeds.
- `big_tech_fp_rate_before` is the fraction of articles where big-tech symbols are demoted (not suppressed from DB).
- `ai_semi_noise_rejection_pct` is the fraction of crypto articles classified as `broad_tech_without_crypto_signal`.
- `supported_universe_coverage_pct` is the fraction of crypto articles mentioning at least one of BTC/ETH/SOL/XRP/DOGE.

## After deploy

Re-run with `--output-dir /tmp/rob155_baseline_after` and compare `baseline_report.json` files
to confirm noise metrics improved. Attach artifact paths to the ROB-155 Linear ticket.

## Related

- `scripts/news_issue_lab_quality_eval.py --mode tag-precision` — labeled precision evaluation.
- `scripts/news_feed_readonly_smoke.py` — live endpoint smoke validation.
- `tests/test_news_quality_baseline.py` — unit tests (no DB required).
