"""Experiment state, folded from the ledger events (the only source of truth).

Nothing mutates state except ``State.apply(event)``; commands compute a
payload, append it, then fold it. Re-reading the ledger therefore always
reproduces the state a previous process had, latches included.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from research.rob1391_us_resubmit_shadow.ledger import Event

ARMS = ("C", "T", "T_opt")


def D(value: Any) -> Decimal:
    return Decimal(str(value))


@dataclass(slots=True)
class ArmBook:
    fills: list[dict[str, Any]] = field(default_factory=list)
    unknown: str | None = None

    @property
    def shares(self) -> int:
        return sum(int(f["qty"]) for f in self.fills)

    @property
    def fill_notional(self) -> Decimal:
        return sum((D(f["qty"]) * D(f["price"]) for f in self.fills), Decimal("0"))

    def session_notional(self, session: str) -> Decimal:
        return sum(
            (
                D(f["qty"]) * D(f["price"])
                for f in self.fills
                if f["session"] == session
            ),
            Decimal("0"),
        )

    def sessions(self) -> list[str]:
        return sorted({f["session"] for f in self.fills})


@dataclass(slots=True)
class ParentState:
    rec: dict[str, Any]
    arms: dict[str, ArmBook]
    marks: dict[str, str | None] = field(default_factory=dict)  # session -> close
    session_log: dict[str, dict[str, Any]] = field(default_factory=dict)
    real_followups: list[str] = field(default_factory=list)
    review_block: str | None = None

    @property
    def episode_id(self) -> str:
        return self.rec["episode_id"]

    @property
    def d0(self) -> date:
        return date.fromisoformat(self.rec["d0"])

    @property
    def d20(self) -> date:
        return date.fromisoformat(self.rec["d20"])

    @property
    def last_tracked(self) -> date:
        return date.fromisoformat(self.rec["last_tracked"])

    @property
    def resubmit_sessions(self) -> list[str]:
        return list(self.rec["resubmit_sessions"])

    @property
    def budget(self) -> Decimal:
        return D(self.rec["budget"])

    @property
    def p(self) -> Decimal:
        return D(self.rec["p"])

    def remaining(self, arm: str) -> int:
        return int(self.rec["q"]) - self.arms[arm].shares


@dataclass(slots=True)
class State:
    manifest: dict[str, Any]
    manifest_sha256: str
    latches: list[dict[str, Any]] = field(default_factory=list)
    registered: dict[str, dict[str, Any]] = field(default_factory=dict)
    stepped: dict[str, dict[str, Any]] = field(default_factory=dict)
    parents: dict[str, ParentState] = field(default_factory=dict)
    assignment_order: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- derived
    @property
    def latched(self) -> bool:
        return bool(self.latches)

    @property
    def loss_latched(self) -> bool:
        return any(latch["latch_type"] == "loss" for latch in self.latches)

    @property
    def window(self) -> list[str]:
        return list(self.manifest["window"]["sessions"])

    def latch_types(self) -> set[str]:
        return {latch["latch_type"] for latch in self.latches}

    # ------------------------------------------------------------- folding
    def apply(self, event: Event) -> None:
        p = event.payload
        if event.kind == "sealed":
            if p["manifest_sha256"] != self.manifest_sha256:
                raise ValueError("seal event does not match manifest.sha256")
        elif event.kind == "latch":
            self.latches.append(p)
        elif event.kind == "register":
            session = p["session"]
            if session in self.registered:
                raise ValueError(f"duplicate register event for {session}")
            self.registered[session] = p
            self.latches.extend(p.get("latches", []))
            for rec in p["assigned"]:
                if rec["episode_id"] in self.parents:
                    raise ValueError(f"episode {rec['episode_id']} registered twice")
                arms = {arm: ArmBook() for arm in ARMS}
                if int(rec["filled_d0"]) > 0:
                    real = {
                        "session": rec["d0"],
                        "qty": int(rec["filled_d0"]),
                        "price": rec["avg_fill_d0"],
                        "kind": "real_d0",
                        "at": rec["expiry_at"],
                    }
                    for arm in ARMS:
                        arms[arm].fills.append(dict(real))
                self.parents[rec["episode_id"]] = ParentState(rec=rec, arms=arms)
                self.assignment_order.append(rec["episode_id"])
        elif event.kind == "step":
            session = p["session"]
            if session in self.stepped:
                raise ValueError(f"duplicate step event for {session}")
            self.stepped[session] = p
            self.latches.extend(p.get("latches", []))
            for episode_id, entry in p["parents"].items():
                parent = self.parents[episode_id]
                parent.session_log[session] = entry
                parent.marks[session] = entry.get("close")
                for ref in entry.get("new_real_followups", []):
                    if ref not in parent.real_followups:
                        parent.real_followups.append(ref)
                if entry.get("review_block") and not parent.review_block:
                    parent.review_block = entry["review_block"]
                for arm in ("T", "T_opt"):
                    arm_entry = (entry.get("resubmission") or {}).get(arm)
                    if not arm_entry:
                        continue
                    for fill in arm_entry.get("fills", []):
                        parent.arms[arm].fills.append(
                            {
                                "session": session,
                                "qty": int(fill["qty"]),
                                "price": fill["price"],
                                "kind": "virtual",
                                "at": fill["at"],
                            }
                        )
                    if (
                        arm_entry.get("status") == "unknown"
                        and not parent.arms[arm].unknown
                    ):
                        parent.arms[arm].unknown = (
                            arm_entry.get("unknown_reason") or "unknown"
                        )


def fold(manifest: dict[str, Any], manifest_sha256: str, events: list[Event]) -> State:
    state = State(manifest=manifest, manifest_sha256=manifest_sha256)
    for event in events:
        state.apply(event)
    return state
