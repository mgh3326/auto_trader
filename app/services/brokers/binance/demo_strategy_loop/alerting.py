"""Best-effort Discord alerts for repeated ROB-993 data-quality failures."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ALERT_ENABLED_ENV = "BINANCE_TREND_ALERT_ENABLED"
REPEAT_AFTER = dt.timedelta(hours=6)
RETRY_AFTER_FAILED_SEND = dt.timedelta(minutes=5)
SEND_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class TickFailure:
    signature: str
    bucket: str


class DataFailureEpisodes:
    """Expose data-quality alerts only after two consecutive ticks per symbol."""

    def __init__(self) -> None:
        self._previous: dict[str, tuple[int, str]] = {}

    def update(self, events: Iterable[object]) -> tuple[TickFailure, ...]:
        current: dict[str, tuple[int, str]] = {}
        for event in events:
            reason = getattr(event, "reason", getattr(event, "event", None))
            symbol = getattr(event, "symbol", None)
            if reason not in {
                "data_missing_bars",
                "data_fetch_failed",
            } or not isinstance(symbol, str):
                continue
            bucket = f"data_quality:{symbol}"
            detail = getattr(event, "detail", None)
            if detail is None:
                detail = getattr(event, "error_class", None)
            signature = f"{symbol}: {reason}"
            if detail:
                signature = f"{signature}: {detail}"
            previous_count, _ = self._previous.get(bucket, (0, ""))
            current[bucket] = (previous_count + 1, signature)
        self._previous = current
        return tuple(
            TickFailure(signature, bucket)
            for bucket, (count, signature) in current.items()
            if count >= 2
        )


def alert_enabled(environ: Mapping[str, str]) -> bool:
    return environ.get(ALERT_ENABLED_ENV) == "true"


async def send_data_quality_alert(failure: TickFailure) -> bool:
    """Send through the existing Discord ops webhook without exposing secrets."""
    from app.core.config import settings
    from app.monitoring.trade_notifier.transports import send_discord_embed_single

    webhook_url = settings.discord_webhook_alerts
    if not webhook_url:
        logger.info("strategy loop data alert not sent: Discord webhook is unset")
        return False
    import httpx

    embed = {
        "title": "Binance Demo 전략 루프 데이터 확인 필요",
        "color": 0xE74C3C,
        "fields": [
            {"name": "symbol / reason", "value": failure.signature, "inline": False},
            {
                "name": "operator_action",
                "value": "시장 데이터 연결과 누락된 봉을 확인하세요.",
                "inline": False,
            },
        ],
    }
    async with httpx.AsyncClient(
        timeout=SEND_TIMEOUT_SECONDS, follow_redirects=False
    ) as client:
        return bool(
            await send_discord_embed_single(
                http_client=client,
                webhook_url=webhook_url,
                embed=embed,
            )
        )


class DataQualityMonitor:
    """Send once per active symbol episode and remind no more often than 6h."""

    def __init__(
        self,
        *,
        enabled: bool,
        send: Callable[[TickFailure], Awaitable[bool]] = send_data_quality_alert,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._enabled = enabled
        self._send = send
        self._clock = clock
        self._attempted_at: dict[str, dt.datetime] = {}
        self._delivered_at: dict[str, dt.datetime] = {}
        self._episode_tokens: dict[str, object] = {}
        self._pending: set[asyncio.Task[bool]] = set()

    async def tick_done(self, failures: tuple[TickFailure, ...]) -> None:
        if not self._enabled:
            return
        try:
            current = {failure.bucket for failure in failures}
            for bucket in self._attempted_at.keys() - current:
                del self._attempted_at[bucket]
                self._delivered_at.pop(bucket, None)
                self._episode_tokens.pop(bucket, None)
            now = self._clock()
            for failure in failures:
                episode_token = self._episode_tokens.setdefault(
                    failure.bucket, object()
                )
                delivered_at = self._delivered_at.get(failure.bucket)
                if delivered_at is not None and now - delivered_at < REPEAT_AFTER:
                    continue
                attempted_at = self._attempted_at.get(failure.bucket)
                if (
                    delivered_at is None
                    and attempted_at is not None
                    and now - attempted_at < RETRY_AFTER_FAILED_SEND
                ):
                    continue
                self._attempted_at[failure.bucket] = now
                task = asyncio.get_running_loop().create_task(
                    self._deliver(
                        failure,
                        attempted_at=now,
                        episode_token=episode_token,
                    )
                )
                self._pending.add(task)
                task.add_done_callback(self._forget)
        except Exception:
            logger.exception("strategy loop data alert hook failed")

    async def _deliver(
        self,
        failure: TickFailure,
        *,
        attempted_at: dt.datetime,
        episode_token: object,
    ) -> bool:
        try:
            delivered = bool(
                await asyncio.wait_for(
                    self._send(failure), timeout=SEND_TIMEOUT_SECONDS + 1
                )
            )
            if delivered and self._episode_tokens.get(failure.bucket) is episode_token:
                self._delivered_at[failure.bucket] = attempted_at
            return delivered
        except Exception as exc:
            logger.warning(
                "strategy loop data alert failed error_class=%s",
                type(exc).__name__,
            )
            return False

    def _forget(self, task: asyncio.Task[bool]) -> None:
        self._pending.discard(task)
        _ = task.cancelled() or task.exception()

    async def drain(self) -> None:
        if not self._pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._pending, return_exceptions=True),
                timeout=SEND_TIMEOUT_SECONDS + 2,
            )
        except Exception:
            logger.warning("strategy loop data alert drain overran")
