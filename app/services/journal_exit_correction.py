"""hk 5357 — one-off correction of exit fields on lots closed by a late reconcile.

Before hk 5332 (PR #2468) a reconcile that booked a sell stamped the closed lot
journal with the booking time as ``exit_date`` and left ``total_pnl_krw`` empty.
New bookings are right; this module repairs older rows by the same rules:
``exit_date`` is the ``filled_at`` of the in-effect authoritative sell ledger row
that closed the lot, and ``total_pnl_krw`` is
``(fill price - entry_price) * allocated quantity`` (KRW equities only).

A closed lot journal keeps no durable link to the sell that closed it, and the
FIFO closer lets one booked sell close several lots, even a lot whose journal was
created after the fill. A match by quantity, price or time therefore proves
nothing, and none is attempted. The operator supplies one evidence entry per
journal that cites a retained close record (for example the archived reconcile
result listing ``closed_journal_ids``) and names the sell by its full ledger
identity ``(broker, account_mode, venue, broker_order_id, fill_seq)`` with the
quantity allocated to that journal. This module checks the entry against the
database and refuses the whole batch if any link is missing or inconsistent. It
cannot authenticate the cited record; the record reference is bound into the
preview digest and written to the journal audit note for review. Nothing here
sends orders, reads a broker or writes the ledger.

The layers are split so the rules are testable without a database:

* ``parse_evidence`` — strict input parsing, before any connection.
* ``plan_journal`` / ``plan_batch`` — pure; compare a journal, its evidence and
  the ledger row the evidence names.
* ``load_order_fills`` — the only ledger reader, ANDs ``execution_ledger_in_effect``.
* ``preview`` / ``commit`` — session orchestration; ``commit`` holds off ledger
  writers, locks the journal rows, recomputes the plan and writes only if it
  still equals the previewed one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType
from app.services.execution_ledger.supersede import AUTHORITATIVE_SOURCES

DECISION_REF = "hk 5357"
NOTE_TOKEN = "hk5357"
MAX_IDS = 20
MAX_REASON_CHARS = 500
MAX_ACTOR_CHARS = 100
#: ``commit`` waits at most this long for ledger writers before giving up.
LOCK_TIMEOUT_MS = 5000
#: journal ``account`` -> ledger ``broker``
BROKER_BY_ACCOUNT = {"kis": "kis", "toss": "toss"}
LEDGER_TABLE = ExecutionLedger.__table__.fullname  # schema-qualified, never user input

_MONEY = Decimal("0.0001")
_ID = re.compile(r"[1-9][0-9]*")
_REF = re.compile(r"[^\s\x00-\x1f\x7f]{1,200}")
MAX_EVIDENCE_BYTES = 64 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

Status = Literal["eligible", "already_corrected", "ineligible"]
BatchStatus = Literal["eligible", "noop", "refused", "committed", "plan_changed"]


class CorrectionInputError(ValueError):
    """Bad CLI input; raised before any database access."""


class LedgerRowLike(Protocol):
    id: int
    broker: str
    account_mode: str
    venue: str
    broker_order_id: str
    fill_seq: int
    instrument_type: Any
    symbol: str
    side: str
    currency: str
    source: str
    quarantined_at: datetime | None
    filled_qty: Decimal
    filled_price: Decimal
    filled_at: datetime
    filled_at_basis: str | None


# --------------------------------------------------------------------- inputs


def parse_ids(raw: str | None) -> list[int]:
    """Exact comma-separated positive integers; no ranges, patterns or repeats."""
    if raw is None or not raw.strip():
        raise CorrectionInputError("--ids is required")
    parts = raw.split(",")
    if any(not _ID.fullmatch(part) for part in parts):
        raise CorrectionInputError(
            "--ids must be exact comma-separated positive integers"
        )
    ids = [int(part) for part in parts]
    if len(set(ids)) != len(ids):
        raise CorrectionInputError("--ids must not repeat an id")
    if len(ids) > MAX_IDS:
        raise CorrectionInputError(f"--ids accepts at most {MAX_IDS} ids")
    return ids


def validate_text(name: str, value: str | None, *, max_chars: int) -> str:
    if value is None or not value.strip():
        raise CorrectionInputError(f"--{name} is required")
    cleaned = value.strip()
    if len(cleaned) > max_chars:
        raise CorrectionInputError(f"--{name} is longer than {max_chars} characters")
    if _CONTROL.search(cleaned):
        raise CorrectionInputError(f"--{name} must be a single line without controls")
    return cleaned


# ------------------------------------------------------------------- evidence


@dataclass(frozen=True)
class Evidence:
    """What the operator cites to link one closed journal to its closing sell."""

    journal_id: int
    evidence_ref: str
    record_order_id: str
    record_closed_journal_ids: tuple[int, ...]
    venue: str
    broker_order_id: str
    fill_seq: int
    allocated_quantity: Decimal
    #: Execution time proven by a retained record, for a ledger row whose own
    #: ``filled_at`` is not a broker fill time (KIS reconciler rows carry the
    #: order time). ``verified_fill_ref`` names that record.
    verified_fill_at: datetime | None = None
    verified_fill_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "journal_id": self.journal_id,
            "evidence_ref": self.evidence_ref,
            "record_order_id": self.record_order_id,
            "record_closed_journal_ids": list(self.record_closed_journal_ids),
            "ledger": {
                "venue": self.venue,
                "broker_order_id": self.broker_order_id,
                "fill_seq": self.fill_seq,
            },
            "allocated_quantity": format(self.allocated_quantity, "f"),
            "verified_fill_time": None
            if self.verified_fill_at is None
            else {
                "at": self.verified_fill_at.isoformat(),
                "ref": self.verified_fill_ref,
            },
        }


_ENTRY_KEYS = {
    "journal_id",
    "evidence_ref",
    "record_order_id",
    "record_closed_journal_ids",
    "ledger",
    "allocated_quantity",
}
_OPTIONAL_ENTRY_KEYS = {"verified_fill_time"}
_LEDGER_KEYS = {"venue", "broker_order_id", "fill_seq"}
_VERIFIED_KEYS = {"at", "ref"}


def _text(name: str, value: Any, *, pattern: re.Pattern[str] = _REF) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CorrectionInputError(f"evidence {name} must be a single token")
    return value


def _int(name: str, value: Any, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise CorrectionInputError(f"evidence {name} must be an integer >= {minimum}")
    return value


def parse_evidence(raw: str, ids: Sequence[int]) -> dict[int, Evidence]:
    """Strictly parse the evidence file: exactly one entry per requested journal."""
    if len(raw.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise CorrectionInputError("evidence file is too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise CorrectionInputError("evidence file is not valid JSON") from None
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        raise CorrectionInputError("evidence must be a JSON list of objects")
    out: dict[int, Evidence] = {}
    for entry in data:
        if not _ENTRY_KEYS <= set(entry) <= _ENTRY_KEYS | _OPTIONAL_ENTRY_KEYS:
            raise CorrectionInputError(
                "evidence entry keys are not exactly the contract"
            )
        ledger = entry["ledger"]
        if not isinstance(ledger, dict) or set(ledger) != _LEDGER_KEYS:
            raise CorrectionInputError(
                "evidence ledger keys are not exactly the contract"
            )
        closed = entry["record_closed_journal_ids"]
        if not isinstance(closed, list) or not closed:
            raise CorrectionInputError("evidence record_closed_journal_ids is empty")
        quantity = entry["allocated_quantity"]
        if not isinstance(quantity, str) or not re.fullmatch(
            r"[0-9]+(\.[0-9]+)?", quantity
        ):
            raise CorrectionInputError(
                "evidence allocated_quantity must be a decimal string"
            )
        verified_at: datetime | None = None
        verified_ref: str | None = None
        verified = entry.get("verified_fill_time")
        if "verified_fill_time" in entry:
            if not isinstance(verified, dict) or set(verified) != _VERIFIED_KEYS:
                raise CorrectionInputError(
                    "evidence verified_fill_time keys are not exactly the contract"
                )
            verified_ref = _text("verified_fill_time.ref", verified["ref"])
            try:
                verified_at = datetime.fromisoformat(str(verified["at"]))
            except ValueError:
                raise CorrectionInputError(
                    "evidence verified_fill_time.at is not an ISO timestamp"
                ) from None
            if not isinstance(verified["at"], str) or verified_at.utcoffset() is None:
                raise CorrectionInputError(
                    "evidence verified_fill_time.at needs a UTC offset"
                )
        item = Evidence(
            journal_id=_int("journal_id", entry["journal_id"], minimum=1),
            evidence_ref=_text("evidence_ref", entry["evidence_ref"]),
            record_order_id=_text("record_order_id", entry["record_order_id"]),
            record_closed_journal_ids=tuple(
                _int("record_closed_journal_ids item", v, minimum=1) for v in closed
            ),
            venue=_text("ledger.venue", ledger["venue"]),
            broker_order_id=_text("ledger.broker_order_id", ledger["broker_order_id"]),
            fill_seq=_int("ledger.fill_seq", ledger["fill_seq"], minimum=0),
            allocated_quantity=Decimal(quantity),
            verified_fill_at=verified_at,
            verified_fill_ref=verified_ref,
        )
        if item.allocated_quantity <= 0:
            raise CorrectionInputError("evidence allocated_quantity must be positive")
        if item.journal_id in out:
            raise CorrectionInputError("evidence repeats a journal id")
        out[item.journal_id] = item
    if set(out) != set(ids):
        raise CorrectionInputError(
            "evidence must have exactly one entry per --ids journal"
        )
    return out


# ----------------------------------------------------------------------- plan


@dataclass(frozen=True)
class JournalPlan:
    journal_id: int
    status: Status
    reason: str | None = None
    symbol: str | None = None
    account: str | None = None
    quantity: Decimal | None = None
    entry_price: Decimal | None = None
    current_exit_date: datetime | None = None
    current_total_pnl_krw: Decimal | None = None
    new_exit_date: datetime | None = None
    new_total_pnl_krw: Decimal | None = None
    ledger_id: int | None = None
    ledger_source: str | None = None
    ledger_filled_qty: Decimal | None = None
    ledger_filled_price: Decimal | None = None
    ledger_filled_at_basis: str | None = None
    fill_time_source: str | None = None
    evidence: Evidence | None = None
    notes_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        def money(value: Decimal | None) -> str | None:
            return None if value is None else format(value, "f")

        def when(value: datetime | None) -> str | None:
            return None if value is None else value.isoformat()

        return {
            "journal_id": self.journal_id,
            "status": self.status,
            "reason": self.reason,
            "symbol": self.symbol,
            "account": self.account,
            "quantity": money(self.quantity),
            "entry_price": money(self.entry_price),
            "current": {
                "exit_date": when(self.current_exit_date),
                "total_pnl_krw": money(self.current_total_pnl_krw),
            },
            "new": {
                "exit_date": when(self.new_exit_date),
                "total_pnl_krw": money(self.new_total_pnl_krw),
            },
            "ledger_id": self.ledger_id,
            "ledger_source": self.ledger_source,
            "ledger_filled_qty": money(self.ledger_filled_qty),
            "ledger_filled_price": money(self.ledger_filled_price),
            "ledger_filled_at_basis": self.ledger_filled_at_basis,
            "fill_time_source": self.fill_time_source,
            "evidence": None if self.evidence is None else self.evidence.as_dict(),
        }


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(_MONEY, rounding=ROUND_HALF_UP)


def _ineligible(
    journal_id: int, reason: str, journal: Any | None = None
) -> JournalPlan:
    return JournalPlan(
        journal_id=journal_id,
        status="ineligible",
        reason=reason,
        symbol=getattr(journal, "symbol", None),
        account=getattr(journal, "account", None),
    )


def _marker(ledger_id: int, evidence_ref: str) -> str:
    return f"exit_correction:{NOTE_TOKEN} ledger_id={ledger_id} evidence={evidence_ref}"


def _sha(text_value: str | None) -> str:
    return hashlib.sha256((text_value or "").encode("utf-8")).hexdigest()


def ledger_identity(
    journal: Any, evidence: Evidence
) -> tuple[str, str, str, str, int] | None:
    """``(broker, account_mode, venue, broker_order_id, fill_seq)`` of the cited fill."""
    broker = BROKER_BY_ACCOUNT.get(journal.account or "")
    if broker is None:
        return None
    return (broker, "live", evidence.venue, evidence.broker_order_id, evidence.fill_seq)


def plan_journal(
    journal: Any, evidence: Evidence, rows: Sequence[LedgerRowLike]
) -> JournalPlan:
    """Judge one journal against its evidence and every ledger row of the cited order.

    The cited ``fill_seq`` is a claim, not proof of uniqueness: all rows of the
    order are read, and an order with more than one in-effect authoritative row
    is refused because the evidence cannot say which fill the allocation closed.
    """
    jid = int(journal.id)
    if journal.status != JournalStatus.closed:
        return _ineligible(jid, "journal_not_closed", journal)
    if journal.account_type != "live":
        return _ineligible(jid, "journal_not_live", journal)
    if journal.side != "buy":
        return _ineligible(jid, "unsupported_journal_side", journal)
    if journal.instrument_type != InstrumentType.equity_kr:
        return _ineligible(jid, "unsupported_instrument", journal)
    if BROKER_BY_ACCOUNT.get(journal.account or "") is None:
        return _ineligible(jid, "unsupported_account", journal)
    if (
        journal.entry_price is None
        or journal.quantity is None
        or journal.exit_price is None
        or journal.exit_date is None
        or Decimal(journal.quantity) <= 0
    ):
        return _ineligible(jid, "journal_fields_missing", journal)
    # The cited close record must name this journal and the cited broker order.
    if jid not in evidence.record_closed_journal_ids:
        return _ineligible(jid, "evidence_does_not_list_journal", journal)
    if evidence.record_order_id != evidence.broker_order_id:
        return _ineligible(jid, "evidence_order_mismatch", journal)
    if not rows:
        return _ineligible(jid, "ledger_row_not_found", journal)
    usable = [
        r
        for r in rows
        if r.quarantined_at is None and r.source in AUTHORITATIVE_SOURCES
    ]
    if not usable:
        return _ineligible(jid, "ledger_row_not_in_effect_authoritative", journal)
    if len(usable) > 1:
        return _ineligible(jid, "ambiguous_order_fills", journal)
    [row] = usable

    quantity = Decimal(journal.quantity)
    identity = ledger_identity(journal, evidence)
    if (
        row.broker,
        row.account_mode,
        row.venue,
        row.broker_order_id,
        row.fill_seq,
    ) != identity:
        return _ineligible(jid, "ledger_row_not_in_effect_authoritative", journal)
    if (
        row.instrument_type != InstrumentType.equity_kr
        or row.symbol != journal.symbol
        or row.side != "sell"
        or row.currency != "KRW"
    ):
        return _ineligible(jid, "ledger_row_is_not_this_sell", journal)
    if evidence.allocated_quantity != quantity:
        return _ineligible(jid, "allocation_quantity_mismatch", journal)
    if evidence.allocated_quantity > Decimal(row.filled_qty):
        return _ineligible(jid, "allocation_exceeds_fill", journal)
    if _q(Decimal(row.filled_price)) != _q(Decimal(journal.exit_price)):
        return _ineligible(jid, "ledger_price_mismatch", journal)
    # ``filled_at`` is an execution time only when its basis says so; KIS
    # reconciler rows carry the order time. Otherwise the evidence must supply a
    # retained, verified execution time.
    if row.filled_at_basis == "broker_fill_time":
        if evidence.verified_fill_at not in (None, row.filled_at):
            return _ineligible(jid, "verified_fill_time_conflicts_with_ledger", journal)
        exec_time, time_source = row.filled_at, "ledger_broker_fill_time"
    elif evidence.verified_fill_at is not None:
        exec_time, time_source = evidence.verified_fill_at, "evidence_verified"
    else:
        return _ineligible(jid, "fill_time_not_a_broker_fill_time", journal)
    if exec_time > journal.exit_date:
        return _ineligible(jid, "fill_after_booking", journal)

    entry_price = Decimal(journal.entry_price)
    new_pnl = _q((Decimal(row.filled_price) - entry_price) * quantity)
    current_pnl = (
        None if journal.total_pnl_krw is None else _q(Decimal(journal.total_pnl_krw))
    )
    done = (
        journal.exit_date == exec_time
        and current_pnl == new_pnl
        and _marker(int(row.id), evidence.evidence_ref) in (journal.notes or "")
    )
    return JournalPlan(
        journal_id=jid,
        status="already_corrected" if done else "eligible",
        symbol=journal.symbol,
        account=journal.account,
        quantity=quantity,
        entry_price=entry_price,
        current_exit_date=journal.exit_date,
        current_total_pnl_krw=current_pnl,
        new_exit_date=exec_time,
        new_total_pnl_krw=new_pnl,
        ledger_id=int(row.id),
        ledger_source=row.source,
        ledger_filled_qty=Decimal(row.filled_qty),
        ledger_filled_price=Decimal(row.filled_price),
        ledger_filled_at_basis=row.filled_at_basis,
        fill_time_source=time_source,
        evidence=evidence,
        notes_sha256=_sha(journal.notes),
    )


@dataclass(frozen=True)
class BatchPlan:
    plans: tuple[JournalPlan, ...]

    @property
    def status(self) -> Literal["eligible", "noop", "refused"]:
        if any(p.status == "ineligible" for p in self.plans):
            return "refused"
        if all(p.status == "already_corrected" for p in self.plans):
            return "noop"
        return "eligible"

    @property
    def digest(self) -> str:
        """Fingerprint of every fact the write depends on, evidence included."""
        body = [{**p.as_dict(), "notes_sha256": p.notes_sha256} for p in self.plans]
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def plan_batch(
    ids: Sequence[int],
    journals: dict[int, Any],
    evidence: dict[int, Evidence],
    fills: dict[int, Sequence[LedgerRowLike]],
) -> BatchPlan:
    plans: list[JournalPlan] = []
    for jid in ids:
        journal = journals.get(jid)
        if journal is None:
            plans.append(_ineligible(jid, "journal_not_found"))
        else:
            plans.append(plan_journal(journal, evidence[jid], fills.get(jid, ())))
    # Several journals may share one fill, but only within what it booked.
    allocated: dict[int, Decimal] = {}
    for p in plans:
        if p.ledger_id is not None and p.evidence is not None:
            allocated[p.ledger_id] = (
                allocated.get(p.ledger_id, Decimal(0)) + p.evidence.allocated_quantity
            )
    plans = [
        _ineligible(p.journal_id, "allocations_exceed_fill", journals[p.journal_id])
        if p.ledger_id is not None
        and p.ledger_filled_qty is not None
        and allocated[p.ledger_id] > p.ledger_filled_qty
        else p
        for p in plans
    ]
    return BatchPlan(tuple(plans))


# ------------------------------------------------------------------ database


async def load_journals(
    session: AsyncSession, ids: Sequence[int], *, lock: bool
) -> dict[int, TradeJournal]:
    stmt = select(TradeJournal).where(TradeJournal.id.in_(list(ids)))
    stmt = stmt.order_by(TradeJournal.id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    rows = (await session.execute(stmt)).scalars().all()
    return {int(row.id): row for row in rows}


async def load_order_fills(
    session: AsyncSession, journal: TradeJournal, evidence: Evidence
) -> list[ExecutionLedger]:
    """Every in-effect ledger row of the cited order, whatever its ``fill_seq``.

    The planner refuses an order with more than one authoritative row, so a
    chosen sequence cannot make an ambiguous order look unique. Reads only;
    every fill reader ANDs ``execution_ledger_in_effect`` (#1175), so a
    quarantined row is not found.
    """
    identity = ledger_identity(journal, evidence)
    if identity is None:
        return []
    broker, account_mode, venue, broker_order_id, _fill_seq = identity
    stmt = (
        select(ExecutionLedger)
        .where(
            execution_ledger_in_effect(),
            ExecutionLedger.broker == broker,
            ExecutionLedger.account_mode == account_mode,
            ExecutionLedger.venue == venue,
            ExecutionLedger.broker_order_id == broker_order_id,
        )
        .order_by(ExecutionLedger.fill_seq, ExecutionLedger.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(stmt)).scalars().all())


async def lock_ledger_writers(session: AsyncSession) -> None:
    """Hold off every ledger writer until this transaction ends.

    SHARE mode conflicts with the row-exclusive lock that inserts and updates of
    ``execution_ledger`` take, so after this returns no writer is in flight and
    none can change the cited fill before the commit. Readers are not affected.
    Taken before the journal row locks: ledger writers go ledger-then-journal, so
    the same order cannot deadlock with them.
    """
    await session.execute(text(f"SET LOCAL lock_timeout = {int(LOCK_TIMEOUT_MS)}"))
    await session.execute(text(f"LOCK TABLE {LEDGER_TABLE} IN SHARE MODE"))


async def _build_plan(
    session: AsyncSession,
    ids: Sequence[int],
    evidence: dict[int, Evidence],
    *,
    lock: bool,
) -> tuple[BatchPlan, dict[int, TradeJournal]]:
    journals = await load_journals(session, ids, lock=lock)
    fills = {
        jid: await load_order_fills(session, journal, evidence[jid])
        for jid, journal in journals.items()
    }
    return plan_batch(ids, journals, evidence, fills), journals


@dataclass(frozen=True)
class BatchResult:
    status: BatchStatus
    plan: BatchPlan
    written: tuple[int, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "decision_ref": DECISION_REF,
            "plan_digest": self.plan.digest,
            "written_journal_ids": list(self.written),
            "journals": [p.as_dict() for p in self.plan.plans],
        }


async def preview(
    session: AsyncSession, ids: Sequence[int], evidence: dict[int, Evidence]
) -> BatchResult:
    """Read-only plan; the transaction is ended without writing."""
    await session.execute(text("SET TRANSACTION READ ONLY"))
    try:
        plan, _ = await _build_plan(session, ids, evidence, lock=False)
    finally:
        await session.rollback()
    return BatchResult(plan.status, plan)


def audit_note(plan: JournalPlan, *, reason: str, actor: str) -> str:
    def money(value: Decimal | None) -> str:
        return "None" if value is None else format(value, "f")

    def when(value: datetime | None) -> str:
        return "None" if value is None else value.isoformat()

    assert plan.ledger_id is not None and plan.evidence is not None
    ev = plan.evidence
    return (
        f"[{_marker(plan.ledger_id, ev.evidence_ref)}] "
        f"exit_date {when(plan.current_exit_date)} -> {when(plan.new_exit_date)}; "
        f"total_pnl_krw {money(plan.current_total_pnl_krw)} -> "
        f"{money(plan.new_total_pnl_krw)}; "
        f"fill={ev.venue}/{ev.broker_order_id}/{ev.fill_seq} "
        f"fill_time={plan.fill_time_source} "
        f"allocated={format(ev.allocated_quantity, 'f')}; "
        f"reason={reason}; actor={actor}; decision={DECISION_REF}"
    )


def apply_plan(
    journal: TradeJournal, plan: JournalPlan, *, reason: str, actor: str
) -> None:
    """Write the corrected exit fields and the audit note on a locked lot row.

    ``exit_price`` and ``pnl_pct`` are deliberately not touched.
    """
    journal.exit_date = plan.new_exit_date
    journal.total_pnl_krw = plan.new_total_pnl_krw
    line = audit_note(plan, reason=reason, actor=actor)
    existing = journal.notes
    journal.notes = (
        f"{existing.rstrip()}\n\n{line}" if existing and existing.strip() else line
    )


async def commit(
    session: AsyncSession,
    ids: Sequence[int],
    evidence: dict[int, Evidence],
    *,
    expected_digest: str,
    reason: str,
    actor: str,
) -> BatchResult:
    """Hold off ledger writers, lock the journals, write only if the plan equals the preview."""
    try:
        await lock_ledger_writers(session)
        plan, journals = await _build_plan(session, ids, evidence, lock=True)
        if plan.status == "refused":
            await session.rollback()
            return BatchResult("refused", plan)
        if plan.status == "noop":
            # Every journal already holds the corrected values and its receipt
            # for this evidence, so there is nothing to write whatever digest was
            # given. An identical retry after a lost response lands here.
            await session.rollback()
            return BatchResult("noop", plan)
        if plan.digest != expected_digest:
            await session.rollback()
            return BatchResult("plan_changed", plan)
        written: list[int] = []
        for item in plan.plans:
            if item.status != "eligible":
                continue
            apply_plan(journals[item.journal_id], item, reason=reason, actor=actor)
            written.append(item.journal_id)
        await session.flush()
        await session.commit()
        return BatchResult("committed", plan, tuple(written))
    except BaseException:
        await session.rollback()
        raise
