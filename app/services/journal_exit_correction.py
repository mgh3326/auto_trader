"""hk 5357 — one-off correction of exit fields on lots closed by a late reconcile.

Before hk 5332 (PR #2468) a reconcile that booked a sell stamped the closed lot
journal with the booking time as ``exit_date`` and left ``total_pnl_krw`` empty.
New bookings are right; this module repairs the three older rows by the same
rules: ``exit_date`` is the ``filled_at`` of the in-effect authoritative sell
ledger row that closed the lot, and ``total_pnl_krw`` is
``(fill price - entry_price) * quantity`` (KRW equities only).

A closed lot journal keeps no reference to its closing order, so the sell row is
found by account, symbol, quantity and price within a bounded window before the
recorded ``exit_date``. Exactly one row must match; zero, several or an
unreadable journal make that journal ``ineligible`` and the whole batch is left
untouched. Nothing here sends orders, reads a broker or writes the ledger.

The layers are split so the rules are testable without a database:

* ``plan_journal`` / ``plan_batch`` — pure; compare a journal with candidate rows.
* ``load_candidates`` — the only ledger reader, ANDs ``execution_ledger_in_effect``.
* ``preview`` / ``commit`` — session orchestration; ``commit`` locks the journal
  rows, recomputes the plan and writes only if it still equals the previewed one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
#: How long before the recorded ``exit_date`` the closing fill may sit. The
#: three known rows were booked about a day after the fill.
FILL_WINDOW = timedelta(days=7)
#: journal ``account`` -> ledger ``broker``
BROKER_BY_ACCOUNT = {"kis": "kis", "toss": "toss"}

_MONEY = Decimal("0.0001")
_ID = re.compile(r"[1-9][0-9]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

Status = Literal["eligible", "already_corrected", "ineligible"]
BatchStatus = Literal["eligible", "noop", "refused", "committed", "plan_changed"]


class CorrectionInputError(ValueError):
    """Bad CLI input; raised before any database access."""


class LedgerRowLike(Protocol):
    id: int
    broker: str
    account_mode: str
    instrument_type: Any
    symbol: str
    side: str
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
    ledger_filled_price: Decimal | None = None
    ledger_filled_at_basis: str | None = None
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
            "ledger_filled_price": money(self.ledger_filled_price),
            "ledger_filled_at_basis": self.ledger_filled_at_basis,
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


def _marker(ledger_id: int) -> str:
    return f"exit_correction:{NOTE_TOKEN} ledger_id={ledger_id}"


def _sha(text_value: str | None) -> str:
    return hashlib.sha256((text_value or "").encode("utf-8")).hexdigest()


def plan_journal(journal: Any, candidates: Iterable[LedgerRowLike]) -> JournalPlan:
    """Judge one journal against the ledger rows that might have closed it."""
    jid = int(journal.id)
    if journal.status != JournalStatus.closed:
        return _ineligible(jid, "journal_not_closed", journal)
    if journal.account_type != "live":
        return _ineligible(jid, "journal_not_live", journal)
    if journal.side != "buy":
        return _ineligible(jid, "unsupported_journal_side", journal)
    if journal.instrument_type != InstrumentType.equity_kr:
        return _ineligible(jid, "unsupported_instrument", journal)
    broker = BROKER_BY_ACCOUNT.get(journal.account or "")
    if broker is None:
        return _ineligible(jid, "unsupported_account", journal)
    if (
        journal.entry_price is None
        or journal.quantity is None
        or journal.exit_price is None
        or journal.exit_date is None
        or journal.created_at is None
        or Decimal(journal.quantity) <= 0
    ):
        return _ineligible(jid, "journal_fields_missing", journal)

    quantity = Decimal(journal.quantity)
    exit_price = _q(Decimal(journal.exit_price))
    low = journal.exit_date - FILL_WINDOW
    matches = [
        row
        for row in candidates
        if row.quarantined_at is None
        and row.source in AUTHORITATIVE_SOURCES
        and row.broker == broker
        and row.account_mode == "live"
        and row.instrument_type == InstrumentType.equity_kr
        and row.symbol == journal.symbol
        and row.side == "sell"
        and Decimal(row.filled_qty) == quantity
        and _q(Decimal(row.filled_price)) == exit_price
        and max(low, journal.created_at) <= row.filled_at <= journal.exit_date
    ]
    if not matches:
        return _ineligible(jid, "no_matching_ledger_row", journal)
    if len(matches) > 1:
        return _ineligible(jid, "ambiguous_ledger_rows", journal)
    [row] = matches

    entry_price = Decimal(journal.entry_price)
    new_pnl = _q((Decimal(row.filled_price) - entry_price) * quantity)
    current_pnl = (
        None if journal.total_pnl_krw is None else _q(Decimal(journal.total_pnl_krw))
    )
    done = (
        journal.exit_date == row.filled_at
        and current_pnl == new_pnl
        and _marker(int(row.id)) in (journal.notes or "")
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
        new_exit_date=row.filled_at,
        new_total_pnl_krw=new_pnl,
        ledger_id=int(row.id),
        ledger_source=row.source,
        ledger_filled_price=Decimal(row.filled_price),
        ledger_filled_at_basis=row.filled_at_basis,
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
        """Fingerprint of every fact the write depends on."""
        body = [{**p.as_dict(), "notes_sha256": p.notes_sha256} for p in self.plans]
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def plan_batch(
    ids: Sequence[int],
    journals: dict[int, Any],
    candidates: dict[int, Sequence[LedgerRowLike]],
) -> BatchPlan:
    plans: list[JournalPlan] = []
    for jid in ids:
        journal = journals.get(jid)
        if journal is None:
            plans.append(_ineligible(jid, "journal_not_found"))
        else:
            plans.append(plan_journal(journal, candidates.get(jid, ())))
    # One sell row closes one lot here; a row claimed twice is ambiguous.
    claimed: dict[int, int] = {}
    for p in plans:
        if p.ledger_id is not None:
            claimed[p.ledger_id] = claimed.get(p.ledger_id, 0) + 1
    plans = [
        _ineligible(
            p.journal_id, "ledger_row_shared_by_journals", journals[p.journal_id]
        )
        if p.ledger_id is not None and claimed[p.ledger_id] > 1
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


async def load_candidates(
    session: AsyncSession, journal: TradeJournal
) -> list[ExecutionLedger]:
    """In-effect authoritative KR sell rows that could have closed ``journal``.

    Reads only; every fill reader ANDs ``execution_ledger_in_effect`` (#1175).
    The pure planner narrows by quantity and price.
    """
    broker = BROKER_BY_ACCOUNT.get(journal.account or "")
    if (
        broker is None
        or journal.exit_date is None
        or journal.instrument_type != InstrumentType.equity_kr
    ):
        return []
    stmt = (
        select(ExecutionLedger)
        .where(
            execution_ledger_in_effect(),
            ExecutionLedger.broker == broker,
            ExecutionLedger.account_mode == "live",
            ExecutionLedger.instrument_type == InstrumentType.equity_kr,
            ExecutionLedger.symbol == journal.symbol,
            ExecutionLedger.side == "sell",
            ExecutionLedger.source.in_(sorted(AUTHORITATIVE_SOURCES)),
            ExecutionLedger.filled_at <= journal.exit_date,
            ExecutionLedger.filled_at >= journal.exit_date - FILL_WINDOW,
        )
        .order_by(ExecutionLedger.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _build_plan(
    session: AsyncSession, ids: Sequence[int], *, lock: bool
) -> tuple[BatchPlan, dict[int, TradeJournal]]:
    journals = await load_journals(session, ids, lock=lock)
    candidates = {jid: await load_candidates(session, j) for jid, j in journals.items()}
    return plan_batch(ids, journals, candidates), journals


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


async def preview(session: AsyncSession, ids: Sequence[int]) -> BatchResult:
    """Read-only plan; the transaction is ended without writing."""
    await session.execute(text("SET TRANSACTION READ ONLY"))
    try:
        plan, _ = await _build_plan(session, ids, lock=False)
    finally:
        await session.rollback()
    return BatchResult(plan.status, plan)


def audit_note(plan: JournalPlan, *, reason: str, actor: str) -> str:
    def money(value: Decimal | None) -> str:
        return "None" if value is None else format(value, "f")

    def when(value: datetime | None) -> str:
        return "None" if value is None else value.isoformat()

    assert plan.ledger_id is not None
    return (
        f"[{_marker(plan.ledger_id)}] "
        f"exit_date {when(plan.current_exit_date)} -> {when(plan.new_exit_date)}; "
        f"total_pnl_krw {money(plan.current_total_pnl_krw)} -> "
        f"{money(plan.new_total_pnl_krw)}; "
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
    *,
    expected_digest: str,
    reason: str,
    actor: str,
) -> BatchResult:
    """Lock the journals, recompute the plan, write only if it equals the preview."""
    try:
        plan, journals = await _build_plan(session, ids, lock=True)
        if plan.status == "refused":
            await session.rollback()
            return BatchResult("refused", plan)
        if plan.digest != expected_digest:
            await session.rollback()
            return BatchResult("plan_changed", plan)
        if plan.status == "noop":
            await session.rollback()
            return BatchResult("noop", plan)
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
