from __future__ import annotations

from urllib.parse import quote

from app.schemas.invest_feed_news import NewsMarket
from app.schemas.invest_stock_detail import (
    StockDetailNaverEndpointProbe,
    StockDetailNaverEnrichment,
)

DOCS_PATH = "docs/invest/naver-stock-detail-raw-data-poc.md"


def _naver_symbol(market: NewsMarket, symbol: str) -> str:
    if market == "us":
        # Naver world-stock pages use Reuters suffixes such as MSFT.O.  Keep a
        # caller-provided suffix when present and default bare US symbols to .O
        # for this bounded PoC rather than attempting exchange inference.
        normalized = symbol.upper().replace("-", ".")
        if "." not in normalized:
            normalized = f"{normalized}.O"
        return normalized
    if market == "kr":
        return symbol.zfill(6) if symbol.isdigit() else symbol
    return symbol.upper()


async def build_naver_stock_detail_poc(
    market: NewsMarket,
    symbol: str,
    *_args: object,
    **_kwargs: object,
) -> StockDetailNaverEnrichment | None:
    """Return a fixture-backed Naver Stock data-map PoC for stock detail.

    This intentionally does not fetch Naver at request time, write to the DB, or
    expose community post text. It gives `/invest` a route-compatible read-model
    describing which Naver raw/intermediate endpoints are worth a follow-up
    approved collector, and which fields can enrich the existing stock-detail
    contract.
    """

    if market == "crypto":
        return None

    naver_code = _naver_symbol(market, symbol)
    quoted = quote(naver_code, safe=".")

    if market == "us":
        page_url = f"https://stock.naver.com/worldstock/stock/{quoted}/price"
        endpoints = [
            StockDetailNaverEndpointProbe(
                surface="worldstock_price_polling",
                url=(
                    "https://stock.naver.com/api/polling/worldstock/stock"
                    f"?reutersCodes={quoted}"
                ),
                status="verified_200",
                payloadFields=[
                    "datas[].stockName",
                    "datas[].symbolCode",
                    "datas[].stockExchangeType",
                    "datas[].closePrice",
                    "datas[].compareToPreviousClosePrice",
                    "datas[].fluctuationsRatio",
                    "pollingInterval",
                ],
                mappedFields=[
                    "quote.price",
                    "quote.changeAmount",
                    "quote.changeRate",
                    "exchange",
                    "displayName",
                    "sourceFreshness.pollingInterval",
                ],
                risk="Public JSON endpoint verified by one-off ROB-197 probe; do not poll from product without rate-limit/ToS approval.",
            ),
            StockDetailNaverEndpointProbe(
                surface="worldstock_finance_overview",
                url=f"https://stock.naver.com/worldstock/stock/{quoted}/finance/overview",
                status="page_candidate",
                payloadFields=[
                    "financial summary cards",
                    "annual/quarter rows",
                    "valuation/ratio labels",
                ],
                mappedFields=["valuation", "profile", "sourceFreshness"],
                risk="Page-backed Next.js data needs endpoint contract discovery; keep fixture-only until approved.",
            ),
            StockDetailNaverEndpointProbe(
                surface="worldstock_news",
                url=f"https://stock.naver.com/worldstock/stock/{quoted}/worldnews",
                status="page_candidate",
                payloadFields=["news title", "publisher", "publishedAt", "url"],
                mappedFields=["news.items", "meta.sourceFreshness"],
                risk="Use only citation metadata; current auto_trader/news-ingestor remains canonical news source.",
            ),
            StockDetailNaverEndpointProbe(
                surface="worldstock_investmentinfo",
                url=f"https://stock.naver.com/worldstock/stock/{quoted}/investmentinfo",
                status="needs_auth_or_contract_check",
                payloadFields=["analyst/consensus-like widgets if available"],
                mappedFields=["valuation"],
                risk="May be personalized or contract-gated; do not depend on it in production without review.",
            ),
        ]
    else:
        page_url = f"https://stock.naver.com/domestic/stock/{quoted}/price"
        endpoints = [
            StockDetailNaverEndpointProbe(
                surface="domestic_price_page",
                url=page_url,
                status="page_candidate",
                payloadFields=[
                    "current price",
                    "change",
                    "market",
                    "delay/freshness labels",
                ],
                mappedFields=["quote", "exchange", "sourceFreshness"],
                risk="Prefer KIS/DB quote as source of truth; Naver can fill source freshness/label gaps only after approval.",
            ),
            StockDetailNaverEndpointProbe(
                surface="domestic_news_aggregate_home",
                url="https://stock.naver.com/api/domestic/news/aggregate/home",
                status="verified_200",
                payloadFields=[
                    "flashNews[].title",
                    "flashNews[].url",
                    "flashNews[].leadtext",
                    "press",
                ],
                mappedFields=["news.items", "marketNews"],
                risk="Market-wide endpoint is not symbol-scoped; use related-symbol matcher before attaching to detail pages.",
            ),
            StockDetailNaverEndpointProbe(
                surface="domestic_finance_overview",
                url=f"https://stock.naver.com/domestic/stock/{quoted}/finance/overview",
                status="page_candidate",
                payloadFields=[
                    "annual/quarter finance rows",
                    "PER/PBR/ROE",
                    "dividend labels",
                ],
                mappedFields=["valuation", "profile", "sourceFreshness"],
                risk="Overlap with existing Naver Finance/KIS valuation paths; avoid duplicate collectors.",
            ),
            StockDetailNaverEndpointProbe(
                surface="discussion_signal",
                url="https://stock.naver.com/api/community/discussion/rankings?size=5",
                status="verified_200_signal_only",
                payloadFields=[
                    "rankTime",
                    "totalCount",
                    "itemCodes",
                    "comment-count/reaction metadata",
                ],
                mappedFields=["discussionSignal.rank", "discussionSignal.volume"],
                risk="Do not store/render public discussion text; aggregate signal metrics only.",
            ),
        ]

    return StockDetailNaverEnrichment(
        source="naver_stock_detail_poc",
        market=market,
        symbol=symbol,
        naverCode=naver_code,
        pageUrl=page_url,
        status="fixture_backed_poc",
        liveFetchEnabled=False,
        endpoints=endpoints,
        usefulFields=[
            "source freshness / polling interval",
            "price-change labels for display parity",
            "valuation/profile rows where current auto_trader data is missing",
            "related news citation metadata",
            "discussion volume/ranking as aggregate signal only",
        ],
        noGoFields=[
            "raw public discussion post text",
            "auth-gated personalized investment info",
            "scheduled polling/backfill without explicit approval",
            "Toss private API dependency",
        ],
        docsPath=DOCS_PATH,
    )


__all__ = ["build_naver_stock_detail_poc"]
