"""Immutable order_proposal payload hashing (ROB-816, principle #1).

stdlib only. Prices/quantities are canonical strings (caller normalizes Decimals)
so the hash never depends on float representation. Time/TTL fields are deliberately
excluded: a TTL-only change must NOT change the payload hash (revalidate, not replace).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ProposalRungSpec:
    rung_index: int
    side: str
    quantity: str
    limit_price: str | None
    notional: str | None


def compute_proposal_payload_hash(
    *,
    symbol: str,
    market: str,
    account_mode: str,
    order_type: str,
    rungs: Sequence[ProposalRungSpec],
    exit_intent: str | None = None,
    exit_reason: str | None = None,
    retrospective_id: int | None = None,
    approval_issue_id: str | None = None,
) -> str:
    canonical = {
        "symbol": symbol,
        "market": market,
        "account_mode": account_mode,
        "order_type": order_type,
        "exit_intent": exit_intent,
        "exit_reason": exit_reason,
        "retrospective_id": retrospective_id,
        "approval_issue_id": approval_issue_id,
        "rungs": [
            {
                "rung_index": r.rung_index,
                "side": r.side,
                "quantity": r.quantity,
                "limit_price": r.limit_price,
                "notional": r.notional,
            }
            for r in sorted(rungs, key=lambda r: r.rung_index)
        ],
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
