"""ROB-506 — outbound client for the external news-relevance judgment boundary.

POSTs a pending batch to a Hermes-compatible webhook. Two supported reply
shapes (single contract, minimal coupling):

* 2xx with ``{"judgments": [...]}`` — synchronous judge endpoint; the
  worker applies them via ``symbol_news_store.apply_judgment``.
* 2xx without ``judgments`` — fire-and-forget dispatch; the Hermes session
  judges asynchronously and writes back through the existing token-authed
  ``/trading/api/news-relevance/ingest/bulk`` route (ROB-491).

No in-process LLM, no OpenRouter. Token values are never logged and never
appear in result objects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.news_relevance import NewsRelevanceJudgment

logger = logging.getLogger(__name__)

_CONTRACT_NOTE = {
    "inline_response": (
        "optional: reply 2xx {'judgments': [NewsRelevanceJudgment, ...]}"
    ),
    "writeback_route": "/trading/api/news-relevance/ingest/bulk",
    "criteria_runbook": "docs/runbooks/news-relevance-judgment.md",
}


@dataclass(frozen=True)
class JudgmentClientResult:
    status: Literal["judged", "dispatched", "failed", "skipped"]
    judgments: list[NewsRelevanceJudgment] = field(default_factory=list)
    http_status: int | None = None
    reason: str | None = None
    invalid_count: int = 0


class NewsRelevanceJudgmentClient:
    """httpx wrapper mirroring ``HermesNotificationClient`` (ROB-265)."""

    def __init__(
        self,
        webhook_url: str | None = None,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._webhook_url = (
            webhook_url
            if webhook_url is not None
            else settings.NEWS_RELEVANCE_JUDGMENT_WEBHOOK_URL
        )
        self._token = (
            token if token is not None else settings.NEWS_RELEVANCE_JUDGMENT_TOKEN
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout_seconds or settings.NEWS_RELEVANCE_JUDGMENT_TIMEOUT_S,
        )

    async def request_judgments(
        self,
        *,
        market: str,
        symbol: str | None,
        pending: list[dict[str, Any]],
    ) -> JudgmentClientResult:
        if not self._webhook_url:
            return JudgmentClientResult(
                status="skipped", reason="webhook_url_not_configured"
            )

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload = {
            "kind": "news_relevance_judgment_request",
            "market": market,
            "symbol": symbol,
            "pending": pending,
            "contract": _CONTRACT_NOTE,
        }

        try:
            response = await self._client.post(
                self._webhook_url, json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "news-relevance judgment request raised: market=%s symbol=%s error=%s",
                market,
                symbol,
                type(exc).__name__,
            )
            return JudgmentClientResult(status="failed", reason="request_failed")

        if not (200 <= response.status_code < 300):
            logger.warning(
                "news-relevance judgment non-2xx: market=%s symbol=%s http_status=%s",
                market,
                symbol,
                response.status_code,
            )
            return JudgmentClientResult(
                status="failed",
                http_status=response.status_code,
                reason=f"http_{response.status_code}",
            )

        try:
            body = response.json()
        except ValueError:
            body = None
        raw_judgments = body.get("judgments") if isinstance(body, dict) else None
        if not isinstance(raw_judgments, list):
            return JudgmentClientResult(
                status="dispatched", http_status=response.status_code
            )

        judgments: list[NewsRelevanceJudgment] = []
        invalid = 0
        for item in raw_judgments:
            try:
                judgments.append(NewsRelevanceJudgment.model_validate(item))
            except ValidationError:
                invalid += 1
        if invalid:
            logger.warning(
                "news-relevance judgment response had invalid items: "
                "market=%s symbol=%s invalid=%s",
                market,
                symbol,
                invalid,
            )
        return JudgmentClientResult(
            status="judged",
            judgments=judgments,
            http_status=response.status_code,
            invalid_count=invalid,
        )

    async def close(self) -> None:
        await self._client.aclose()
