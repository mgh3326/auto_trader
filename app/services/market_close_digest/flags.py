"""Deterministic improvement flags (ROB-1297).

Each flag is a 1-line string derived from counts/markers already on the
snapshot. No LLM, no free-text judgment.
"""

from __future__ import annotations

from app.services.market_close_digest.types import DigestSnapshot


def improvement_flags(snapshot: DigestSnapshot) -> tuple[str, ...]:
    flags: list[str] = []
    if snapshot.oversell_blocked:
        symbols = ",".join(block.symbol for block in snapshot.oversell_blocked)
        flags.append(
            f"오버셀 차단 {len(snapshot.oversell_blocked)}건"
            f" — 매도수량>주문가능 ({symbols})"
        )
    if snapshot.card_count > 0 and snapshot.auto_approve_count == 0:
        flags.append(f"자동승인 0 — 전건 카드 {snapshot.card_count}")
    elif snapshot.card_count > snapshot.auto_approve_count > 0:
        flags.append(
            f"카드 {snapshot.card_count} > 자동승인 {snapshot.auto_approve_count}"
        )
    return tuple(flags)
