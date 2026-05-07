# News Issue Clustering — Runbook (ROB-130)

Read-only market issue clustering API and ticker-news fallback.

## Surface

- HTTP: `GET /trading/api/news-issues?market=all|kr|us|crypto&window_hours=1..168&limit=1..100`
- MCP tool: `get_market_issues(market, window_hours, limit)` — read-only
- Schema: `app.schemas.news_issues.MarketIssuesResponse`
- Service entry point: `app.services.news_issue_clustering_service.build_market_issues`
- Fallback entry point: `app.services.llm_news_service.get_news_articles_with_fallback`
- Entity matcher: `app.services.news_entity_matcher.match_symbols_for_article`

## Behavior

1. Loads recent `news_articles` rows (window default 24h, max 500 rows).
2. Tags each row with the deterministic alias matcher (KR/US/crypto).
3. Clusters by primary entity; remaining rows clustered by 3-gram title shingles
   with Jaccard >= 0.34.
4. Ranks clusters by `0.5*recency + 0.3*source_diversity + 0.2*mention_score`.
5. Returns 16-char SHA-1 issue IDs. Entity-keyed clusters (cluster_key
   `sym:<market>:<symbol>`) have IDs derived only from `(market, cluster_key)` —
   stable across new articles joining the cluster. Shingle clusters (`shg:...`)
   include the sorted article-ID list and are inherently ephemeral.

## ROB-129 metadata consumption

Once the news-ingestor PR ships per-article `candidate_symbols` /
`candidate_sectors` JSONB metadata, replace step (2):

1. If `article.candidate_symbols` is present, prefer those over alias matching.
2. Use `match_symbols_for_article` only as a fallback when the candidate list
   is empty.

The contract additions (TODO ROB-129):

- `news_articles.candidate_symbols: JSONB | None`
- `news_articles.candidate_sectors: JSONB | None`

These are nullable, additive, and backward compatible.

## Operational checks

```bash
curl -sS "$BASE/trading/api/news-issues?market=us&window_hours=6&limit=5" \
  -H "Cookie: $AUTH" | jq '.items[0]'
```

Expected fields per item: `id`, `rank`, `issue_title`, `subtitle`, `direction`,
`source_count`, `article_count`, `signals.{recency_score,source_diversity_score,mention_score}`,
`related_symbols[]`, `articles[].matched_terms`.

## Ticker fallback behaviour

`get_news_articles_with_fallback(symbol, market, hours, limit)` is the lookup
used by research-session news stage and any future ticker-news consumers:

1. Exact `stock_symbol` match — articles tagged with the symbol in the DB
   (reason `exact_symbol`).
2. (Future ROB-129) candidate metadata rows — currently a no-op.
3. Alias title/summary/keywords match — over a wider market window
   (`limit * 5` rows, minimum 50). Articles whose title/summary/keywords
   match the symbol's alias dictionary entries get reason `alias_match`.

Examples:

- `symbol="AMZN", market="us"` — picks up articles tagged `AMZN`, then any
  recent US articles mentioning "Amazon" / "AMZN" / "아마존".
- `symbol="005930", market="kr"` — picks up `005930`-tagged rows, then any
  KR articles mentioning "삼성전자" / "삼전" / "Samsung Electronics".

## Performance / safety boundaries

- No LLM calls.
- No broker/order/intent imports.
- No DB writes; pure read query against `news_articles`.
- `max_rows=500` caps SQL fan-out; tune via call-site if needed.

## Smoke validation after deploy (Hermes)

```bash
uv run pytest tests/test_news_entity_matcher.py \
              tests/test_news_issue_clustering.py \
              tests/test_news_stage_fallback.py \
              tests/test_router_news_issues.py -v
```
