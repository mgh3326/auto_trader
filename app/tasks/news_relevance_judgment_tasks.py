"""ROB-506 — TaskIQ task for async news-relevance judgment.

NO recurring schedule (TaskIQ/cron/Prefect activation is operator-owned and
out of repo). Enqueued from the ``get_news`` KR persist path when
``NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED`` is on, or invoked manually for
smoke. Commit mode is refused while the flag is off; ``dry_run=True`` (the
default) is always allowed and performs no client call / no writes.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.news_relevance_judgment import run_news_relevance_judgment

logger = logging.getLogger(__name__)


@broker.task(task_name="news_relevance.judge_pending")
async def news_relevance_judge_pending(
    market: str = "kr",
    symbol: str | None = None,
    article_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = True,
) -> dict:
    if not dry_run and not settings.NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED:
        return {
            "status": "disabled",
            "reason": "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED is off",
            "market": market,
            "symbol": symbol,
        }
    return await run_news_relevance_judgment(
        market=market,
        symbol=symbol,
        article_ids=article_ids,
        limit=limit,
        dry_run=dry_run,
    )
