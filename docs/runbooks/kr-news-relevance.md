# KR News Investment Relevance Gate (ROB-169)

## What this does

`/invest/api/feed/news` applies a deterministic KR investment-relevance scorer
to every `market="kr"` row. The scorer is read-layer only: ingestion is
unchanged.

## Components

- Scorer: `app/services/kr_news_relevance_service.py::score_kr_news_article`
- Term lists: `app/services/news_entity_alias_data.py`
  - `KR_BROAD_MARKET_TERMS` — KOSPI/KOSDAQ/금리/환율/원자재/CPI/GDP/IPO 등
  - `KR_INVEST_KEYWORDS` — 반도체/배터리/조선/방산/원전/바이오/금융위 등
  - `KR_CRIME_TERMS` — 살해/피의자/사이코패스/마약/사기/보이스피싱 등
  - `KR_SOCIETY_TERMS` — 연예/아이돌/스포츠/사고/날씨/여고생 등
  - `KR_NOISE_TERMS` — 한파/미세먼지/맛집/운세/로또 등
- Wiring: `app/services/invest_view_model/feed_news_service.py::build_feed_news`
- Schema fields: `app/schemas/invest_feed_news.py::FeedNewsItem.{scope,tags,category,noiseReason}`

## How to extend term lists

1. Add the new term to the appropriate constant in `news_entity_alias_data.py`.
2. Add a fixture row in `tests/fixtures/kr_news_relevance/` covering it.
3. Run `uv run pytest tests/test_kr_news_relevance_service.py -v`.

## How to verify in production

Read-only smoke (no auth changes, no mutation):

```bash
# Replace COOKIE with the operator session cookie.
curl -s -b "session=$COOKIE" "https://prod.host/invest/api/feed/news?tab=kr&limit=50" | \
  jq '.items[] | {id, title, market, noiseReason, category, scope, hasIssue: (.issueId!=null), hasSymbols: (.relatedSymbols|length>0)}'
```

Expected:
- `tab=kr` response has zero rows with `noiseReason: "kr_crime"` or `"kr_society"`.
- KOSPI/IPO/금리/환율/반도체/정책 rows present even when `relatedSymbols` is empty.
- `noiseReason` set on demoted rows (visible on broader tabs like `top`/`latest`),
  with `issueId: null` for those rows.

## Rollback

This feature is a pure-function, additive read-layer change with no DB
migration. Revert the wiring commit to disable.
The schema additions in `app/schemas/invest_feed_news.py` and the
frontend type widening are safely additive and may be left in place
during a partial revert.

## Known limitations

- Heuristic term lists; tune them via fixtures, not threshold changes.
- KR scope-based symbol demotion (analogous to ROB-155 US scope) is intentionally
  out of scope here. `KR_BIG_CAP_GROUP_SYMBOLS` is reserved for that future ROB.
- tvscreener KR rows are scored the same way; if their richer metadata should
  bypass the gate, that should be its own ticket (suggested follow-up).
