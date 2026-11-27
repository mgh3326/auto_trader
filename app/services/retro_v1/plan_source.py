"""``retro_timeline`` plan source: why a published plan row produced no buy.

hk 7005 (design 6939 §6, ticket N15). The daily_plan head/version/row store is
the canonical plan (hk 6751), the v3 intent binds one row to
``(plan_head_id, plan_version, row_key)`` (hk 6991) and the E1 journal records
the child outcome. This module turns those already-stored columns into, per
symbol, the closed no-buy reason of §6:

``candidate_not_captured`` — the symbol is absent from the version's input
bundle candidate set (``input_bundles.body.rows``);
``condition_not_reached`` — an ``executable`` row expired with no E1 child;
``data_delayed_or_missing`` — the 6790 score snapshot marks an input
``unavailable`` (or the row's stored ``not_executable_reason`` is one);
``budget_or_guard_block`` — an E1 refusal from the cap/kill/arming/staleness
guards, or a 6790 ``reserved_waiting``/``skipped_waiting`` allocation;
``unfilled`` — an ``accepted`` child with no in-effect ledger fill at expiry.

Anything the stored evidence does not decide is ``source_unavailable`` and is
never collapsed into one of the five (design §6). Read-only: every read is a
SELECT inside the caller's ``REPEATABLE READ READ ONLY`` snapshot, one SAVEPOINT
group per table, and nothing here imports an order writer, broker or scheduler.

The 6790 allocation comparison is operator-only (design §4.2). It is read here
for the two booleans above and is never copied into the response: a session
cannot recover ranking A/C or the W/N allocation from this module's output.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.daily_plan import bundles, heads, rows, versions
from app.models.execution_intent import ExecutionChildRequest, ExecutionIntent
from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.services.daily_plan.vocabulary import (
    ALLOCATION_METADATA_KEY,
    UNAVAILABLE_REASONS,
)
from app.services.retro_v1.envelope import MAX_SOURCE_ROWS, Unknowns, guarded, make_ref

#: Closed no-buy reasons (design 6939 §6) plus the unknown fallback.
PLAN_REASONS: Final = (
    "candidate_not_captured",
    "condition_not_reached",
    "data_delayed_or_missing",
    "budget_or_guard_block",
    "unfilled",
    "source_unavailable",
)
SOURCE_UNAVAILABLE: Final = "source_unavailable"
#: Rows whose absence is the "why no buy" question. A trim/risk_sell row is a
#: sell and a hold row is not a buy, so none of the five reasons describes them.
BUY_ACTIONS: Final = frozenset({"new", "add", "watch"})
#: 6790 allocation outcomes that mean a budget policy held or passed the row.
WAITING_STATUSES: Final = frozenset({"reserved_waiting", "skipped_waiting"})
#: E1 refusals that are budget or guard blocks (design §6: caps, working-order
#: limit, kill, arming, plan staleness, prior unsettled, no-fill window). The
#: codes are the stored ``execution_child_requests.refusal_reason`` values of
#: the gateway's closed Refusal vocabulary; a code outside this set is not one
#: of the five reasons and stays source_unavailable.
GUARD_REFUSALS: Final = frozenset(
    {
        "quantity_cap_exceeded",
        "notional_cap_exceeded",
        "locked_envelope_cap_exceeded",
        "inventory_constraint_exceeded",
        "sleeve_budget_exceeded",
        "sleeve_loss_limit_reached",
        "max_open_intents_exceeded",
        "submissions_killed",
        "forwarder_unavailable",
        "cooldown_active",
        "plan_version_stale",
        "prior_intent_unsettled",
        "entry_no_fill_window",
    }
)
#: Child states that prove a child was decided against before any send.
REFUSED_STATES: Final = frozenset({"refused", "not_sent"})
#: Child states whose order outcome is not decided by the journal alone.
UNRESOLVED_STATES: Final = frozenset({"sending", "uncertain", "shadowed"})
#: Children that can carry the buy's outcome; a refused cancel is not one.
LINEAGE_ACTIONS: Final = frozenset({"submit", "amend"})


@dataclass(frozen=True, slots=True)
class PlanRow:
    """The daily_plan row in force for one symbol at the latest published seq."""

    seq: int
    published_at: datetime | None
    row_kind: str
    market: str
    symbol: str
    account_ref: str | None
    action: str
    execution_state: str
    body: Mapping[str, Any]
    #: The canonical row key text the v3 intent binds (``plan_row_key``).
    key: str
    ref: str


@dataclass(frozen=True, slots=True)
class PlanEvent:
    at: datetime
    kind: str
    reason: str | None
    ref: str


@dataclass(frozen=True, slots=True)
class PlanEvidence:
    """What the stored plan evidence proves about one symbol."""

    plan_unread: bool = False
    row: PlanRow | None = None
    bundle_known: bool = False
    in_bundle: bool | None = None
    expired: bool | None = None
    input_unknown: bool = False
    allocation_waiting: bool = False
    children_known: bool = True
    guard_refused: bool = False
    child_accepted: bool = False
    child_unresolved: bool = False
    fills_known: bool = True
    filled: bool | None = None


def derive_plan_reason(evidence: PlanEvidence) -> str:
    """The closed reason of design §6, or ``source_unavailable``.

    Precedence follows the evidence: a child outcome is the direct record of
    what happened to the row, so it decides before the plan-level inputs. With
    no child, a budget allocation or an unavailable input explains the row;
    otherwise an expired ``executable`` row is a condition that never fired.
    """
    if evidence.plan_unread:
        return SOURCE_UNAVAILABLE
    if evidence.row is None:
        if evidence.bundle_known and evidence.in_bundle is False:
            return "candidate_not_captured"
        return SOURCE_UNAVAILABLE
    if evidence.row.action not in BUY_ACTIONS:
        return SOURCE_UNAVAILABLE
    if not evidence.children_known:
        return SOURCE_UNAVAILABLE
    if evidence.guard_refused:
        return "budget_or_guard_block"
    if evidence.child_unresolved:
        return SOURCE_UNAVAILABLE
    if evidence.child_accepted:
        if not evidence.fills_known or evidence.filled is None:
            return SOURCE_UNAVAILABLE
        if evidence.filled:
            return SOURCE_UNAVAILABLE
        if evidence.expired is not True:
            return SOURCE_UNAVAILABLE
        return "unfilled"
    if evidence.allocation_waiting:
        return "budget_or_guard_block"
    if evidence.input_unknown:
        return "data_delayed_or_missing"
    if evidence.expired is not True:
        return SOURCE_UNAVAILABLE
    if evidence.row.execution_state == "executable":
        return "condition_not_reached"
    return SOURCE_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class _Child:
    action: str
    state: str
    refusal_reason: str | None
    broker_order_id: str | None


@dataclass
class PlanSource:
    """The plan evidence of one (market, session date), read in one snapshot."""

    head_id: UUID | None = None
    plan_unread: bool = False
    rows: dict[str, PlanRow] = field(default_factory=dict)
    bundle_known: bool = False
    candidates: frozenset[str] = frozenset()
    allocation: Mapping[str, Any] | None = None
    children_known: bool = True
    children: dict[str, list[_Child]] = field(default_factory=dict)
    fills_known: bool = True
    filled_orders: frozenset[str] = frozenset()

    def evidence(self, symbol: str, as_of: datetime) -> PlanEvidence:
        row = self.rows.get(symbol)
        expired = _expired(row, as_of) if row is not None else None
        kids = self.children.get(symbol, [])
        accepted = [
            child
            for child in kids
            if child.action == "submit" and child.state == "accepted"
        ]
        filled: bool | None = None
        if accepted:
            order_ids = {child.broker_order_id for child in accepted}
            if None in order_ids or not self.fills_known:
                filled = None
            else:
                filled = any(order in self.filled_orders for order in order_ids)
        return PlanEvidence(
            plan_unread=self.plan_unread,
            row=row,
            bundle_known=self.bundle_known,
            in_bundle=(symbol in self.candidates) if self.bundle_known else None,
            expired=expired,
            input_unknown=_input_unknown(self.allocation, symbol, row),
            allocation_waiting=_allocation_waiting(self.allocation, symbol),
            children_known=self.children_known,
            guard_refused=any(
                child.action in LINEAGE_ACTIONS
                and child.state in REFUSED_STATES
                and child.refusal_reason in GUARD_REFUSALS
                for child in kids
            ),
            child_accepted=bool(accepted),
            child_unresolved=any(
                child.action in LINEAGE_ACTIONS
                and (
                    child.state in UNRESOLVED_STATES
                    or (
                        child.state in REFUSED_STATES
                        and child.refusal_reason not in GUARD_REFUSALS
                    )
                )
                for child in kids
            ),
            fills_known=self.fills_known,
            filled=filled,
        )

    def events(self, as_of: datetime) -> Iterator[tuple[str, PlanEvent]]:
        """``(symbol, event)`` for every row in force: publish, then expiry."""
        for symbol, row in self.rows.items():
            if row.published_at is not None:
                yield (
                    symbol,
                    PlanEvent(
                        at=row.published_at,
                        kind=(
                            "published_executable"
                            if row.execution_state == "executable"
                            else "published_watch"
                        ),
                        reason=row.action,
                        ref=row.ref,
                    ),
                )
            until = _valid_until(row)
            if until is not None and until <= as_of:
                yield (
                    symbol,
                    PlanEvent(at=until, kind="expired", reason=None, ref=row.ref),
                )


def _row_key(record: Any) -> tuple[str, str, str, str | None]:
    return (record.row_kind, record.market, record.symbol, record.account_ref)


def _key_text(record: Any) -> str:
    return json.dumps(
        [record.row_kind, record.market, record.symbol, record.account_ref],
        separators=(",", ":"),
    )


def _ref(head_id: UUID, seq: int, key: str) -> str:
    return make_ref("plan", f"{head_id}|{seq}|{key}")


def _valid_until(row: PlanRow) -> datetime | None:
    raw = row.body.get("valid_until")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _expired(row: PlanRow, as_of: datetime) -> bool | None:
    until = _valid_until(row)
    if until is None:
        return None
    return until <= as_of


def _allocation_candidate(
    allocation: Mapping[str, Any] | None, symbol: str
) -> Mapping[str, Any] | None:
    if not isinstance(allocation, Mapping):
        return None
    for view in allocation.get("candidates") or []:
        if isinstance(view, Mapping) and view.get("symbol") == symbol:
            return view
    return None


def _input_unknown(
    allocation: Mapping[str, Any] | None, symbol: str, row: PlanRow | None
) -> bool:
    if row is not None and row.body.get("not_executable_reason") in UNAVAILABLE_REASONS:
        return True
    view = _allocation_candidate(allocation, symbol)
    if view is None:
        return False
    inputs = view.get("inputs")
    if not isinstance(inputs, Mapping):
        return False
    return any(
        isinstance(value, Mapping) and value.get("state") == "unknown"
        for value in inputs.values()
    )


def _allocation_waiting(allocation: Mapping[str, Any] | None, symbol: str) -> bool:
    if not isinstance(allocation, Mapping):
        return False
    budget = allocation.get("budget")
    if not isinstance(budget, Mapping):
        return False
    for ranking in budget.values():
        if not isinstance(ranking, Mapping):
            continue
        for policy in ranking.values():
            if not isinstance(policy, Mapping):
                continue
            for item in policy.get("allocations") or []:
                if (
                    isinstance(item, Mapping)
                    and item.get("symbol") == symbol
                    and item.get("status") in WAITING_STATUSES
                ):
                    return True
    return False


def _fold(
    version_rows: list[Any], row_records: list[Any], head_id: UUID
) -> dict[str, PlanRow]:
    """The row in force per row key, with the seq that published it.

    Mirrors ``app.services.daily_plan.store._fold``: a version's adoption replay
    puts an earlier row back in force without inserting a new one.
    """
    by_id = {str(record.id): record for record in row_records}
    by_version: dict[Any, list[Any]] = defaultdict(list)
    for record in row_records:
        by_version[record.version_id].append(record)
    folded: dict[
        tuple[str, str, str, str | None], tuple[int, datetime | None, Any]
    ] = {}
    for version in version_rows:
        for record in by_version.get(version.id, ()):
            folded[_row_key(record)] = (version.seq, version.created_at, record)
        replay_ids = (version.metadata or {}).get("adoption_replay_rows", [])
        for replay_id in replay_ids:
            record = by_id.get(str(replay_id))
            if record is not None:
                folded[_row_key(record)] = (version.seq, version.created_at, record)
    by_symbol: dict[str, list[tuple[bool, int, datetime | None, Any]]] = defaultdict(
        list
    )
    for seq, published_at, record in folded.values():
        by_symbol[record.symbol].append(
            (record.row_kind == "candidate", seq, published_at, record)
        )
    chosen: dict[str, PlanRow] = {}
    for symbol, items in by_symbol.items():
        _candidate, seq, published_at, record = max(items, key=lambda item: item[:2])
        key = _key_text(record)
        chosen[symbol] = PlanRow(
            seq=seq,
            published_at=published_at,
            row_kind=record.row_kind,
            market=record.market,
            symbol=record.symbol,
            account_ref=record.account_ref,
            action=record.action,
            execution_state=record.execution_state,
            body=record.body or {},
            key=key,
            ref=_ref(head_id, seq, key),
        )
    return chosen


async def read_plan_source(
    session: AsyncSession,
    unknowns: Unknowns,
    *,
    market: str,
    day: date,
    symbols: list[str] | None,
    as_of: datetime,
) -> PlanSource:
    """Read the plan evidence of one (market, date) inside the caller's snapshot.

    ``as_of`` decides expiry only; it never writes. A read that fails or is cut
    off leaves the affected evidence unknown (``source_unavailable``), never a
    guessed reason.
    """

    async def read_head() -> Any:
        stmt = select(heads.c.id).where(
            heads.c.market == market, heads.c.session_date == day
        )
        return (await session.execute(stmt)).first()

    head, ok = await guarded(session, unknowns, "source.plan", read_head, None)
    if not ok:
        return PlanSource(plan_unread=True)
    if head is None:
        return PlanSource()
    head_id = head.id
    source = PlanSource(head_id=head_id)

    async def read_versions() -> list[Any]:
        stmt = (
            select(
                versions.c.id,
                versions.c.seq,
                versions.c.created_at,
                versions.c.metadata,
                versions.c.input_bundle_id,
            )
            .where(
                versions.c.head_id == head_id,
                versions.c.status == "PUBLISHED",
            )
            .order_by(versions.c.seq)
            .limit(MAX_SOURCE_ROWS)
        )
        return list((await session.execute(stmt)).all())

    version_rows, ok = await guarded(
        session, unknowns, "source.plan", read_versions, []
    )
    if not ok:
        source.plan_unread = True
        return source
    if len(version_rows) >= MAX_SOURCE_ROWS:
        unknowns.note("source.plan", "incomplete_evidence")
        source.plan_unread = True
        return source
    if not version_rows:
        return source
    version_ids = [version.id for version in version_rows]

    async def read_rows() -> list[Any]:
        stmt = (
            select(
                rows.c.id,
                rows.c.version_id,
                rows.c.row_kind,
                rows.c.market,
                rows.c.symbol,
                rows.c.account_ref,
                rows.c.action,
                rows.c.execution_state,
                rows.c.body,
            )
            .where(rows.c.version_id.in_(version_ids), rows.c.market == market)
            .order_by(rows.c.id)
            .limit(MAX_SOURCE_ROWS)
        )
        return list((await session.execute(stmt)).all())

    row_records, ok = await guarded(session, unknowns, "source.plan", read_rows, [])
    if not ok or len(row_records) >= MAX_SOURCE_ROWS:
        if ok:
            unknowns.note("source.plan", "incomplete_evidence")
        source.plan_unread = True
        return source
    source.rows = _fold(version_rows, row_records, head_id)

    latest = version_rows[-1]
    if latest.input_bundle_id is not None:

        async def read_bundle() -> Any:
            stmt = select(bundles.c.body).where(
                bundles.c.id == latest.input_bundle_id,
                bundles.c.head_id == head_id,
            )
            return (await session.execute(stmt)).first()

        bundle, ok = await guarded(
            session, unknowns, "source.plan_bundle", read_bundle, None
        )
        if ok and bundle is not None:
            body = bundle.body if isinstance(bundle.body, Mapping) else {}
            bundle_rows = body.get("rows")
            source.candidates = frozenset(
                str(item["symbol"])
                for item in (bundle_rows if isinstance(bundle_rows, list) else [])
                if isinstance(item, Mapping)
                and item.get("row_kind") == "candidate"
                and item.get("market") == market
                and isinstance(item.get("symbol"), str)
            )
            source.bundle_known = True

    metadata = latest.metadata if isinstance(latest.metadata, Mapping) else {}
    allocation = metadata.get(ALLOCATION_METADATA_KEY)
    source.allocation = allocation if isinstance(allocation, Mapping) else None

    async def read_intents() -> list[Any]:
        stmt = select(
            ExecutionIntent.id,
            ExecutionIntent.symbol,
            ExecutionIntent.plan_row_key,
        ).where(
            ExecutionIntent.schema_version == 3,
            ExecutionIntent.plan_head_id == head_id,
        )
        if symbols:
            stmt = stmt.where(func.upper(ExecutionIntent.symbol).in_(symbols))
        stmt = stmt.limit(MAX_SOURCE_ROWS)
        return list((await session.execute(stmt)).all())

    intents, ok = await guarded(
        session, unknowns, "source.plan_intents", read_intents, []
    )
    if not ok or len(intents) >= MAX_SOURCE_ROWS:
        if ok:
            unknowns.note("source.plan_intents", "incomplete_evidence")
        source.children_known = False
        return source
    symbols_by_intent: dict[Any, str] = {}
    for intent in intents:
        row = source.rows.get(intent.symbol)
        # A v3 intent binds one row key, not just a symbol: a trim (holding)
        # intent must never decide the candidate buy row's reason.
        if row is None or intent.plan_row_key != row.key:
            continue
        symbols_by_intent[intent.id] = intent.symbol
    if not symbols_by_intent:
        return source

    async def read_children() -> list[Any]:
        stmt = (
            select(
                ExecutionChildRequest.intent_id,
                ExecutionChildRequest.action,
                ExecutionChildRequest.state,
                ExecutionChildRequest.refusal_reason,
                ExecutionChildRequest.broker_order_id,
            )
            .where(ExecutionChildRequest.intent_id.in_(list(symbols_by_intent)))
            .order_by(ExecutionChildRequest.id)
            .limit(MAX_SOURCE_ROWS)
        )
        return list((await session.execute(stmt)).all())

    child_rows, ok = await guarded(
        session, unknowns, "source.plan_intents", read_children, []
    )
    if not ok or len(child_rows) >= MAX_SOURCE_ROWS:
        if ok:
            unknowns.note("source.plan_intents", "incomplete_evidence")
        source.children_known = False
        return source
    for child in child_rows:
        source.children.setdefault(symbols_by_intent[child.intent_id], []).append(
            _Child(
                action=child.action,
                state=child.state,
                refusal_reason=child.refusal_reason,
                broker_order_id=child.broker_order_id,
            )
        )

    order_ids = sorted(
        {
            child.broker_order_id
            for kids in source.children.values()
            for child in kids
            if child.action == "submit"
            and child.state == "accepted"
            and child.broker_order_id is not None
        }
    )
    if not order_ids:
        return source

    async def read_fills() -> list[Any]:
        stmt = (
            select(ExecutionLedger.broker_order_id.label("plan_order_id"))
            .where(
                ExecutionLedger.broker_order_id.in_(order_ids),
                execution_ledger_in_effect(),
            )
            .distinct()
        )
        return list((await session.execute(stmt)).scalars().all())

    filled, ok = await guarded(session, unknowns, "source.plan_fills", read_fills, [])
    if not ok:
        source.fills_known = False
        return source
    source.filled_orders = frozenset(str(order) for order in filled)
    return source
