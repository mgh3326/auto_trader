"""ROB-1290 — what a re-judgement session may hand back, and nothing else.

The closed union
----------------
A session spawned for one watch fire produces exactly one of two things:

:class:`ProposalDraft`
    "Propose this." Carries everything ``order_proposal_create`` needs, and
    is turned into a real proposal row by :mod:`.proposal_chain`.
:class:`Decline`
    "Do not propose, and here is why -- for *this* event." The reason is
    required and non-blank.

There is no third member, and that is the point. §101차 ⑤ forbids an
analysis-only outcome, so the type that models a session's answer must not
be able to express one. :func:`interpret_judgement` is the funnel every
answer passes through: anything that is not one of these two -- ``None``, a
report, a bare string, a future variant somebody adds without thinking --
is :class:`JudgementRefused`, which the spawner turns into "this fire is
not finished" rather than into a comfortable success.

Why the judge lives outside this repo
-------------------------------------
The judgement itself is LLM work, and this runtime owns no in-process LLM
provider (the ROB-501 boundary). So :class:`RepricingJudge` is a port, the
package ships no implementation of it, and the spawner refuses to be built
without one. What ships here is the part that must be deterministic: the
shape of the answer, and the wiring from that answer to a durable row.

The judge decides; it never writes
----------------------------------
A judge is handed a :class:`~.spawn.SpawnRequest` and returns a value. It
is given no capability object, and :mod:`.proposal_chain` is the sole
writer -- which is what lets a judge failure be classified as "provably
nothing was written", and therefore as safely retryable rather than as an
ambiguity needing an operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "Decline",
    "Judgement",
    "JudgementRefused",
    "ProposalDraft",
    "ProposalRung",
    "RepricingJudge",
    "interpret_judgement",
]

_SIDES = frozenset({"buy", "sell"})


class JudgementRefused(ValueError):
    """A session handed back something that is not a judgement.

    Deliberately an error rather than a state: the completion criterion
    counts fires that produced neither a proposal nor an attributed reason
    as failures, so the type system must not offer a way to record one.
    """


@dataclass(frozen=True)
class ProposalRung:
    """One ladder step of a draft, in the shape the boundary tool takes."""

    rung_index: int
    side: str
    quantity: str
    limit_price: str | None = None
    notional: str | None = None

    def __post_init__(self) -> None:
        if self.rung_index < 0:
            raise JudgementRefused(f"rung_index must be >= 0, got {self.rung_index}")
        if self.side not in _SIDES:
            raise JudgementRefused(f"rung side must be one of {sorted(_SIDES)}")
        if not str(self.quantity).strip():
            raise JudgementRefused("rung quantity must be non-blank")

    def as_tool_argument(self) -> dict[str, object]:
        return {
            "rung_index": self.rung_index,
            "side": self.side,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "notional": self.notional,
        }


@dataclass(frozen=True)
class ProposalDraft:
    """A session's decision to propose, validated before it reaches the tool.

    ``event_uuid`` is carried so the draft is bound to the fire it answers.
    A judge that returns a draft for a different event is refused in
    :func:`interpret_judgement` rather than silently proposing against
    whatever fire happened to be in flight.
    """

    event_uuid: str
    symbol: str
    market: str
    account_mode: str
    side: str
    order_type: str
    rungs: tuple[ProposalRung, ...]
    thesis: str
    strategy: str = "watch-trigger-repricing"
    valid_until: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "event_uuid",
            "symbol",
            "market",
            "account_mode",
            "order_type",
            "thesis",
            "strategy",
        ):
            if not str(getattr(self, name) or "").strip():
                raise JudgementRefused(f"ProposalDraft.{name} must be non-blank")
        if self.side not in _SIDES:
            raise JudgementRefused(f"side must be one of {sorted(_SIDES)}")
        if not self.rungs:
            raise JudgementRefused(
                "a draft with no rungs proposes nothing; return a Decline instead"
            )
        mismatched = [r.rung_index for r in self.rungs if r.side != self.side]
        if mismatched:
            raise JudgementRefused(
                f"rungs {mismatched} disagree with the draft side {self.side!r}"
            )


@dataclass(frozen=True)
class Decline:
    """A session's decision not to propose, with the reason for this fire."""

    event_uuid: str
    reason: str

    def __post_init__(self) -> None:
        if not str(self.event_uuid or "").strip():
            raise JudgementRefused("Decline.event_uuid must be non-blank")
        if not str(self.reason or "").strip():
            raise JudgementRefused(
                "a Decline without a reason is an analysis-only outcome by "
                "another name; §101차 ⑤ forbids one"
            )


Judgement = ProposalDraft | Decline


@runtime_checkable
class RepricingJudge(Protocol):
    """The out-of-process judgement seam. May be sync or async."""

    def judge(self, request: object) -> object: ...


def interpret_judgement(value: object, *, event_uuid: str) -> Judgement:
    """Funnel a session's answer into the closed union, or refuse it.

    Every path from a spawned session to a terminal passes through here, so
    a new "outcome" cannot be introduced by returning a novel object: it
    would land in the final ``raise`` instead of quietly becoming a third
    kind of success.
    """
    if isinstance(value, ProposalDraft | Decline):
        if value.event_uuid != event_uuid:
            raise JudgementRefused(
                f"judgement is bound to event {value.event_uuid!r} but the "
                f"session was spawned for {event_uuid!r}"
            )
        return value
    raise JudgementRefused(
        f"a re-judgement session must return a ProposalDraft or a Decline for "
        f"event {event_uuid}; got {type(value).__name__}. There is no "
        "analysis-only judgement -- a session that looked and said nothing "
        "has not finished."
    )
