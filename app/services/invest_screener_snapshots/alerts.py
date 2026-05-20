"""ROB-281 Stage 6 — Discord operational alerts for screener refresh jobs.

Routes failure / suspicious-distribution / schedule-miss alerts to
``settings.discord_webhook_alerts`` (D3 lock-in — Hermes is the review-trigger
notification contract and is NOT used for ops alerts).

Contract:

* Never invoked on success (``assert_never_called_on_success`` belt-and-braces
  test in :mod:`tests.services.invest_screener_snapshots.test_alerts`).
* Never raises. Alert-delivery failures must not mask the underlying task
  failure that triggered the alert; we log and return ``False`` instead.
* Noop (returns ``False``) when ``settings.discord_webhook_alerts`` is unset.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from app.core.config import settings
from app.monitoring.trade_notifier.transports import send_discord_embed_single

logger = logging.getLogger(__name__)

_EMBED_COLOR_FAILURE = 0xE74C3C  # red, matches existing Discord alert convention
_MESSAGE_TRUNCATE = 1024  # Discord embed field value limit


def _format_distribution_preview(distribution: Mapping[str, int] | None) -> str:
    if not distribution:
        return "n/a"
    top = sorted(distribution.items(), key=lambda kv: kv[1], reverse=True)[:3]
    return ", ".join(f"{date}={count}" for date, count in top)


async def send_screener_refresh_alert(
    *,
    slot: str,
    market: str,
    exception: BaseException,
    distribution: Mapping[str, int] | None = None,
    commit_status: str = "skipped",
    http_client: httpx.AsyncClient | None = None,
) -> bool:
    """Post a failure / guard-violation embed to ``discord_webhook_alerts``.

    Returns ``True`` if the embed was successfully posted, ``False`` otherwise
    (webhook unset, transport error, or unexpected exception during send).
    The caller should re-raise the original ``exception`` after this call so
    TaskIQ records the underlying failure.

    When ``http_client`` is not provided, a short-lived client is created and
    closed inside this function — convenient for one-shot alerts from
    scheduled task bodies.
    """
    webhook = settings.discord_webhook_alerts
    if not webhook:
        logger.info(
            "screener refresh alert noop (discord_webhook_alerts unset): "
            "slot=%s market=%s exc=%s",
            slot,
            market,
            exception.__class__.__name__,
        )
        return False

    exc_class = exception.__class__.__name__
    exc_msg = str(exception)
    if len(exc_msg) > _MESSAGE_TRUNCATE:
        exc_msg = exc_msg[: _MESSAGE_TRUNCATE - 1] + "…"

    embed: dict[str, Any] = {
        "title": f"screener refresh failure — {market} / {slot}",
        "color": _EMBED_COLOR_FAILURE,
        "fields": [
            {"name": "slot", "value": slot, "inline": True},
            {"name": "market", "value": market, "inline": True},
            {"name": "commit", "value": commit_status, "inline": True},
            {"name": "exception", "value": f"`{exc_class}`", "inline": True},
            {
                "name": "snapshot_date distribution (top 3)",
                "value": _format_distribution_preview(distribution),
                "inline": False,
            },
            {"name": "message", "value": f"```\n{exc_msg}\n```", "inline": False},
        ],
    }

    own_client = http_client is None
    if own_client:
        http_client = httpx.AsyncClient(timeout=10.0)
    try:
        return await send_discord_embed_single(
            http_client=http_client,
            webhook_url=webhook,
            embed=embed,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "screener refresh alert send failed: slot=%s market=%s",
            slot,
            market,
        )
        return False
    finally:
        if own_client and http_client is not None:
            await http_client.aclose()
