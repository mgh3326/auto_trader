"""Decision links for the retro/v1 readers (hk 4188, design #4154 §3).

Reads the append-only decision tables written by L1/L3/L4 and resolves them with
the design's rules. Everything here is SELECT only; the pure functions take
already loaded rows so they are testable with in-memory objects.

``resolve_attempts`` is the "전송 시도 해소 규칙" of §3 row 4: per send key, the
attempts without a ``proven_not_sent`` outcome decide the link. The tables have
no ORM model (their only writer uses SQL text), so the reader describes the
columns it reads as lightweight Core tables.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import Text, column, select, table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.retro_v1.envelope import (
    KST,
    MAX_SOURCE_ROWS,
    Unknowns,
    guarded,
    make_ref,
)

DECISION_LINKS: Final = table(
    "decision_order_links",
    column("attempt_id", PG_UUID(as_uuid=True)),
    column("decision_id", PG_UUID(as_uuid=True)),
    column("account_scope", Text),
    column("send_key", Text),
    column("proposal_id", PG_UUID(as_uuid=True)),
    column("rung_index"),
    schema="review",
)
LINK_OUTCOMES: Final = table(
    "decision_order_link_outcomes",
    column("attempt_id", PG_UUID(as_uuid=True)),
    column("outcome", Text),
    schema="review",
)
DECISION_EVIDENCE: Final = table(
    "decision_evidence",
    column("decision_id", PG_UUID(as_uuid=True)),
    column("candidate_call_id", PG_UUID(as_uuid=True)),
    column("candidate_symbol", Text),
    column("candidate_link_basis", Text),
    schema="review",
)

#: How a link was made. ``recorded`` = an exact key a writer stored; ``inference``
#: = a heuristic, or a recorded key without proof the order was sent.
LinkBasis = Literal["recorded", "inference"]
LINK_BASES: Final = frozenset({"recorded", "inference"})

#: The decision-link scope of an order ledger row, by its broker.
SCOPE_BY_BROKER: Final = {
    "kis": "kis_live",
    "toss": "toss_live",
    "upbit": "upbit_live",
}
_LINK_ROWS_CAP: Final = MAX_SOURCE_ROWS * 4


def decision_ref(decision_id: object) -> str:
    return make_ref("decision", decision_id)


def kst_day(moment: datetime) -> Any:
    """KST calendar day of an instant (the "KST trading day" of design Q20)."""
    return moment.astimezone(KST).date()


def order_send_key(order: Any) -> str | None:
    key = getattr(order, "idempotency_key", None) or getattr(
        order, "client_order_id", None
    )
    return key or None


def order_scope(order: Any) -> str | None:
    return SCOPE_BY_BROKER.get(getattr(order, "broker", None))


@dataclass(frozen=True, slots=True)
class AttemptResolution:
    """What the attempts of one send key say about an order.

    ``none``: no live attempt (the legacy rule applies). ``ambiguous``: live
    attempts belong to two or more decisions. ``recorded``: one decision and a
    ``sent`` outcome. ``unconfirmed``: one decision, no ``sent`` evidence.
    ``unavailable``: the attempt read failed or was cut off, so nothing — not
    even the legacy rung match — may be concluded.
    """

    kind: Literal["none", "ambiguous", "recorded", "unconfirmed", "unavailable"]
    decision_id: UUID | None = None
    proposal_id: UUID | None = None
    rung_index: int | None = None


def resolve_attempts(
    attempts: Sequence[Any], outcomes: Mapping[Any, str]
) -> AttemptResolution:
    """§3 row 4: decisions of attempts that are not ``proven_not_sent``.

    A lost ``proven_not_sent`` row can only add a live attempt, so it can turn a
    ``recorded`` link into ``ambiguous``/``unconfirmed``, never the reverse: the
    rule fails toward "no link", not toward a wrong one.
    """
    live = [a for a in attempts if outcomes.get(a.attempt_id) != "proven_not_sent"]
    if not live:
        return AttemptResolution("none")
    decisions = {a.decision_id for a in live}
    if len(decisions) > 1:
        return AttemptResolution("ambiguous")
    sent = [a for a in live if outcomes.get(a.attempt_id) == "sent"]
    bindings = {(a.proposal_id, a.rung_index) for a in (sent or live)}
    proposal_id, rung_index = bindings.pop() if len(bindings) == 1 else (None, None)
    return AttemptResolution(
        "recorded" if sent else "unconfirmed",
        decisions.pop(),
        proposal_id,
        rung_index,
    )


@dataclass(slots=True)
class OrderLinkEvidence:
    """Attempt rows by ``(account_scope, send_key)`` and outcome by attempt.

    ``complete`` is false when the attempt read failed or hit its cap: an
    omitted attempt could belong to another decision, so no order resolves.
    """

    attempts: dict[tuple[str, str], list[Any]]
    outcomes: dict[Any, str]
    complete: bool = True

    @classmethod
    def empty(cls) -> OrderLinkEvidence:
        return cls({}, {})

    def resolve(self, order: Any) -> AttemptResolution:
        scope, key = order_scope(order), order_send_key(order)
        if scope is None or key is None:
            return AttemptResolution("none")
        if not self.complete:
            return AttemptResolution("unavailable")
        return resolve_attempts(self.attempts.get((scope, key), []), self.outcomes)


async def load_order_links(
    session: AsyncSession, unknowns: Unknowns, send_keys: Sequence[str]
) -> OrderLinkEvidence:
    """Attempt and outcome rows of the given send keys.

    A failed read is an unknown. With no outcome rows every attempt counts as
    live, which can only withhold ``recorded``.
    """
    keys = sorted(set(send_keys) - {""})
    evidence = OrderLinkEvidence.empty()
    if not keys:
        return evidence

    async def read_attempts() -> list[Any]:
        stmt = (
            select(
                DECISION_LINKS.c.attempt_id,
                DECISION_LINKS.c.decision_id,
                DECISION_LINKS.c.account_scope,
                DECISION_LINKS.c.send_key,
                DECISION_LINKS.c.proposal_id,
                DECISION_LINKS.c.rung_index,
            )
            .where(DECISION_LINKS.c.send_key.in_(keys))
            .order_by(DECISION_LINKS.c.attempt_id)
            .limit(_LINK_ROWS_CAP)
        )
        return list((await session.execute(stmt)).all())

    attempts, ok = await guarded(
        session, unknowns, "source.decision_links", read_attempts, []
    )
    if ok and len(attempts) >= _LINK_ROWS_CAP:
        ok = False
        unknowns.note("source.decision_links", "incomplete_evidence")
    evidence.complete = ok
    wanted = set(keys)
    by_key: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for attempt in attempts:
        if attempt.send_key in wanted:
            by_key[(attempt.account_scope, attempt.send_key)].append(attempt)
    evidence.attempts = dict(by_key)
    if not attempts:
        return evidence

    ids = sorted({a.attempt_id for a in attempts}, key=str)

    async def read_outcomes() -> list[Any]:
        stmt = (
            select(LINK_OUTCOMES.c.attempt_id, LINK_OUTCOMES.c.outcome)
            .where(LINK_OUTCOMES.c.attempt_id.in_(ids))
            .limit(_LINK_ROWS_CAP)
        )
        return list((await session.execute(stmt)).all())

    outcomes, ok = await guarded(
        session, unknowns, "source.decision_link_outcomes", read_outcomes, []
    )
    if ok and len(outcomes) >= _LINK_ROWS_CAP:
        ok = False
        unknowns.note("source.decision_link_outcomes", "incomplete_evidence")
    # Without the outcomes a hidden proven_not_sent could turn a live attempt
    # into a wrong positive attribution, so the evidence is unusable.
    evidence.complete = evidence.complete and ok
    known = set(ids)
    evidence.outcomes = {
        o.attempt_id: o.outcome for o in outcomes if o.attempt_id in known
    }
    return evidence


async def load_decisions(
    session: AsyncSession, unknowns: Unknowns, decision_ids: Sequence[Any]
) -> dict[Any, Any]:
    """Decision rows (candidate reference columns) by ``decision_id``."""
    ids = sorted(set(decision_ids) - {None}, key=str)
    if not ids:
        return {}

    async def read() -> list[Any]:
        stmt = (
            select(
                DECISION_EVIDENCE.c.decision_id,
                DECISION_EVIDENCE.c.candidate_call_id,
                DECISION_EVIDENCE.c.candidate_symbol,
                DECISION_EVIDENCE.c.candidate_link_basis,
            )
            .where(DECISION_EVIDENCE.c.decision_id.in_(ids))
            .limit(MAX_SOURCE_ROWS)
        )
        return list((await session.execute(stmt)).all())

    rows, ok = await guarded(session, unknowns, "source.decisions", read, [])
    if ok and len(rows) >= MAX_SOURCE_ROWS:
        unknowns.note("source.decisions", "incomplete_evidence")
    wanted = set(ids)
    return {r.decision_id: r for r in rows if r.decision_id in wanted}
