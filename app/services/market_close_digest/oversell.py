"""Deterministic oversell-block classifier (ROB-1297).

A block is oversell iff the proposal is a sell AND the stored reason matches
an explicit sellable-qty overflow marker. Buy-side cash failures
(``insufficient balance``) are not oversell.
"""

from __future__ import annotations

from app.services.market_close_digest.types import OversellBlock, ProposalRow

OVERSELL_MARKERS: tuple[str, ...] = (
    "exceeds orderable",
    "exceeds sellable",
    "quantity_exceeds_sellable",
    "qty_exceeds",
    "quantity exceeds",
    "loss_cut_confirmation_quantity_exceeds_sellable",
)


def is_oversell_reason(reason: str | None) -> bool:
    text = (reason or "").lower()
    if not text:
        return False
    return any(marker in text for marker in OVERSELL_MARKERS)


def is_oversell_block(*, side: str, reason: str | None) -> bool:
    return side == "sell" and is_oversell_reason(reason)


def collect_oversell_blocks(
    proposals: tuple[ProposalRow, ...],
) -> tuple[OversellBlock, ...]:
    blocks: list[OversellBlock] = []
    seen: set[tuple[str, str]] = set()
    for row in proposals:
        if not is_oversell_block(side=row.side, reason=row.void_reason):
            continue
        reason = (row.void_reason or "").strip()
        key = (row.symbol, reason)
        if key in seen:
            continue
        seen.add(key)
        blocks.append(OversellBlock(symbol=row.symbol, reason=reason))
    return tuple(blocks)
