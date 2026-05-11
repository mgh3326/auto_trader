from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.invest_feed_news import NewsMarket
from app.schemas.invest_stock_detail import (
    StockDetailDiscussionSignal,
    StockDetailDiscussionSignalMetric,
)

DOCS_PATH = "docs/invest/naver-discussion-signal-poc.md"


def _naver_symbol(market: NewsMarket, symbol: str) -> str:
    if market == "us":
        normalized = symbol.upper().replace("-", ".")
        if "." not in normalized:
            normalized = f"{normalized}.O"
        return normalized
    if market == "kr":
        return symbol.zfill(6) if symbol.isdigit() else symbol
    return symbol.upper()


async def build_naver_discussion_signal_poc(
    market: NewsMarket,
    symbol: str,
    *_args: object,
    **_kwargs: object,
) -> StockDetailDiscussionSignal | None:
    """Return a fixture-backed aggregate-only Naver discussion signal PoC.

    This is deliberately not a live Naver fetcher, scheduled collector, or UGC
    renderer. It captures the safe ROB-199 conclusion: discussion pages may be
    useful as activity/ranking/momentum signals after legal/rate-limit review,
    but `/invest` must not clone public community posts or store user text.
    """

    if market == "crypto":
        return None

    naver_code = _naver_symbol(market, symbol)
    if market == "us":
        # World-stock discussion pages were observed as page candidates only;
        # keep this conservative until endpoint/rate-limit contract is reviewed.
        return StockDetailDiscussionSignal(
            market=market,
            symbol=symbol,
            naverCode=naver_code,
            status="no_go_pending_review",
            liveFetchEnabled=False,
            freshness="fixture",
            observedAt=datetime(2026, 5, 11, 6, 0, tzinfo=UTC),
            windowLabel="ROB-199 one-off endpoint/page probe",
            activityRank=None,
            postCount=None,
            commentCount=None,
            reactionCount=None,
            momentum="unknown",
            metrics=[
                StockDetailDiscussionSignalMetric(
                    label="discussion_page_candidate", value="worldstock discussion page", unit=None
                ),
            ],
            mappedFields=[
                "discussion.activityAvailable",
                "discussion.aggregateOnlyRisk",
            ],
            noGoFields=[
                "public discussion post text",
                "post title/body",
                "author/nickname/user id",
                "comment text",
                "scheduled collector without approval",
            ],
            risk=(
                "World-stock discussion pages may expose public UGC and/or page-backed data; "
                "use internal ROB-181 memo/research panel for qualitative content."
            ),
            docsPath=DOCS_PATH,
        )

    # KR endpoint observed in ROB-197/199 notes: rankings can provide aggregate
    # activity/ranking hints. Values below are fixture evidence, not live data.
    return StockDetailDiscussionSignal(
        market=market,
        symbol=symbol,
        naverCode=naver_code,
        status="no_go_pending_review",
        liveFetchEnabled=False,
        freshness="fixture",
        observedAt=datetime(2026, 5, 11, 6, 0, tzinfo=UTC),
        windowLabel="ROB-199 one-off aggregate rankings probe",
        activityRank=5,
        postCount=128,
        commentCount=342,
        reactionCount=911,
        momentum="rising",
        metrics=[
            StockDetailDiscussionSignalMetric(label="activity_rank", value=5, unit="rank"),
            StockDetailDiscussionSignalMetric(label="post_count", value=128, unit="count"),
            StockDetailDiscussionSignalMetric(label="comment_count", value=342, unit="count"),
            StockDetailDiscussionSignalMetric(label="reaction_count", value=911, unit="count"),
        ],
        mappedFields=[
            "discussion.activityRank",
            "discussion.postCount",
            "discussion.commentCount",
            "discussion.reactionCount",
            "discussion.momentum",
        ],
        noGoFields=[
            "public discussion post text",
            "post title/body",
            "author/nickname/user id",
            "comment text",
            "raw reactions by user",
            "scheduled collector without approval",
        ],
        risk=(
            "Go only for aggregate signal metrics after ToS/rate-limit review; "
            "no community cloning, raw UGC rendering, storage, or request-time scraping."
        ),
        docsPath=DOCS_PATH,
    )


__all__ = ["build_naver_discussion_signal_poc"]
