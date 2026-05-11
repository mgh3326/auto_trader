# ROB-197 Naver stock-detail raw data PoC

## Goal

Evaluate Naver Securities stock-detail raw/intermediate data as a bounded, read-only enrichment source for `/invest/stocks/:market/:symbol`.

This PoC is intentionally fixture-backed inside the `/invest` stock-detail response. It does not fetch Naver from the product request path, write production DB rows, schedule collectors, backfill data, use Toss private APIs, or expose broker/order/watch mutations.

## One-off endpoint evidence

Read-only probes from the implementation worktree on 2026-05-11:

| Surface | Probe URL | Result | Notes |
| --- | --- | --- | --- |
| US world-stock price polling | `https://stock.naver.com/api/polling/worldstock/stock?reutersCodes=MSFT.O` | 200 JSON | Includes `pollingInterval`, `datas[].stockName`, exchange type, delayed quote/price-change fields. Useful for source freshness and price display parity, but should not replace KIS/DB source of truth without ToS/rate-limit approval. |
| Security-service overview candidate | `https://stock.naver.com/api/securityService/stock/overview?itemCode=MSFT.O` | 400 | Confirms some chunk-discovered API paths need exact parameters/page proxy discovery before use. Treat as candidate only. |
| Domestic news aggregate | `https://stock.naver.com/api/domestic/news/aggregate/home` | 200 JSON | Market-wide KR news. Can inform news-source mapping but is not symbol-scoped by itself. Use existing related-symbol matcher before attaching rows to a detail page. |
| Discussion ranking signal | `https://stock.naver.com/api/community/discussion/rankings?size=5` | 200 JSON | Contains rank time, item codes, and post/reaction structures. Use only aggregate signal metrics; do not store/render post text or clone community content. |

## Candidate map by `/invest` stock-detail block

| `/invest` block | Naver candidate | Useful fields | Current auto_trader comparison | PoC decision |
| --- | --- | --- | --- | --- |
| Header/quote | `/api/polling/worldstock/stock?reutersCodes=...` for US; domestic price page/API for KR | display name, exchange, price, change, freshness/polling interval | Current quote/candle paths already come from KIS/Yahoo/DB/market services | Add fixture-backed `naverEnrichment` map only; no request-time polling. |
| Profile/valuation | `/finance/overview` and security-service finance endpoints from Next.js chunks | PER/PBR/ROE/dividend/annual-quarter rows | Existing Naver Finance/KIS/valuation services already cover parts of KR; US uses current market-data/fundamentals paths | Candidate for follow-up adapter once exact endpoint contract is verified. |
| News | `/worldnews`, `/api/domestic/news/aggregate/home`, Naver research endpoints | title, publisher, URL, published time, summary snippet | Existing `news-ingestor`/`build_feed_news` remains canonical and already has related-symbol matching | Use citation metadata only; no body scraping in this PoC. |
| Investment info / consensus-like widgets | `/investmentinfo`, `/api/stockDomestic/invest-info/*` candidates | analyst/consensus-like labels if public | auto_trader latest analysis/research session remains canonical | Needs auth/product-contract review; do not rely on it yet. |
| Discussion | `/api/community/discussion/rankings`, comment-count/reaction candidates | rank, mention/comment/reaction counts, item-code popularity | No existing community clone; ROB-199 should be signal-only | Aggregate metrics only; no post text storage/rendering. |

## Code-level PoC contract

`StockDetailResponse.naverEnrichment` is a nullable read-model field with:

- `source = "naver_stock_detail_poc"`
- `liveFetchEnabled = false`
- `endpoints[]` with candidate URL, verification status, payload fields, mapped `/invest` fields, and risk note
- `usefulFields[]` and `noGoFields[]` for UI/reviewer visibility
- `docsPath` pointing to this file

The default service provider is `build_naver_stock_detail_poc()` in `app/services/invest_view_model/naver_stock_detail_poc.py`. It is deterministic and fixture-backed. The frontend displays a compact read-only PoC card in the stock-detail right rail.

## Risks and guardrails

- Naver APIs may be rate-limited, contract-dependent, or page-proxy-specific. Keep any live fetch behind a separately approved bounded collector.
- Do not use Naver discussion content as community content. If pursued, reduce it to aggregate signal metrics only.
- Do not treat Toss private endpoints as production sources for this sprint.
- Do not add scheduler/backfill/production DB writes from this PoC.
- Keep KIS/Upbit/auto_trader DB/news-ingestor as source of truth; Naver may enrich display/freshness gaps after approval.

## Follow-up candidates

1. Discover exact page-backed finance/overview endpoint parameters for `MSFT.O`, `005930`, and `035420` with saved dry-run JSON samples.
2. Add a disabled-by-default Naver stock-detail adapter test fixture for finance overview normalization.
3. Extend existing coverage-status concepts with a Naver stock-detail source row if a live/fixture adapter is approved.
4. Implement ROB-199 as discussion aggregate-signal only, excluding post text.
