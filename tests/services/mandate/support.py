"""Builders for the mandate (hk 3795 T1) tests. No database."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from app.services.mandate.fold import PlanEvent, PlanState, apply_event

D = Decimal
SESSION = date(2026, 11, 23)


def ev(event: str, **fields) -> PlanEvent:
    return PlanEvent(event=event, actor="op", **fields)


class Stream:
    """Applies events one by one, as the store does (seq is assigned)."""

    def __init__(self) -> None:
        self.state = PlanState()
        self.proposals: dict[int, UUID] = {}

    def add(self, event: str, **fields) -> PlanState:
        self.state = apply_event(self.state, ev(event, **fields))
        return self.state

    def approve(self) -> None:
        self.add("proposed")
        self.add(
            "approved", terms_id=1, terms_sha256="a" * 64, effective_session=SESSION
        )

    def arm_and_propose(self, k: int) -> UUID:
        pid = uuid4()
        self.add("tranche_armed", tranche_index=k)
        self.add("tranche_proposed", tranche_index=k, proposal_id=pid)
        self.proposals[k] = pid
        return pid

    def fence(self, k: int, pid: UUID, *, mode: str = "human", day: date = SESSION):
        attempt = uuid4()
        kwargs = {
            "tranche_index": k,
            "proposal_id": pid,
            "notional": D("940000"),
            "kst_day": day,
            "approval_mode": mode,
            "account_ref": "acct-1",
            "attempt_id": attempt,
        }
        if mode == "auto":
            kwargs["activation_profile_id"] = 7
        self.add("tranche_send_fenced", **kwargs)
        return self.state.tranche(k).unclosed_fences[-1]

    def fill_first(self, amount: str = "300000", qty: str = "3", *, done: bool = True):
        pid = self.arm_and_propose(1)
        if done:
            self.add(
                "tranche_filled",
                tranche_index=1,
                proposal_id=pid,
                fill_amount=D(amount),
                fill_quantity=D(qty),
                first_fill_amount=D(amount),
                first_fill_quantity=D(qty),
                evidence={"trade_date": "2026-11-23"},
            )
        else:
            self.add(
                "tranche_partially_filled",
                tranche_index=1,
                proposal_id=pid,
                fill_amount=D(amount),
                fill_quantity=D(qty),
                evidence={"trade_date": "2026-11-23"},
            )
        return pid
