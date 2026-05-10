# ROB-161 tvscreener ingest + /invest feed smoke runbook

> Read-only smoke. No broker / order / scheduler / DB-mutation actions. Never paste secret values.

## Local TestClient smoke (pre-merge)

```bash
cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-161-tvscreener-ingest-invest-feed
uv run pytest \
  tests/test_news_ingestor_bulk_tvscreener.py \
  tests/test_invest_feed_news_tvscreener.py \
  -v -m "not live"
```

Expected: all green. If any failure references `_parse_tradingview_symbol`, re-check the fallback parser change in `app/services/news_payload_normalizer.py`.

## End-to-end push smoke against a local auto_trader API

Pre-req: a non-production Postgres pointed at by `DATABASE_URL` (use `.env.local`, never `.env.production`).

1. Start the auto_trader API:

```bash
cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-161-tvscreener-ingest-invest-feed
uv run uvicorn app.main:app --port 8000
```

2. From `/Users/mgh3326/work/news-ingestor`, run the dry-run push to inspect the payload:

```bash
cd /Users/mgh3326/work/news-ingestor
uv run python -m news_ingestor push-pending --market us --feed-set us-tvscreener --dry-run --limit 5
```

3. Execute against the local API (export the env var **names** only — values come from your secret store):

```bash
export AUTO_TRADER_BULK_INGEST_URL=http://127.0.0.1:8000/api/v1/news/ingest/bulk
export AUTO_TRADER_INGEST_TOKEN="$(pass auto_trader/local-ingest-token)"  # adapt to your pwstore
uv run python -m news_ingestor push-pending --market us --feed-set us-tvscreener --execute --limit 5
```

4. Verify auto_trader stored the rows:

```bash
curl -s "http://127.0.0.1:8000/invest/api/feed/news?tab=top&limit=10&includeQuotes=false" | jq '.items[] | {feedSource, market, title, related: [.relatedSymbols[].symbol]}'
```

Pass criteria: at least one `feedSource` starts with `http_tvscreener_news_` and at least one of those rows has a non-empty `related` list.

5. Tab smoke:

```bash
curl -s "http://127.0.0.1:8000/invest/api/feed/news?tab=us&limit=10" | jq '[.items[].feedSource] | unique'
curl -s "http://127.0.0.1:8000/invest/api/feed/news?tab=crypto&limit=10" | jq '[.items[].feedSource] | unique'
curl -s "http://127.0.0.1:8000/invest/api/feed/news?tab=top&limit=5&includeQuotes=true" | jq '.items[0].relatedSymbols[0].quote'
```

6. /invest/feed/news (server-rendered) sanity:

```bash
curl -s -I http://127.0.0.1:8000/invest/feed/news
```

Expected `HTTP/1.1 200 OK`.

## Post-deploy production smoke (read-only)

Run **only after** the merge SHA is on the deployed environment. Substitute `$DEPLOYED_HOST`.

```bash
curl -s "https://$DEPLOYED_HOST/invest/api/feed/news?tab=top&limit=20" | jq '[.items[] | select(.feedSource | startswith("http_tvscreener_news_"))] | length'
curl -s "https://$DEPLOYED_HOST/invest/api/feed/news?tab=us&limit=20" | jq '[.items[] | select(.feedSource | startswith("http_tvscreener_news_"))] | length'
curl -s "https://$DEPLOYED_HOST/invest/api/feed/news?tab=crypto&limit=20" | jq '[.items[] | select(.feedSource | startswith("http_tvscreener_news_"))] | length'
```

Expected: integer >= 1 on at least one of `top`/`us`/`crypto` after news-ingestor's tvscreener feed-set has run at least once. Zero on **all three** is a smoke fail — file a follow-up; do not remediate by re-running the push from the dev box.

## Notes / out of scope

- `tvscreener_symbol_news` source is **not** produced by news-ingestor today (see `/Users/mgh3326/work/news-ingestor/src/news_ingestor/sources/tvscreener_news.py:11-13`). The Linear text mentions it for forward-compatibility; ROB-163 will own the per-symbol news source.
- `get_article()` body enrichment is **out of scope** here (ROB-162).
- No scheduler/Prefect cadence change in this issue.
- TradingView prefix parser lives in `app/services/news_payload_normalizer.py` (post-ROB-155 refactor).
