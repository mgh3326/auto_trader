"""Scheduleless resident task for the quotes:toss shadow consumer (#1120).

No ``schedule`` label is registered and
``QUOTES_TOSS_CONSUMER_ENABLED`` defaults false — the task only exists so
an operator can kick the consumer manually.  Records only: orders 0,
policy 0, session kicks 0, broker calls 0.
"""

from __future__ import annotations

from app.core.taskiq_broker import broker as taskiq_broker
from app.jobs.quotes_consumer import run_quotes_toss_consumer


@taskiq_broker.task(task_name="quotes_consumer.consume_quotes_toss")
async def consume_quotes_toss() -> dict:
    """Resident records-only consumer; gated off by default."""
    return await run_quotes_toss_consumer()
