"""KIS gateway throttle rejections on order POSTs.

Background
----------
ROB-585/ROB-645 throttle order TRs to 8/s process-locally and then disable every
re-POST (``retry_request_errors=False`` + ``max_retries_override=0``) so an order
whose outcome is ambiguous is never sent twice. That rule is correct for
timeouts, transport errors and 5xx bodies — none of them prove the broker did
not create an order.

It is *not* correct for a gateway throttle rejection. KIS answers
``EGW00201 초당 거래건수를 초과하였습니다`` with a normal (<500) HTTP response
carrying ``rt_cd != "0"`` and no ``ODNO``: the request was declined at the
gateway, before the order engine, and provably no order exists. Treating it as a
terminal rejection burns a live sell for the whole session even though a single
re-POST a fraction of a second later would have been accepted.

The process-local limiter cannot prevent an app-key-scoped or multi-process
burst on its own: :data:`app.core.config.DEFAULT_KIS_API_RATE_LIMITS` buckets
per endpoint and the registry lives only inside one process.  The incident
establishes that such a burst still reached the KIS gateway.

This module deliberately stops at transport evidence.  A response can identify
the narrow gateway-rejection candidate, but it cannot establish that no local
KIS order-ledger row exists.  The order-execution boundary combines this typed
response with its reserved idempotency key and ledger lookup before it permits
the one allowed re-POST.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# A throttle re-POST is a last-resort recovery action, not a transport retry.
# It is admitted only after the order-execution boundary has checked the
# idempotency reservation and KIS order-ledger evidence.
MAX_THROTTLE_RESUBMITS = 1

# Documented KIS gateway throttle codes. EGW00201 is the per-second gateway
# limit observed on live overseas orders; EGW00215 is the ledger limit ROB-585
# originally paced against.
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
    """A narrow, response-backed candidate for a safe KIS order re-POST.

    This exception does *not* itself authorize a retry.  Its construction
    requires the documented normal-HTTP gateway shape and no provider order
    number.  The caller must additionally prove a reserved idempotency key and
    absence from the KIS order ledger.
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
    not misread as retryable.
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
    """Return transport evidence for the documented KIS gateway rejection.

    The response alone is intentionally insufficient to retry.  It is only
    strong enough to pass the candidate to the execution boundary, where the
    local reservation and ledger evidence are available.
    """
    if not isinstance(http_status, int) or not 200 <= http_status < 300:
        return None
    if str(response.get("rt_cd") or "") == "0":
        return None
    message_code = str(response.get("msg_cd") or "").strip().upper()
    # Message-text fallback is useful for observability, but an undocumented
    # code is not sufficient proof for an order re-POST.
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


def is_proven_not_delivered_for_repost(
    rejection: KISGatewayThrottleRejection,
    *,
    idempotency_key: str | None,
    intent_reserved: bool,
    ledger_entry_present: bool | None,
) -> bool:
    """Whether evidence authorizes exactly one KIS gateway-throttle re-POST.

    ``ledger_entry_present is False`` is deliberately strict: a failed lookup
    is ``None`` and remains ambiguous, and an existing ledger row is ``True``.
    Neither case can authorize another order POST.
    """
    return (
        rejection.broker_order_id is None
        and bool(idempotency_key and idempotency_key.strip())
        and intent_reserved
        and ledger_entry_present is False
    )


def throttle_backoff_seconds(depth: int) -> float:
    """Backoff before re-POST attempt ``depth`` (0-based).

    The limit is per second, so waiting out the current window is enough; the
    delay grows so a second collision is not retried at the same cadence.
    """

    return 0.25 * (2 ** max(depth, 0))
