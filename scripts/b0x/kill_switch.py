"""B0-X kill switch — contract §2-4 / §4 「일일 정지(kill)」.

Deliberate distinction, because §4 mixes two different kinds of number in one
table and conflating them would either over- or under-stop the experiment:

  * **Kill** — the ``일일 정지(kill)`` row only: *일 손실 5 USDT*. Reaching it
    stops **all** new order submission for the rest of the UTC day, cancels
    outstanding B0-X orders, and raises an operator notice. Resumption is an
    operator decision (§2-4), so nothing in this module can clear it — the
    day has to roll over, or the operator has to act.
  * **Caps** — per-order notional, per-symbol total, concurrent positions,
    daily new entries. These bound individual orders and are applied inside
    :mod:`scripts.b0x.derivation` per leg, with a recorded reason. A saturated
    cap is a normal cycle outcome, not a stop.

``evaluate`` never short-circuits: it returns every tripped reason so the
observation log shows the full state, not just the first thing that fired.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from scripts.b0x.envelope import Envelope
from scripts.b0x.state import LaneAccountState


class KillReason:
    DAILY_LOSS_BUDGET_REACHED = "daily_loss_budget_reached"


class CapStatus:
    CONCURRENT_POSITIONS_SATURATED = "concurrent_positions_saturated"
    DAILY_NEW_ENTRY_CAP_SATURATED = "daily_new_entry_cap_saturated"


@dataclass(frozen=True, slots=True)
class KillSwitchDecision:
    """``allow_new_orders=False`` means: submit nothing, cancel everything."""

    allow_new_orders: bool
    kill_reasons: tuple[str, ...] = ()
    cap_status: tuple[str, ...] = ()
    realized_pnl_today: Decimal = Decimal("0")
    daily_loss_kill: Decimal = Decimal("0")

    @property
    def tripped(self) -> bool:
        return bool(self.kill_reasons)

    def canonical(self) -> dict[str, object]:
        return {
            "allow_new_orders": self.allow_new_orders,
            "kill_reasons": list(self.kill_reasons),
            "cap_status": list(self.cap_status),
            "realized_pnl_today": format(self.realized_pnl_today, "f"),
            "daily_loss_kill": format(self.daily_loss_kill, "f"),
        }

    def operator_notice(self, *, lane: str) -> str | None:
        """Human-readable §2-4 notification text, or ``None`` when not tripped."""

        if not self.tripped:
            return None
        return (
            f"[B0-X KILL SWITCH] lane={lane} — {', '.join(self.kill_reasons)}. "
            f"realized_pnl_today={format(self.realized_pnl_today, 'f')} "
            f"(limit -{format(self.daily_loss_kill, 'f')}). "
            "신규 주문 중단 + 잔여 주문 취소 완료. 재개는 운영자 결정 (계약 §2-4)."
        )


def evaluate(*, state: LaneAccountState, envelope: Envelope) -> KillSwitchDecision:
    """Evaluate the §4 kill row plus the informational cap saturation flags."""

    kill_reasons: list[str] = []
    # realized_pnl_today is signed; a loss is negative. The kill fires when the
    # *loss* reaches the budget, i.e. pnl <= -budget.
    if state.realized_pnl_today <= -envelope.daily_loss_kill:
        kill_reasons.append(KillReason.DAILY_LOSS_BUDGET_REACHED)

    cap_status: list[str] = []
    if len(state.positions) >= envelope.max_concurrent_positions:
        cap_status.append(CapStatus.CONCURRENT_POSITIONS_SATURATED)
    if len(set(state.new_entry_symbols_today)) >= envelope.max_new_entries_per_utc_day:
        cap_status.append(CapStatus.DAILY_NEW_ENTRY_CAP_SATURATED)

    return KillSwitchDecision(
        allow_new_orders=not kill_reasons,
        kill_reasons=tuple(kill_reasons),
        cap_status=tuple(cap_status),
        realized_pnl_today=state.realized_pnl_today,
        daily_loss_kill=envelope.daily_loss_kill,
    )


__all__ = [
    "KillReason",
    "CapStatus",
    "KillSwitchDecision",
    "evaluate",
]
