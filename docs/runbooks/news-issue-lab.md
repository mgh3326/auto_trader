# News Issue Lab v2 diagnostics runbook

## Purpose

`scripts/news_issue_lab.py` is an experimental operator lab for inspecting Toss-like market news issue candidates from existing `news_articles` rows. ROB-134 adds filtering and ranking diagnostics before any LLM rendering or production read-layer promotion.

## Required non-mutating smoke

Run the lab against the MacBook server DB and local BGE-M3 endpoint without storing results. The CLI default `--batch-size 16` is intentionally conservative for the local embedding server; keep it unless the server has been separately load-tested.

```bash
uv run python scripts/news_issue_lab.py \
  --market all --window-hours 24 --limit 240 --top 12 \
  --embedding-endpoint http://127.0.0.1:10631/v1/embeddings \
  --embedding-model BAAI/bge-m3 \
  --compare-v1 --merge-clusters \
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

## 클러스터 병합 (ROB-135)

The merge pass runs after the per-article clustering and before the V2 ranking. It fuses near-duplicate clusters that share a topic, symbol, or strong lexical/embedding overlap, so the top-N stops being dominated by single-article topic-tied entries (the ROB-134 baseline showed many ties at 0.5053).

### Default behavior

- Merge pass is **on** by default. To reproduce ROB-134 exactly, pass `--no-merge-clusters`.
- Defaults: `--merge-rep-threshold 0.86`, `--merge-token-jaccard 0.30`, `--merge-rep-articles 3`; topic-label agreement also has a calibrated low-rep safety valve at `topic_rep_threshold=0.43` for the ROB-134 duplicate-title baseline.
- Merge requires `rep_sim >= rep_threshold` AND at least one of `topic_agree`, `symbol_agree`, `token_jaccard >= token_jaccard_threshold`. Strong embedding similarity (≥ 0.93) plus minimal lexical overlap also merges. When two clusters have the same rule-based topic label, `rep_sim >= 0.43` is sufficient after the normal anti-merge guardrails, because the local BGE-M3 representative vectors score Korean/English same-topic headlines lower than the original 0.86 plan assumption.
- Anti-merge guardrails block: zero-token-overlap noise, oversized fused clusters (>25 articles), and source-diverse clusters with disagreeing topic labels.

### Smoke commands

```bash
# merged output (default)
uv run python scripts/news_issue_lab.py \
  --market all --window-hours 24 --limit 240 --top 12 \
  --embedding-endpoint http://127.0.0.1:10631/v1/embeddings \
  --embedding-model BAAI/bge-m3 \
  --compare-v1 --merge-clusters \
  --output /tmp/rob135_news_issue_lab_v2_24h_all_240_merged.md

# JSON variant
uv run python scripts/news_issue_lab.py \
  --market all --window-hours 24 --limit 240 --top 12 \
  --embedding-endpoint http://127.0.0.1:10631/v1/embeddings \
  --embedding-model BAAI/bge-m3 \
  --compare-v1 --merge-clusters --format json \
  --output /tmp/rob135_news_issue_lab_v2_24h_all_240_merged.json

# baseline (ROB-134 behavior)
uv run python scripts/news_issue_lab.py \
  --market all --window-hours 24 --limit 240 --top 12 \
  --embedding-endpoint http://127.0.0.1:10631/v1/embeddings \
  --embedding-model BAAI/bge-m3 \
  --compare-v1 --no-merge-clusters --format json \
  --output /tmp/rob135_news_issue_lab_v2_24h_all_240_baseline.json
```

### Diagnostics

Every issue payload gains:

- `merge_member_count`
- `merged_cluster_ids`

The top-level payload gains:

- `merge_diagnostics.enabled`
- `merge_diagnostics.merge_before_count`
- `merge_diagnostics.merge_after_count`
- `merge_diagnostics.rejected_near_misses`
- `merge_diagnostics.thresholds`
- `merge_diagnostics.decisions[]` (each with `absorber_cid`, `absorbed_cid`, `rep_sim`, `token_jaccard`, `source_overlap`, `topic_agree`, `symbol_agree`, `decision`, `reason`, `absorber_title`, `absorbed_title`)

Markdown output gains a `## 클러스터 병합 진단 (ROB-135)` section with merged-cluster and rejected-near-miss tables.

### Known limits (out of scope for ROB-135)

- Topic-label rules can still cosmetically mislabel Yahoo credit-card titles ("Gold" → 금·원자재). The merge pass does not depend on those labels being correct, but operators may see odd-looking topic agreement signals on noise rows. Existing `noise_penalty` continues to downrank them.
- Yahoo plural/singular term mismatches (e.g., `credit cards` vs `credit card`) are unchanged in this scope.


## Korean LLM rendering (ROB-136)

ROB-136 keeps Korean issue-card rendering inside this lab CLI only. It does not add production API, UI, MCP, scheduler, broker, order, watch, or request-time LLM behavior.

### Default no-LLM mode

LLM rendering is disabled by default. The default path is deterministic and uses the rule-based card plus fallback metadata:

```bash
uv run python scripts/news_issue_lab.py   --market all --window-hours 24 --limit 240 --top 12   --embedding-endpoint http://127.0.0.1:10631/v1/embeddings   --embedding-model BAAI/bge-m3   --compare-v1 --merge-clusters   --format json   --output /tmp/rob136_news_issue_lab_no_llm.json
```

`--no-llm` is an explicit alias for the same deterministic mode.

### Optional LLM smoke

Use `--llm-render` only for an operator-confirmed local/OpenAI-compatible endpoint and start with a small `--llm-max-render`. Run without `--store` first and inspect the markdown or JSON output before persisting any lab result.

```bash
uv run python scripts/news_issue_lab.py   --market all --window-hours 24 --limit 240 --top 12   --embedding-endpoint http://127.0.0.1:10631/v1/embeddings   --embedding-model BAAI/bge-m3   --compare-v1 --merge-clusters   --llm-render   --llm-endpoint http://127.0.0.1:8000   --llm-model <local-openai-compatible-model>   --llm-max-render 5   --format json   --output /tmp/rob136_news_issue_lab_llm_top5.json
```

### Schema and fallback

The renderer validates that each LLM response is a JSON object with:

- `title_ko`
- `subtitle_ko`
- `direction` (`up`, `down`, or `neutral`)
- `summary_ko`
- `impact_points`
- `related_symbols`
- `confidence`

Malformed JSON, missing fields, wrong types, empty title/subtitle/summary, invalid direction/confidence, length/cardinality violations, unknown related symbols, low Korean-script ratio, or investment-advice phrases such as 매수/매도/추천/목표가 cause deterministic fallback. Fallback cards keep the rule-based title/subtitle/direction, add a schema-complete Korean summary and impact points, set `confidence=0.0`, and record `render_status=fallback` plus `render_rejection_reason`.

### Diagnostics and storage

Run-level diagnostics live at `payload.run.llm_render` and include enabled/provider/model/prompt version, requested count, ok/fallback/skipped counts, rejection counts, and total latency. Each issue payload records `render_status`, `render_model`, `render_prompt_version`, `render_input_hash`, `render_rejection_reason`, `render_latency_ms`, `summary_ko`, `impact_points`, and `confidence`. Existing lab result columns continue to receive the final top-level rendered-or-fallback `title_ko`, `subtitle_ko`, and `direction`; no DDL is required.

### Safety reminders

- Do not send or store full article bodies; prompts are built from cluster metadata, titles, source/feed_source, timestamps, short summaries/excerpts, topics, related symbols, and ranking/merge diagnostics only.
- Do not print credentials, tokens, cookies, DSNs, or Authorization values.
- Do not run `--store` until no-store markdown/JSON output has been reviewed.
- Do not wire this renderer into production API/UI/MCP request paths without a separate issue and review.
