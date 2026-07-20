"""ROB-993 — kill switch for the Binance Demo strategy loop.

Two gates, both computed fresh from the durable ledger on every tick
(never memory-only, so they survive a process restart):

  * ``max_concurrent_positions`` — reuses
    ``BinanceDemoLedgerService.count_open_lifecycles()``, the same global
    open-root-lifecycle count ROB-844's ``reserve_root_planned`` already
    enforces at the DB layer. Default 1.
  * ``max_consecutive_stop_losses_per_utc_day`` — walks this loop's own
    closed root rows (tagged ``extra_metadata["strategy_loop_tag"]``),
    most-recent-first within the current UTC day, counting a leading run
    of ``exit_reason == "stop_loss"`` closes. Any other exit (take-profit,
    manual, this PR's immediate infra-proof close) breaks the streak.

Pure evaluation function (mirrors ``evaluate_risk`` /
``ScalpingRiskLimits`` in ``demo_scalping.contract``, ROB-307) plus a
DB-reading snapshot builder.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService

EXIT_REASON_STOP_LOSS = "stop_loss"


class KillSwitchReasonCode:
    MAX_CONCURRENT_POSITIONS_REACHED = "max_concurrent_positions_reached"
    CONSECUTIVE_STOP_LOSS_LIMIT_REACHED = "consecutive_stop_loss_limit_reached"


@dataclass(frozen=True)
class StrategyLoopKillSwitchLimits:
    max_concurrent_positions: int = 1
    max_consecutive_stop_losses_per_utc_day: int = 2


@dataclass(frozen=True)
class KillSwitchSnapshot:
    """Durable state read from ``binance_demo_order_ledger``."""

    open_position_count: int
    consecutive_stop_losses_today: int


@dataclass(frozen=True)
class KillSwitchDecision:
    allowed: bool
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


def evaluate_kill_switch(
    *,
    snapshot: KillSwitchSnapshot,
    limits: StrategyLoopKillSwitchLimits,
) -> KillSwitchDecision:
    """Return every tripped reason; empty == allowed (no short-circuit)."""
    reasons: list[str] = []
    if snapshot.open_position_count >= limits.max_concurrent_positions:
        reasons.append(KillSwitchReasonCode.MAX_CONCURRENT_POSITIONS_REACHED)
    if (
        snapshot.consecutive_stop_losses_today
        >= limits.max_consecutive_stop_losses_per_utc_day
    ):
        reasons.append(KillSwitchReasonCode.CONSECUTIVE_STOP_LOSS_LIMIT_REACHED)
    return KillSwitchDecision(allowed=not reasons, reason_codes=tuple(reasons))


def _utc_day_start(now: dt.datetime) -> dt.datetime:
    return dt.datetime.combine(now.date(), dt.time.min, tzinfo=dt.UTC)


async def build_kill_switch_snapshot(
    ledger: BinanceDemoLedgerService,
    *,
    strategy_loop_tag: str,
    now: dt.datetime,
) -> KillSwitchSnapshot:
    """Read a fresh :class:`KillSwitchSnapshot` from the durable ledger.

    ``strategy_loop_tag`` scopes the consecutive-SL walk to this loop's
    own root orders — it never counts closes written by the unrelated
    demo-scalping executor or the ROB-298 smoke CLIs that share the same
    ``binance_demo_order_ledger`` table.
    """
    open_position_count = await ledger.count_open_lifecycles()
    closed_today = await ledger.closed_rows_since(since=_utc_day_start(now))
    own_roots = [
        row
        for row in closed_today
        if row.parent_client_order_id is None
        and (row.extra_metadata or {}).get("strategy_loop_tag") == strategy_loop_tag
    ]
    own_roots.sort(key=lambda row: row.closed_at or row.created_at, reverse=True)
    consecutive_stop_losses = 0
    for row in own_roots:
        if (row.extra_metadata or {}).get("exit_reason") == EXIT_REASON_STOP_LOSS:
            consecutive_stop_losses += 1
        else:
            break
    return KillSwitchSnapshot(
        open_position_count=open_position_count,
        consecutive_stop_losses_today=consecutive_stop_losses,
    )
