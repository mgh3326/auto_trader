"""hk 7004 — Discord alerts for autonomous strategy orders (design 6939 I12).

The order path calls this module after its own commit, for six transitions: a
live order accepted, refused or uncertain, a booked fill, an automatic
stop-loss send, and an account kill freeze. Callers are wired in later tickets
(N10+); this module is the emitter, its event vocabulary and its message shape.

* **One message per (kind, event key).** The dedupe identity is the event
  kind plus the caller's full, untruncated ``event_key`` — two transitions
  that share a journal key but differ in kind are different messages, and
  two keys that only share a display-length prefix never merge. The key
  itself must name ONE transition: for append-only journal or ledger rows
  the row's own key may suffice per kind, but a mutable row — the
  ``account_control`` freeze is one row per account — must carry the
  transition discriminator inside ``event_key`` (e.g. the freeze row's
  ``updated_at`` or an activation sequence) so a later freeze after
  re-activation is a new identity, not a duplicate. Display truncates the
  key; dedupe never does. A delivery that failed or timed out releases the
  key — nothing was proven delivered, so a retry may try again; an
  ambiguous timeout can then duplicate one message, the same trade-off the
  TREND alerting path makes.
* **Fail-open.** Delivery runs as a background task bounded by a 5 s timeout
  and every exception is swallowed. The caller's result, exit code and timing
  never change. ``drain`` waits, bounded, for in-flight sends on shutdown.
* **No trading surface.** Nothing here imports a broker, order, ledger or
  database module — directly or transitively; the Discord send goes through
  the ``app.core.discord_transport`` leaf, not the ``trade_notifier``
  package whose ``__init__`` pulls the DB-backed notifier services. The
  payload is one Discord embed posted to the existing ops webhook
  (``settings.discord_webhook_alerts``). The account field is a closed
  account mode (``kis_live``, ``toss_live``, ``upbit_live``, mocks); anything
  that looks like a broker account number is replaced by a fixed placeholder.

Every entry point is a no-op unless ``AUTONOMOUS_ORDER_ALERTS_ENABLED`` is
exactly ``true``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import httpx

from app.core.config import settings
from app.core.discord_transport import send_discord_embed_single

logger = logging.getLogger(__name__)

ALERTS_ENABLED_ENV = "AUTONOMOUS_ORDER_ALERTS_ENABLED"
SEND_TIMEOUT_SECONDS = 5.0
DRAIN_TIMEOUT_SECONDS = SEND_TIMEOUT_SECONDS + 2.0
#: Delivered keys remembered for retry idempotency. Old keys are evicted first,
#: so a retry older than the last N deliveries can post again.
MAX_REMEMBERED_KEYS = 2048
_FIELD_MAX = 200


class OrderAlertKind(StrEnum):
    ORDER_ACCEPTED = "order_accepted"
    ORDER_REFUSED = "order_refused"
    ORDER_UNCERTAIN = "order_uncertain"
    FILL_BOOKED = "fill_booked"
    STOP_LOSS_SENT = "stop_loss_sent"
    KILL_FREEZE = "kill_freeze"


@dataclass(frozen=True)
class OrderAlertEvent:
    """One alertable transition.

    ``event_key`` is the durable identity of THIS transition that the caller
    already holds (journal key plus transition, ledger fill, kill row plus
    activation, ...): together with ``kind`` it is what makes a retried call
    one message and a later transition on the same row a new message.
    ``account`` is the account mode; ``quantity`` and ``price`` are exact
    decimal strings, never floats. ``plan_head_id``/``plan_version``/
    ``plan_row_key`` are the daily_plan binding of the intent, when the event
    has one; ``plan_head_id`` is the ``review.daily_plan_heads.id`` UUID.
    """

    kind: OrderAlertKind
    event_key: str
    account: str
    symbol: str
    side: str | None = None
    quantity: str | None = None
    price: str | None = None
    plan_head_id: UUID | None = None
    plan_version: int | None = None
    plan_row_key: str | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.plan_head_id is not None and not isinstance(self.plan_head_id, UUID):
            raise TypeError(
                "plan_head_id must be a uuid.UUID or None, got "
                f"{type(self.plan_head_id).__name__}"
            )


class AlertChannel(Protocol):
    async def send(self, event: OrderAlertEvent) -> bool: ...


def alerts_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Exact-string gate like the other demo flags: only lowercase ``true``."""
    env = os.environ if environ is None else environ
    return env.get(ALERTS_ENABLED_ENV) == "true"


def _clean(value: object, *, limit: int = _FIELD_MAX) -> str:
    return " ".join(str(value).split())[:limit]


def _field(value: object) -> str:
    if value is None:
        return "-"
    return _clean(value) or "-"


#: Two or more digits in a row is a broker identifier, never an account mode.
_ACCOUNT_NUMBER = re.compile(r"[0-9]{2,}")


def mask_account(value: str | None) -> str:
    """Keep the account mode readable; never reveal part of an account number.

    A fixed placeholder (not a suffix reveal), like ``mask_account_identifier``.
    """
    text = _clean(value) if value else ""
    if not text:
        return "-"
    return "[MASKED]" if _ACCOUNT_NUMBER.search(text) else text


_SIDE_LABELS = {"buy": "매수", "sell": "매도"}

_TITLES = {
    OrderAlertKind.ORDER_ACCEPTED: "자동 주문 접수",
    OrderAlertKind.ORDER_REFUSED: "자동 주문 거부",
    OrderAlertKind.ORDER_UNCERTAIN: "자동 주문 결과 불명",
    OrderAlertKind.FILL_BOOKED: "자동 주문 체결 기록",
    OrderAlertKind.STOP_LOSS_SENT: "자동 손절 매도 전송",
    OrderAlertKind.KILL_FREEZE: "계좌 킬 스위치 동결",
}

_COLORS = {
    OrderAlertKind.ORDER_ACCEPTED: 0x2ECC71,
    OrderAlertKind.FILL_BOOKED: 0x2ECC71,
    OrderAlertKind.ORDER_UNCERTAIN: 0xE67E22,
    OrderAlertKind.ORDER_REFUSED: 0xE74C3C,
    OrderAlertKind.STOP_LOSS_SENT: 0xE74C3C,
    OrderAlertKind.KILL_FREEZE: 0x992D22,
}


def _plan_text(event: OrderAlertEvent) -> str:
    parts: list[str] = []
    if event.plan_head_id is not None:
        parts.append(f"head {event.plan_head_id}")
    if event.plan_version is not None:
        parts.append(f"seq {event.plan_version}")
    if event.plan_row_key:
        parts.append(f"row {_clean(event.plan_row_key)}")
    return " · ".join(parts) or "-"


def alert_embed(event: OrderAlertEvent) -> dict[str, Any]:
    """Discord embed: the event, its plan binding and its reason — no secrets."""
    side = _clean(event.side) if event.side else ""
    return {
        "title": _TITLES[event.kind],
        "color": _COLORS[event.kind],
        "fields": [
            {"name": "이벤트", "value": event.kind.value, "inline": True},
            {"name": "계좌", "value": mask_account(event.account), "inline": True},
            {"name": "종목", "value": _field(event.symbol), "inline": True},
            {
                "name": "구분",
                "value": _SIDE_LABELS.get(side, side) or "-",
                "inline": True,
            },
            {"name": "수량", "value": _field(event.quantity), "inline": True},
            {"name": "가격", "value": _field(event.price), "inline": True},
            {"name": "계획", "value": _plan_text(event), "inline": False},
            {"name": "사유", "value": _field(event.reason_code), "inline": True},
            {
                "name": "이벤트 키",
                "value": f"`{_clean(event.event_key)}`",
                "inline": False,
            },
        ],
    }


class DiscordAlertChannel:
    """The existing ops channel: ``settings.discord_webhook_alerts``."""

    def __init__(self, webhook_url: str | None) -> None:
        self._webhook_url = webhook_url

    async def send(self, event: OrderAlertEvent) -> bool:
        if not self._webhook_url:
            logger.info(
                "autonomous order alert not sent: discord_webhook_alerts unset kind=%s",
                event.kind.value,
            )
            return False
        async with httpx.AsyncClient(
            timeout=SEND_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            return await send_discord_embed_single(
                http_client=client,
                webhook_url=self._webhook_url,
                embed=alert_embed(event),
            )


class AutonomousOrderAlerter:
    """One message per (kind, event key), sent off the caller's path.

    ``emit`` never waits and never raises: it claims the key, schedules the
    send and returns. A duplicate (kind, key) pair is dropped while the
    first send is still in flight and after it was delivered.
    """

    def __init__(
        self,
        *,
        channel: AlertChannel | None,
        enabled: bool,
        send_timeout: float = SEND_TIMEOUT_SECONDS,
        max_remembered: int = MAX_REMEMBERED_KEYS,
    ) -> None:
        self._channel = channel
        self._enabled = enabled
        self._send_timeout = send_timeout
        self._max_remembered = max_remembered
        self._delivered: OrderedDict[tuple[OrderAlertKind, str], None] = OrderedDict()
        self._in_flight: set[tuple[OrderAlertKind, str]] = set()
        self._pending: set[asyncio.Task[bool]] = set()

    @property
    def enabled(self) -> bool:
        return self._enabled and self._channel is not None

    def emit(self, event: OrderAlertEvent) -> bool:
        """Schedule one alert; True when a send was scheduled, not delivered."""
        try:
            if not self.enabled:
                return False
            # Dedupe on the full, untruncated key: ``_clean``'s 200-char cap
            # is for display only, and two distinct identities that share a
            # prefix must never merge.
            identity = " ".join(str(event.event_key).split())
            if not identity:
                logger.warning(
                    "autonomous order alert dropped: empty event key kind=%s",
                    event.kind.value,
                )
                return False
            key = (event.kind, identity)
            if key in self._delivered or key in self._in_flight:
                return False
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning(
                    "autonomous order alert dropped: no running event loop kind=%s",
                    event.kind.value,
                )
                return False
            task = loop.create_task(self._deliver(event, key))
            self._in_flight.add(key)
            self._pending.add(task)
            task.add_done_callback(self._forget)
            return True
        except Exception:  # noqa: BLE001 - alert delivery is never trading truth
            logger.exception("autonomous order alert hook failed")
            return False

    async def drain(self, *, timeout: float | None = None) -> None:
        """Wait, bounded, for in-flight sends; cancel any that overrun."""
        pending = [task for task in self._pending if not task.done()]
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout if timeout is not None else DRAIN_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            logger.warning("autonomous order alert drain overran; pending cancelled")

    async def _deliver(
        self, event: OrderAlertEvent, key: tuple[OrderAlertKind, str]
    ) -> bool:
        channel = self._channel
        if channel is None:  # guarded by ``enabled``; belt and braces
            self._in_flight.discard(key)
            return False
        try:
            delivered = bool(
                await asyncio.wait_for(channel.send(event), timeout=self._send_timeout)
            )
        except Exception as exc:  # noqa: BLE001 - never trading truth
            logger.warning(
                "autonomous order alert delivery failed kind=%s error_class=%s",
                event.kind.value,
                type(exc).__name__,
            )
            return False
        finally:
            self._in_flight.discard(key)
        if delivered:
            self._remember(key)
        return delivered

    def _remember(self, key: tuple[OrderAlertKind, str]) -> None:
        self._delivered[key] = None
        self._delivered.move_to_end(key)
        while len(self._delivered) > self._max_remembered:
            self._delivered.popitem(last=False)

    def _forget(self, task: asyncio.Task[bool]) -> None:
        self._pending.discard(task)
        # Retrieve the outcome so a bug here is never an unhandled
        # "exception was never retrieved" report on the caller's loop.
        _ = task.cancelled() or task.exception()


def build_default_alerter(
    environ: Mapping[str, str] | None = None,
) -> AutonomousOrderAlerter:
    return AutonomousOrderAlerter(
        channel=DiscordAlertChannel(settings.discord_webhook_alerts),
        enabled=alerts_enabled(environ),
    )


_default_alerter: AutonomousOrderAlerter | None = None


def get_default_alerter() -> AutonomousOrderAlerter:
    """Process-wide alerter; its delivered-key memory is what makes retries
    idempotent, so callers must not build a fresh one per event."""
    global _default_alerter
    if _default_alerter is None:
        _default_alerter = build_default_alerter()
    return _default_alerter


def reset_default_alerter() -> None:
    """Drop the process-wide instance (tests, or after a settings reload)."""
    global _default_alerter
    _default_alerter = None


def emit_order_alert(event: OrderAlertEvent) -> bool:
    """Call after commit: schedule one alert for ``event``. Never raises."""
    return get_default_alerter().emit(event)
