"""Prefect wrapper for ROB-1297 market-close digest.

The flow is importable only; no deployment / cron is registered in this repo.
Intended operator cadence (upstream Prefect, not this file):

* US  05:05 KST trading days
* KR  15:45 KST trading days
* crypto 09:05 KST daily

Holiday skip lives in the digest service (shared XKRX/XNYS calendar). Send is
opt-in (``send=False`` by default) so a manual flow run cannot page Telegram
by accident.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from prefect import flow, task

from app.core.db import AsyncSessionLocal
from app.monitoring.trade_notifier import get_trade_notifier
from app.monitoring.trade_notifier.runtime import (
    configure_trade_notifier_from_settings,
)
from app.services.market_close_digest.service import run_market_close_digest
from app.services.market_close_digest.types import Market


def _to_dict(result: Any) -> dict[str, Any]:
    snapshot = result.snapshot
    payload: dict[str, Any] = {
        "market": result.market,
        "sessionDate": result.session_date.isoformat(),
        "status": result.status,
        "sent": result.sent,
        "mutationCount": result.mutation_count,
        "message": result.message,
    }
    if snapshot is not None:
        payload["fillCount"] = snapshot.fill_count
        payload["buyCount"] = snapshot.buy_count
        payload["sellCount"] = snapshot.sell_count
        payload["oversellBlocked"] = len(snapshot.oversell_blocked)
        payload["autoApproveCount"] = snapshot.auto_approve_count
        payload["cardCount"] = snapshot.card_count
        payload["flags"] = list(snapshot.flags)
    return payload


async def run_market_close_digest_refresh(
    *,
    market: Market,
    session_date: date | None = None,
    send: bool = False,
) -> dict[str, Any]:
    notifier = None
    if send:
        configure_trade_notifier_from_settings(log_context="market-close-digest")
        notifier = get_trade_notifier()
    async with AsyncSessionLocal() as session:
        result = await run_market_close_digest(
            market=market,
            session_date=session_date,
            session=session,
            send=send,
            notifier=notifier,
        )
    return _to_dict(result)


@task(name="market_close_digest")
async def market_close_digest_task(
    *,
    market: Literal["us", "kr", "crypto"] = "us",
    session_date: date | None = None,
    send: bool = False,
) -> dict[str, Any]:
    return await run_market_close_digest_refresh(
        market=market,
        session_date=session_date,
        send=send,
    )


@flow(name="market_close_digest")
async def market_close_digest_flow(
    *,
    market: Literal["us", "kr", "crypto"] = "us",
    session_date: date | None = None,
    send: bool = False,
) -> dict[str, Any]:
    """Market-close digest; deployment registration is deferred to upstream."""
    return await market_close_digest_task(
        market=market,
        session_date=session_date,
        send=send,
    )
