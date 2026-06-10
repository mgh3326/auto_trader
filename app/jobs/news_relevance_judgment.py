"""ROB-506 — orchestration for the async news-relevance judgment worker.

Pure service-layer flow (no ``@broker.task`` here — that lives in
``app/tasks/news_relevance_judgment_tasks.py``):

    list_pending → judgment client → apply via symbol_news_store

Safety invariants:
* never writes ``status`` directly — ``apply_judgment`` derives it
  server-side (``relationship=unrelated`` or ``relevance=low`` → excluded);
* client failure / dispatch / invalid payload leaves rows ``pending``;
* judgments outside the requested batch are skipped (counted), so a
  confused external endpoint cannot touch arbitrary rows;
* no broker/order/watch surface, no secrets in the returned summary.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services import symbol_news_store
from app.services.news_relevance_judgment_client import (
    NewsRelevanceJudgmentClient,
)

logger = logging.getLogger(__name__)

_MAX_BATCH = 200  # ingest route hard cap (NewsRelevanceIngestRequest)


async def run_news_relevance_judgment(
    *,
    market: str = "kr",
    symbol: str | None = None,
    article_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    client: Any | None = None,
    session_factory: Any | None = None,
) -> dict[str, Any]:
    session_factory = session_factory or AsyncSessionLocal
    batch_limit = limit or settings.NEWS_RELEVANCE_JUDGMENT_BATCH_LIMIT
    batch_limit = max(1, min(int(batch_limit), _MAX_BATCH))

    summary: dict[str, Any] = {
        "status": "no_pending",
        "market": market,
        "symbol": symbol,
        "dry_run": dry_run,
        "client_mode": "webhook",
        "fetched_pending": 0,
        "judged": 0,
        "applied_confirmed": 0,
        "applied_excluded": 0,
        "skipped_unrequested": 0,
        "invalid_judgments": 0,
        "link_not_found": 0,
        "http_status": None,
        "reason": None,
    }

    async with session_factory() as db:
        pending = await symbol_news_store.list_pending(
            db, market, batch_limit, symbol=symbol
        )
        if article_ids is not None:
            wanted = set(article_ids)
            pending = [row for row in pending if row["article_id"] in wanted]
        summary["fetched_pending"] = len(pending)
        if not pending:
            return summary

        if dry_run:
            summary["status"] = "dry_run"
            return summary

        owns_client = client is None
        if owns_client:
            client = NewsRelevanceJudgmentClient()
        try:
            result = await client.request_judgments(
                market=market, symbol=symbol, pending=pending
            )
        finally:
            if owns_client:
                await client.close()

        summary["status"] = result.status
        summary["http_status"] = result.http_status
        summary["reason"] = result.reason
        summary["invalid_judgments"] = result.invalid_count
        if result.status != "judged":
            # failed / dispatched / skipped — rows stay pending by design.
            return summary

        requested = {(row["article_id"], market, row["symbol"]) for row in pending}
        for judgment in result.judgments:
            key = (judgment.article_id, judgment.market, judgment.symbol)
            if key not in requested:
                summary["skipped_unrequested"] += 1
                continue
            status = await symbol_news_store.apply_judgment(
                db,
                article_id=judgment.article_id,
                market=judgment.market,
                symbol=judgment.symbol,
                relationship=judgment.relationship,
                relevance=judgment.relevance,
                price_relevance=judgment.price_relevance,
                score=judgment.score,
                reason=judgment.reason,
                judged_by=judgment.judged_by,
            )
            if status is None:
                summary["link_not_found"] += 1
            else:
                summary["judged"] += 1
                if status == "excluded":
                    summary["applied_excluded"] += 1
                else:
                    summary["applied_confirmed"] += 1
        await db.commit()

    logger.info(
        "news_relevance judgment run: market=%s symbol=%s status=%s "
        "fetched=%s judged=%s confirmed=%s excluded=%s skipped=%s invalid=%s",
        market,
        symbol,
        summary["status"],
        summary["fetched_pending"],
        summary["judged"],
        summary["applied_confirmed"],
        summary["applied_excluded"],
        summary["skipped_unrequested"],
        summary["invalid_judgments"],
    )
    return summary
