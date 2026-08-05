"""Best-effort Discord observation boundary for the KIS mock runner.

Webhook delivery is deliberately orthogonal to trading logic: a delivery
failure records an alert retry but can never transform a rejected/no-submit
decision into success or cause a broker retry.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

KR_WEBHOOK_ENV = "DISCORD_WEBHOOK_KR"
ALERTS_WEBHOOK_ENV = "DISCORD_WEBHOOK_ALERTS"


@dataclass(frozen=True)
class DeliveryReport:
    channel: str
    delivered: bool
    skipped: bool = False
    retry_recorded: bool = False


class RetryRecorder(Protocol):
    async def record(self, *, channel: str, event: str, error_type: str) -> None: ...


class LoggingRetryRecorder:
    """Scheduleless retry evidence: structured log only, never a hidden job."""

    async def record(self, *, channel: str, event: str, error_type: str) -> None:
        logger.warning(
            "KIS mock notification retry recorded channel=%s event=%s error_type=%s",
            channel,
            event,
            error_type,
        )


WebhookPost = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _post_webhook(url: str, payload: dict[str, Any]) -> None:
    """Post a compact event without logging or otherwise exposing the URL."""
    import httpx

    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()


class DiscordNotifier:
    def __init__(
        self,
        environment: Mapping[str, str],
        *,
        post: WebhookPost = _post_webhook,
        retry_recorder: RetryRecorder | None = None,
    ) -> None:
        self._environment = environment
        self._post = post
        self._retry_recorder = retry_recorder or LoggingRetryRecorder()

    async def lifecycle(self, *, event: str, payload: dict[str, Any]) -> DeliveryReport:
        return await self._deliver(
            channel="kr_lifecycle", env_key=KR_WEBHOOK_ENV, event=event, payload=payload
        )

    async def alert(self, *, event: str, payload: dict[str, Any]) -> DeliveryReport:
        return await self._deliver(
            channel="alerts", env_key=ALERTS_WEBHOOK_ENV, event=event, payload=payload
        )

    async def _deliver(
        self,
        *,
        channel: str,
        env_key: str,
        event: str,
        payload: dict[str, Any],
    ) -> DeliveryReport:
        url = str(self._environment.get(env_key, "")).strip()
        if not url:
            return DeliveryReport(channel=channel, delivered=False, skipped=True)
        try:
            await self._post(url, {"event": event, "payload": payload})
        except Exception as exc:  # noqa: BLE001 - notification is never trading truth
            await self._retry_recorder.record(
                channel=channel, event=event, error_type=type(exc).__name__
            )
            return DeliveryReport(
                channel=channel,
                delivered=False,
                retry_recorded=True,
            )
        return DeliveryReport(channel=channel, delivered=True)
