"""ROB-265 Plan 4 — Hermes review-trigger notification client.

Hermes is the operator review surface auto_trader delivers watch-trigger
events to. This client POSTs a closed payload (see
:class:`ReviewTriggerPayload`) carrying the full immutable trigger
identity snapshot from ``investment_watch_events`` plus the linkage
back to the source report / item / alert.

OpenClaw is the legacy notification surface and is intentionally not
touched from this module. Plan 5 will remove the OpenClaw-flavoured
watch-alert path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict

from app.core.config import settings
from app.schemas.investment_reports import (
    ItemIntentLiteral,
    MarketLiteral,
    TargetKindLiteral,
    WatchActionModeLiteral,
    WatchClauseOpLiteral,
    WatchMetricLiteral,
)

logger = logging.getLogger(__name__)

WatchEventOutcomeLiteral = Literal[
    "notified",
    "review_required",
    "preview_attached",
    "expired",
    "ignored",
    "failed",
]


class ReviewTriggerPayload(BaseModel):
    """Closed contract sent to Hermes when a watch fires.

    Carries every field the operator needs to re-evaluate the watch
    without round-tripping back to auto_trader: full trigger identity
    snapshot + source report/item/alert linkage + correlation_id
    semantics preserved from the event row.
    """

    event_uuid: UUID
    alert_uuid: UUID
    source_report_uuid: UUID
    source_item_uuid: UUID
    correlation_id: str
    kst_date: str
    market: MarketLiteral
    target_kind: TargetKindLiteral
    symbol: str
    metric: WatchMetricLiteral
    operator: WatchClauseOpLiteral
    threshold: Decimal
    threshold_high: Decimal | None = None
    threshold_key: str
    intent: ItemIntentLiteral
    action_mode: WatchActionModeLiteral
    current_value: Decimal | None
    scanner_snapshot: dict[str, Any]
    outcome: WatchEventOutcomeLiteral

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class HermesDeliveryResult:
    status: Literal["success", "skipped", "failed"]
    http_status: int | None = None
    reason: str | None = None


class HermesNotificationClient:
    """POSTs :class:`ReviewTriggerPayload` to the Hermes webhook.

    When ``HERMES_ENABLED`` is False (default) the client logs the
    intended delivery and returns ``status='skipped'`` without making
    the HTTP request. This keeps dev/test runs from hitting an external
    URL while still exercising the payload-building path.
    """

    _DEFAULT_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        webhook_url: str | None = None,
        token: str | None = None,
        enabled: bool | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._webhook_url = webhook_url or settings.HERMES_WEBHOOK_URL
        self._token = token if token is not None else settings.HERMES_TOKEN
        self._enabled = settings.HERMES_ENABLED if enabled is None else enabled
        self._timeout = timeout_seconds or self._DEFAULT_TIMEOUT_SECONDS
        self._client = httpx.AsyncClient(transport=transport, timeout=self._timeout)

    async def send_review_trigger(
        self, payload: ReviewTriggerPayload
    ) -> HermesDeliveryResult:
        if not self._enabled:
            logger.debug(
                "Hermes disabled — skipping review-trigger delivery: "
                "event_uuid=%s alert_uuid=%s outcome=%s",
                payload.event_uuid,
                payload.alert_uuid,
                payload.outcome,
            )
            return HermesDeliveryResult(status="skipped")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        body = payload.model_dump_json(by_alias=True)

        try:
            response = await self._client.post(
                self._webhook_url, content=body, headers=headers
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Hermes review-trigger delivery raised: event_uuid=%s error=%s",
                payload.event_uuid,
                exc,
            )
            return HermesDeliveryResult(status="failed", reason="request_failed")

        if 200 <= response.status_code < 300:
            return HermesDeliveryResult(
                status="success", http_status=response.status_code
            )

        logger.warning(
            "Hermes review-trigger delivery non-2xx: "
            "event_uuid=%s http_status=%s body=%s",
            payload.event_uuid,
            response.status_code,
            response.text[:200],
        )
        return HermesDeliveryResult(
            status="failed",
            http_status=response.status_code,
            reason=f"http_{response.status_code}",
        )

    async def close(self) -> None:
        await self._client.aclose()
