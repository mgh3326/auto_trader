"""Failure alerts for the manual TREND-TSMOM-v1 Futures Demo runner. Default off.

Three failure kinds reach the ops alert channel auto_trader already uses: the
Discord ``discord_webhook_alerts`` webhook that screener-refresh and
approval-dispatch alerts post to. Hermes is not an ops channel and no new
provider is added here.

* ``stopped``: the runner process ended and the operator did not ask for it
  (an exception escaped the loop, SIGTERM, a cancellation). Ctrl-C (SIGINT) is
  the operator's own stop and is never alerted.
* ``error``: a tick reported a failure event (``blocked``, any ``*_uncertain``,
  ``exposure_mismatch``, ``partial_entry_stop_breached``, an aborted
  entry/exit) or the conflict-week kill switch tripped.
* ``heartbeat_missed``: raised by the watcher script. The runner holds a
  session advisory lock and touches its connection every tick; the watcher
  reads that lock and the backend's ``pg_stat_activity.state_change``. A
  missing lock or an old state change means the runner is dead or hung. This
  is the only kind that can report SIGKILL, an OOM kill or a lost host.

There is no exchange-side stop in this lane, so every kind means stop-loss
observation is degraded while a position is open.

Nothing here touches the broker, a ledger or any trading state. Delivery is
best-effort, bounded by a timeout and sent off the tick path; a failed or slow
webhook never changes what the runner does or when its next tick starts. Every
entry point is a no-op unless ``BINANCE_TREND_ALERT_ENABLED`` is exactly
``true``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

ALERT_ENABLED_ENV = "BINANCE_TREND_ALERT_ENABLED"
PLAYBOOK_DOC = "docs/runbooks/binance-trend-demo.md"

DEFAULT_MISS_MINUTES = 10
MIN_MISS_MINUTES = 3
REPEAT_AFTER = dt.timedelta(hours=6)
RETRY_AFTER_FAILED_SEND = dt.timedelta(minutes=5)
SEND_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 15.0
DRAIN_TIMEOUT_SECONDS = SEND_TIMEOUT_SECONDS + 2.0
_SIGNATURE_MAX = 120


class AlertKind(StrEnum):
    STOPPED = "stopped"
    ERROR = "error"
    HEARTBEAT_MISSED = "heartbeat_missed"
    TEST = "test"


@dataclass(frozen=True)
class TrendAlert:
    kind: AlertKind
    signature: str
    at: dt.datetime


class AlertChannel(Protocol):
    async def send(self, alert: TrendAlert) -> bool: ...


def alert_enabled(environ: Mapping[str, str]) -> bool:
    """Exact-string gate like the other demo flags: only lowercase ``true``."""
    return environ.get(ALERT_ENABLED_ENV) == "true"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _clean(signature: str) -> str:
    return " ".join(str(signature).split())[:_SIGNATURE_MAX]


_TITLES = {
    AlertKind.STOPPED: "TREND 데모 러너가 비정상 종료됨",
    AlertKind.ERROR: "TREND 데모 러너 틱 오류",
    AlertKind.HEARTBEAT_MISSED: "TREND 데모 러너 heartbeat 끊김",
    AlertKind.TEST: "TREND 알림 채널 테스트 (장애 아님)",
}
_NO_EXCHANGE_STOP = "TREND 데모 레인에는 거래소 측 손절이 없다 — 포지션이 열려 있으면 손절 감시가 멈춘 상태다."
_ACTIONS = {
    AlertKind.STOPPED: f"{_NO_EXCHANGE_STOP} {PLAYBOOK_DOC} 의 Incident 절 순서로 truth gate 를 먼저 확인한다.",
    AlertKind.ERROR: f"러너가 blocked/uncertain 틱을 반복하면 신규 진입과 청산이 멈춘다. {PLAYBOOK_DOC} 의 Incident 절을 따른다.",
    AlertKind.HEARTBEAT_MISSED: f"{_NO_EXCHANGE_STOP} tmux 의 러너 창과 컨테이너를 확인한다 ({PLAYBOOK_DOC} Incident 절). 의도한 정지였다면 감시 창을 먼저 끄는 순서를 지킨다.",
    AlertKind.TEST: "수신 확인용 1회 메시지. 조치 없음.",
}


def alert_embed(alert: TrendAlert) -> dict[str, Any]:
    """Discord embed with identifiers only: no credentials, no account values."""
    failure = alert.kind is not AlertKind.TEST
    return {
        "title": ("🚨 " if failure else "") + _TITLES[alert.kind],
        "color": 0xE74C3C if failure else 0x3498DB,
        "fields": [
            {"name": "kind", "value": alert.kind.value, "inline": True},
            {"name": "signature", "value": f"`{alert.signature}`", "inline": True},
            {
                "name": "at (UTC)",
                "value": alert.at.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S"),
                "inline": False,
            },
            {"name": "operator_action", "value": _ACTIONS[alert.kind], "inline": False},
        ],
    }


class DiscordAlertsChannel:
    """The existing ops channel: ``settings.discord_webhook_alerts``."""

    def __init__(self, webhook_url: str | None) -> None:
        self._webhook_url = webhook_url

    async def send(self, alert: TrendAlert) -> bool:
        from app.monitoring.trade_notifier.transports import send_discord_embed_single

        if not self._webhook_url:
            logger.info(
                "trend alert not sent: discord_webhook_alerts unset kind=%s",
                alert.kind.value,
            )
            return False
        async with httpx.AsyncClient(
            timeout=SEND_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            return await send_discord_embed_single(
                http_client=client,
                webhook_url=self._webhook_url,
                embed=alert_embed(alert),
            )


def build_default_channel() -> DiscordAlertsChannel:
    from app.core.config import settings

    return DiscordAlertsChannel(settings.discord_webhook_alerts)


@dataclass
class _Episode:
    signature: str
    attempted_at: dt.datetime
    delivered_at: dt.datetime | None


class TrendAlerter:
    """One alert per failure episode, with bounded reminders.

    An episode is keyed by ``(kind, bucket)``; the bucket is a coarse class the
    caller picks (for tick errors, the event name), never diagnostic text such
    as an exception class that can change from one tick to the next. An
    episode ends when the caller reports health with ``clear(kind)``. While it
    lasts, one alert is sent, a reminder at most every ``repeat_after``, and a
    failed delivery is retried no sooner than ``retry_after``. The episode is
    recorded before delivery starts, so a send still in flight also counts.
    """

    def __init__(
        self,
        *,
        channel: AlertChannel | None,
        enabled: bool,
        clock: Callable[[], dt.datetime] = _utcnow,
        repeat_after: dt.timedelta = REPEAT_AFTER,
        retry_after: dt.timedelta = RETRY_AFTER_FAILED_SEND,
        send_timeout: float = SEND_TIMEOUT_SECONDS,
    ) -> None:
        self._channel = channel
        self._enabled = enabled
        self._clock = clock
        self._repeat_after = repeat_after
        self._retry_after = retry_after
        self._send_timeout = send_timeout
        self._episodes: dict[tuple[AlertKind, str], _Episode] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled and self._channel is not None

    async def fire(self, kind: AlertKind, signature: str, bucket: str = "") -> bool:
        """Return True only when a message was delivered by this call."""
        channel = self._channel
        if not self._enabled or channel is None:
            return False
        cleaned = _clean(signature)
        now = self._clock()
        key = (kind, bucket)
        episode = self._episodes.get(key)
        if episode is not None:
            if episode.delivered_at is not None:
                if now - episode.delivered_at < self._repeat_after:
                    return False
            elif now - episode.attempted_at < self._retry_after:
                return False
        current = _Episode(cleaned, now, None)
        self._episodes[key] = current
        delivered = await self._deliver(channel, TrendAlert(kind, cleaned, now))
        current.delivered_at = now if delivered else None
        return delivered

    def clear(self, kind: AlertKind) -> None:
        for key in [k for k in self._episodes if k[0] is kind]:
            del self._episodes[key]

    def clear_bucket(self, kind: AlertKind, bucket: str) -> None:
        """End one episode; other buckets of the same kind keep their timers."""
        self._episodes.pop((kind, bucket), None)

    async def _deliver(self, channel: AlertChannel, alert: TrendAlert) -> bool:
        try:
            return bool(
                await asyncio.wait_for(channel.send(alert), timeout=self._send_timeout)
            )
        except Exception as exc:  # noqa: BLE001 - alert delivery is never trading truth
            logger.warning(
                "trend alert delivery failed kind=%s error_class=%s",
                alert.kind.value,
                type(exc).__name__,
            )
            return False


class TrendRunMonitor:
    """Maps tick failures and stop causes onto alert fire/clear calls.

    Never raises ``Exception``: the runner must behave the same with the alert
    path broken. ``BaseException`` (cancellation, interrupts) still propagates.
    A tick's alert is sent by a background task, so a slow webhook can never
    delay the next tick; ``drain`` waits (bounded) for those tasks on exit. The
    stop alert is awaited inline because the process is already leaving.
    """

    def __init__(self, alerter: TrendAlerter) -> None:
        self._alerter = alerter
        self._pending: set[asyncio.Task[bool]] = set()
        self._error_buckets: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self._alerter.enabled

    async def tick_done(self, failures: tuple[tuple[str, str], ...] = ()) -> None:
        """``failures`` are ``(signature, bucket)`` pairs from ``control``.

        Each bucket is its own episode: a bucket that disappeared re-arms even
        while another failure persists, so a renewed ``blocked`` inside six
        hours is not suppressed by a still-open ``partial_entry_exposure_open``.
        """
        if not self.enabled:
            return  # flag off: no task, no scheduling difference from no alerts
        try:
            current = {bucket for _, bucket in failures}
            for gone in self._error_buckets - current:
                self._alerter.clear_bucket(AlertKind.ERROR, gone)
            self._error_buckets = current
            for signature, bucket in failures:
                task = asyncio.get_running_loop().create_task(
                    self._alerter.fire(AlertKind.ERROR, signature, bucket)
                )
                self._pending.add(task)
                task.add_done_callback(self._forget)
        except Exception:  # noqa: BLE001
            logger.exception("trend alert tick hook failed")

    def _forget(self, task: asyncio.Task[bool]) -> None:
        self._pending.discard(task)
        # Retrieve the outcome so a bug in the alerter is never an unhandled
        # "exception was never retrieved" report on the runner's loop.
        _ = task.cancelled() or task.exception()

    async def stopped(self, *, operator: bool, reason: str) -> None:
        if operator:
            return
        try:
            await self._alerter.fire(AlertKind.STOPPED, reason)
        except Exception:  # noqa: BLE001
            logger.exception("trend alert stop hook failed")

    async def drain(self) -> None:
        """Wait, bounded, for in-flight tick alerts; cancel any that overrun."""
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._pending, return_exceptions=True),
                timeout=DRAIN_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            logger.warning("trend alert drain overran; pending alerts cancelled")


class HeartbeatVerdict(StrEnum):
    ABSENT = "absent"
    OK = "ok"
    MISSED = "missed"


class LaneClock(Protocol):
    async def last_tick_at(self) -> dt.datetime | None: ...


def judge_heartbeat(
    last_tick_at: dt.datetime | None, *, now: dt.datetime, miss_after: dt.timedelta
) -> HeartbeatVerdict:
    """Liveness judgement for the trend runner.

    Unlike H5, this lane has no durable tick stamp: the runner's loop holds a
    session advisory lock and stamps its connection each tick, so an absent
    lock is the runner's death signal. ``ABSENT`` is therefore a miss too; the
    caller passes that policy explicitly to :func:`poll_heartbeat`.
    """
    if last_tick_at is None:
        return HeartbeatVerdict.ABSENT
    if last_tick_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("aware timestamps required")
    if now - last_tick_at > miss_after:
        return HeartbeatVerdict.MISSED
    return HeartbeatVerdict.OK


async def poll_heartbeat(
    lane: LaneClock,
    alerter: TrendAlerter,
    *,
    miss_after: dt.timedelta,
    now: dt.datetime,
    absent_is_failure: bool = True,
    read_timeout: float = READ_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One watcher poll: read the stamp, judge it, alert or re-arm.

    The read has a deadline: a stalled SELECT must end as an ``unreadable``
    alert, never leave the only hung-runner detector waiting forever.
    """
    try:
        last = await asyncio.wait_for(lane.last_tick_at(), timeout=read_timeout)
    except Exception as exc:  # noqa: BLE001 - a blind watcher is itself a failure
        error_class = type(exc).__name__
        await alerter.fire(
            AlertKind.HEARTBEAT_MISSED, f"unreadable:{error_class}", "unreadable"
        )
        return {"event": "watch", "verdict": "unreadable", "error_class": error_class}
    verdict = judge_heartbeat(last, now=now, miss_after=miss_after)
    if verdict is HeartbeatVerdict.MISSED:
        await alerter.fire(AlertKind.HEARTBEAT_MISSED, "stale_tick", "stale")
    elif verdict is HeartbeatVerdict.ABSENT and absent_is_failure:
        await alerter.fire(AlertKind.HEARTBEAT_MISSED, "runner_absent", "absent")
    else:
        alerter.clear(AlertKind.HEARTBEAT_MISSED)
    return {
        "event": "watch",
        "verdict": verdict.value,
        "last_tick_at": last.isoformat() if last is not None else None,
        "age_seconds": int((now - last).total_seconds()) if last is not None else None,
    }
