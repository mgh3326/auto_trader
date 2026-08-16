"""KIS gateway throttle rejections on order POSTs.

KIS answers ``EGW00201 초당 거래건수를 초과하였습니다`` with a normal (<500)
HTTP response carrying ``rt_cd != "0"`` and no ``ODNO``. This module turns
only that narrow, response-backed shape into a typed terminal failure for
operator-facing recording and display; it never schedules or authorizes
another order POST.

The process-local limiter cannot prevent these on its own: KIS meters per app key
across *all* TRs, while DEFAULT_KIS_API_RATE_LIMITS buckets per endpoint,
and several processes share one key. EGW00201 is the account/app-key-wide
per-second limit observed in the incident. The incident order POSTs were at
least three seconds apart, so order-only pacing cannot address that shared
budget; any app-key-wide budget design is deliberately separate work.

The response alone cannot establish whether a local KIS order-ledger row
exists. The execution boundary combines it with its reserved idempotency key
and a ledger lookup solely to distinguish a confirmed non-delivery from an
ambiguous failure before persisting and displaying the result.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Documented KIS gateway throttle codes. EGW00201 is the account/app-key-wide
# per-second limit observed on live overseas orders; EGW00215 is retained as a
# separately documented KIS throttle response for failure classification.
THROTTLE_MSG_CODES = frozenset({"EGW00201", "EGW00215"})


def _broker_order_id_from_response(response: Mapping[str, Any]) -> str | None:
    """Extract only a non-blank KIS order number from a provider body."""
    output = response.get("output")
    sources = (response, output) if isinstance(output, Mapping) else (response,)
    for source in sources:
        order_id = str(
            source.get("ODNO")
            or source.get("odno")
            or source.get("ORD_NO")
            or source.get("ord_no")
            or ""
        ).strip()
        if order_id:
            return order_id
    return None


@dataclass
class KISGatewayThrottleRejection(RuntimeError):
    """A narrow, response-backed KIS throttle failure for surfacing.

    This exception is record/display-only: it never authorizes a retry or
    another order POST. Its construction requires the documented normal-HTTP
    gateway shape and no provider order number. The caller may additionally
    prove a reserved idempotency key and absence from the KIS order ledger to
    label the surfaced failure ``not_delivered``.
    """

    message_code: str
    message: str
    http_status: int
    broker_order_id: str | None

    def __str__(self) -> str:
        return f"{self.message_code} {self.message}".strip()


def is_provider_throttle_reject(msg_cd: object, msg1: object) -> bool:
    """True when a ``rt_cd != "0"`` body is a gateway per-second throttle.

    Matches the documented codes first, then falls back to the message text so
    an undocumented sibling code still classifies. The text probe requires both
    "초당" and "초과" so unrelated "초과" messages (e.g. 주문가능금액 초과) are
    not misread as gateway-throttle failures.
    """

    code = str(msg_cd or "").strip().upper()
    if code in THROTTLE_MSG_CODES:
        return True

    message = str(msg1 or "")
    return "초당" in message and "초과" in message


def gateway_throttle_rejection_from_response(
    response: Mapping[str, Any],
    *,
    http_status: int | None,
    send_disposition: str | None,
) -> KISGatewayThrottleRejection | None:
    """Return transport evidence for the documented terminal KIS rejection.

    The response alone is intentionally insufficient to prove non-delivery. It
    is only strong enough to pass the candidate to the execution boundary,
    where local reservation and ledger evidence are available for failure
    recording and display.
    """
    if not isinstance(http_status, int) or not 200 <= http_status < 300:
        return None
    if str(response.get("rt_cd") or "") == "0":
        return None
    message_code = str(response.get("msg_cd") or "").strip().upper()
    # Message-text fallback is useful for observability, but an undocumented
    # code is not sufficient evidence for confirmed non-delivery.
    if message_code not in THROTTLE_MSG_CODES:
        return None
    if send_disposition != "not_created":
        return None
    broker_order_id = _broker_order_id_from_response(response)
    if broker_order_id is not None:
        return None
    return KISGatewayThrottleRejection(
        message_code=message_code,
        message=str(response.get("msg1") or "").strip(),
        http_status=http_status,
        broker_order_id=broker_order_id,
    )


def is_proven_not_delivered_for_surface(
    rejection: KISGatewayThrottleRejection,
    *,
    idempotency_key: str | None,
    intent_reserved: bool,
    ledger_entry_present: bool | None,
) -> bool:
    """Whether evidence proves a KIS throttle failure was not delivered.

    This predicate is only for persisted/card classification; it never
    authorizes another order POST. ``ledger_entry_present is False`` is
    deliberately strict: a failed lookup is ``None`` and remains ambiguous,
    and an existing ledger row is ``True``.
    """
    return (
        rejection.broker_order_id is None
        and bool(idempotency_key and idempotency_key.strip())
        and intent_reserved
        and ledger_entry_present is False
    )
