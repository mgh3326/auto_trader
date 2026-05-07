# News Issue Lab v2 diagnostics runbook

## Purpose

`scripts/news_issue_lab.py` is an experimental operator lab for inspecting Toss-like market news issue candidates from existing `news_articles` rows. ROB-134 adds filtering and ranking diagnostics before any LLM rendering or production read-layer promotion.

## Required non-mutating smoke

Run the lab against the MacBook server DB and local BGE-M3 endpoint without storing results:

```bash
uv run python scripts/news_issue_lab.py \
  --market all --window-hours 24 --limit 240 --top 12 \
  --embedding-endpoint http://127.0.0.1:10631/v1/embeddings \
  --embedding-model BAAI/bge-m3 \
  --compare-v1 \
  --output /tmp/news_issue_lab_v2_24h_all_240.md
```

Structured JSON inspection variant:

```bash
uv run python scripts/news_issue_lab.py \
  --market all --window-hours 24 --limit 240 --top 12 \
  --embedding-endpoint http://127.0.0.1:10631/v1/embeddings \
  --embedding-model BAAI/bge-m3 \
  --compare-v1 --format json \
  --output /tmp/news_issue_lab_v2_24h_all_240.json
```

## Score formula summary

V2 ranks clusters by a scalar score and keeps diagnostics in every issue payload:

```text
score =
  0.40 * source_diversity_norm
+ 0.25 * article_count_norm
+ 0.20 * recency_norm
+ 0.15 * topic_relevance
- min(0.40, noise_penalty)
- min(0.45, regular_report_penalty)
- min(0.30, duplicate_source_penalty)
```

Default behavior penalizes noisy and regular-report clusters rather than hard-dropping them. Use `--drop-regular-reports` only for explicit tuning when clusters with at least 50% regular-report titles should be removed from the displayed V2 ranking.

Weights can be tuned with:

```bash
--weights diversity=0.40,volume=0.25,recency=0.20,relevance=0.15
```

Weights must include exactly `diversity`, `volume`, `recency`, and `relevance`, be non-negative, and sum to 1.0.

## Raw vs normalized source counts

`raw_source_count` counts original source/feed keys. `normalized_source_count` collapses equivalent source families before scoring. In ROB-134, all `browser_naver_research_*` variants normalize to `browser_naver_research` so many research-house crawler keys do not inflate independent-source diversity.

Every issue includes:

- `source_counts.raw`
- `source_counts.normalized`
- `raw_source_count`
- `normalized_source_count`
- `score_components`, `score_weighted`, and `score_penalties`

The top-level payload also includes raw and normalized source distribution maps for the full fetched article set.

## v1/v2 comparison

Add `--compare-v1` to preserve the legacy ranking for diagnostics. Markdown output includes:

- side-by-side top-N rank table
- downranked/excluded clusters and their dominant penalty
- promoted clusters and score components
- V2 top-N diagnostics table

JSON output adds the same data under top-level `v1_vs_v2`.

## Safety boundaries

- Lab-only scope; do not promote this ranking into production services without a later issue.
- Do not add LLM rendering in ROB-134.
- Do not run broker/order/watch/scheduler/trading mutations.
- Do not perform destructive DB changes.
- Do not redistribute or store full article bodies; use titles, summaries, sources, URLs, and metadata only.
- Do not print credentials, tokens, or connection strings.
- Do not run `--store` until markdown/json non-store smoke output is visually acceptable.
