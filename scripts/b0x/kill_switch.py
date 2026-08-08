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


class MissingNavForRatioKill(ValueError):
    """``envelope.daily_loss_kill_basis == "pct_of_nav"`` but ``state.nav`` is
    ``None`` — a ratio kill threshold has no absolute value to compare
    ``realized_pnl_today`` against without a NAV snapshot. Fails closed rather
    than silently skipping the kill check or guessing a NAV.
    """


@dataclass(frozen=True, slots=True)
class KillSwitchDecision:
    """``allow_new_orders=False`` means: submit nothing, cancel everything."""

    allow_new_orders: bool
    kill_reasons: tuple[str, ...] = ()
    cap_status: tuple[str, ...] = ()
    realized_pnl_today: Decimal = Decimal("0")
    #: The *effective absolute threshold* actually compared against
    #: ``realized_pnl_today``, always in the same currency/unit as that field
    #: — for ``daily_loss_kill_basis="absolute"`` this equals
    #: ``envelope.daily_loss_kill`` unchanged; for ``"pct_of_nav"`` it is
    #: ``nav_snapshot * envelope.daily_loss_kill``, recomputed every cycle.
    daily_loss_kill: Decimal = Decimal("0")
    #: The raw configured envelope value, before any NAV multiplication —
    #: recorded alongside the effective threshold so an observation record
    #: shows both "what the contract says" and "what was actually compared",
    #: which is what makes a currency/unit mismatch between them auditable
    #: instead of hidden inside a single number.
    daily_loss_kill_basis: str = "absolute"
    daily_loss_kill_config: Decimal = Decimal("0")
    nav_snapshot: Decimal | None = None

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
            "daily_loss_kill_basis": self.daily_loss_kill_basis,
            "daily_loss_kill_config": format(self.daily_loss_kill_config, "f"),
            "nav_snapshot": (
                None if self.nav_snapshot is None else format(self.nav_snapshot, "f")
            ),
        }

    def operator_notice(
        self, *, lane: str, remaining_orders_note: str | None = None
    ) -> str | None:
        """Human-readable §2-4 notification text, or ``None`` when not tripped.

        ``remaining_orders_note`` overrides the trailing "신규 주문 중단 + 잔여
        주문 취소 완료" clause for a lane whose cancellation story differs from
        the crypto default (e.g. a venue with no pending-order inquiry to
        cancel against) — see ``scripts.b0x.kr.cycle.KILL_CANCEL_UNSUPPORTED_
        NOTE``. Omitting it keeps the original text unchanged.
        """

        if not self.tripped:
            return None
        basis_note = (
            f" (= NAV {format(self.nav_snapshot, 'f')} x "
            f"{format(self.daily_loss_kill_config, 'f')})"
            if self.daily_loss_kill_basis == "pct_of_nav"
            and self.nav_snapshot is not None
            else ""
        )
        note = (
            remaining_orders_note
            if remaining_orders_note is not None
            else "신규 주문 중단 + 잔여 주문 취소 완료. 재개는 운영자 결정 (계약 §2-4)."
        )
        return (
            f"[B0-X KILL SWITCH] lane={lane} — {', '.join(self.kill_reasons)}. "
            f"realized_pnl_today={format(self.realized_pnl_today, 'f')} "
            f"(limit -{format(self.daily_loss_kill, 'f')}{basis_note}). {note}"
        )


def evaluate(*, state: LaneAccountState, envelope: Envelope) -> KillSwitchDecision:
    """Evaluate the §4 kill row plus the informational cap saturation flags.

    ``envelope.daily_loss_kill_basis`` decides how ``envelope.daily_loss_kill``
    is turned into the absolute threshold compared against
    ``state.realized_pnl_today`` — see ``Envelope.daily_loss_kill_basis`` and
    ``KillSwitchDecision.daily_loss_kill`` for why this indirection exists:
    comparing a raw ratio, or an absolute value in the wrong currency, against
    ``realized_pnl_today`` is the currency-unit defect this function exists to
    make structurally impossible rather than merely style-guided against.
    """

    if envelope.daily_loss_kill_basis == "pct_of_nav":
        if state.nav is None:
            raise MissingNavForRatioKill(
                f"envelope for market={envelope.market!r} uses "
                "daily_loss_kill_basis='pct_of_nav' but state.nav is None — "
                "cannot evaluate a NAV-relative kill without a NAV snapshot"
            )
        effective_kill = state.nav * envelope.daily_loss_kill
    else:
        effective_kill = envelope.daily_loss_kill

    kill_reasons: list[str] = []
    # realized_pnl_today is signed; a loss is negative. The kill fires when the
    # *loss* reaches the budget, i.e. pnl <= -budget. Both sides are now
    # guaranteed to be absolute amounts in the same currency.
    if state.realized_pnl_today <= -effective_kill:
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
        daily_loss_kill=effective_kill,
        daily_loss_kill_basis=envelope.daily_loss_kill_basis,
        daily_loss_kill_config=envelope.daily_loss_kill,
        nav_snapshot=state.nav,
    )


__all__ = [
    "KillReason",
    "CapStatus",
    "KillSwitchDecision",
    "MissingNavForRatioKill",
    "evaluate",
]
