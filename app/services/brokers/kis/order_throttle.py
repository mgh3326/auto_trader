"""KIS gateway throttle rejections on order POSTs (ROB-BAC).

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

The process-local limiter cannot prevent these on its own: KIS meters per app
key across *all* TRs, while :data:`app.core.config.DEFAULT_KIS_API_RATE_LIMITS`
buckets per endpoint, and several processes share one key. So a throttle
rejection is expected occasionally and must be survivable.

This module holds the narrow classifier + backoff used by the order paths. The
re-POST it enables is gated on the send outcome being provably ``NOT_CREATED``;
see :mod:`app.services.brokers.kis.send_outcome`.
"""

from __future__ import annotations

# Bounded re-POST cap for gateway throttle rejections. Mirrors the existing
# token-expiry cap (ROB-739): a small finite number, never an unbounded loop.
MAX_THROTTLE_RESUBMITS = 2

# Documented KIS gateway throttle codes. EGW00201 is the account/app-key-wide
# per-second limit observed on live overseas orders; EGW00215 is the ledger
# limit ROB-585 originally paced against.
THROTTLE_MSG_CODES = frozenset({"EGW00201", "EGW00215"})


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


def throttle_backoff_seconds(depth: int) -> float:
    """Backoff before re-POST attempt ``depth`` (0-based).

    The limit is per second, so waiting out the current window is enough; the
    delay grows so a second collision is not retried at the same cadence.
    """

    return 0.25 * (2 ** max(depth, 0))
