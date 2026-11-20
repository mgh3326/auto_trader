"""What one forward proved. A leaf module: it imports nothing of the app, so the
shadow forwarder can use it without pulling in any order code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ForwardResult:
    """What a broker service call proved. Anything short of ``accepted`` is
    treated as an unknown outcome, because a failure result does not prove that
    the broker received nothing.

    hk 5441 adds two proofs, both off by default so the kis_mock forwarder is
    unchanged. ``shadowed``: a shadow forwarder ran and sent nothing by
    construction. ``not_sent_reason``: the request provably never reached the
    broker (design B29), with the closed reason.
    """

    accepted: bool
    broker_order_id: str | None
    result: dict[str, Any]
    shadowed: bool = False
    not_sent_reason: str | None = None
