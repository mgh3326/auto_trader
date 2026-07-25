"""ROB-993 — deterministic correlation-id spine for the strategy loop.

Mirrors ``app.services.live_correlation`` / ``app.services.paper_correlation``
(ROB-705/714): a sha256 hash of the decision-defining fields, namespaced
so downstream forecast/journal queries can distinguish this loop's ids
from KIS/Upbit/Toss live and paper correlation ids. Pure: no I/O.
"""

from __future__ import annotations

import hashlib


def strategy_loop_correlation_id(
    *,
    strategy_loop_tag: str,
    symbol: str,
    side: str,
    decision_ts: int,
    rung: int = 0,
) -> str:
    canonical = "|".join(
        (
            strategy_loop_tag,
            symbol.upper(),
            side.lower(),
            str(decision_ts),
            str(rung),
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"binance-demo-strategy-loop:{strategy_loop_tag}:{digest}"
