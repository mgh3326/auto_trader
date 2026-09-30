"""Operator quarantine of phantom websocket rows in the execution ledger (#1175).

hk #1172: fillwire recorded KIS H0STCNI0 *accept* notices (``CNTG_YN=1``) as
fills. Those rows are not executions, yet every lot/evidence reader counted
them, turning the symbol ``quantity_mismatch``/``unknown`` and blocking the
#1112 inference. Ledgers are never hand-edited, so this is the service-layer
lever: it marks an exact set of rows as quarantined (they stay in the table,
nothing is deleted) and writes one append-only audit record per row.

Eligibility is proved from the row itself, never assumed. A row qualifies only
when it is a live KIS KR ``websocket`` row whose stored ``raw_payload_json`` is
the fillwire frame of a domestic execution notice (``tr == "H0STCNI0"``) with
``CNTG_YN`` (field 13) exactly ``"1"`` and whose order number (field 2) and
symbol (field 8) equal the row's own. A ``CNTG_YN="2"`` frame is a real fill
and is refused, as is any row without a readable frame. One ineligible id
refuses the whole batch; a batch that is already entirely quarantined is a
no-op. The account fields (0, 1) of the frame are never read or echoed.

The DB backs this up independently: CHECKs keep a quarantine all-or-nothing
and confined to ``source='websocket' AND broker='kis'``, and a trigger makes a
quarantine permanent (it cannot be cleared or rewritten).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.execution_ledger.repository import ExecutionLedgerRepository

#: KIS domestic execution-notice TR (live). Mock/overseas frames are refused.
ACCEPT_NOTICE_TR = "H0STCNI0"
#: Field indices of a decrypted H0STCNI0 record (go-kis ``kis/ws`` layout).
FIELD_ORDER_NO = 2
FIELD_SIDE = 4
FIELD_SYMBOL = 8
FIELD_CNTG_YN = 13
#: ``CNTG_YN`` values: 1 = order accept/confirm notice, 2 = execution (fill).
CNTG_YN_ACCEPT = "1"
CNTG_YN_FILL = "2"

MAX_IDS = 50
MAX_REASON_CHARS = 500
MAX_ACTOR_CHARS = 100
_BIGINT_MAX = 2**63 - 1
#: One exact positive decimal id. ASCII only, no sign, no leading zero, no
#: whitespace, no range or wildcard syntax.
_ID_TOKEN = re.compile(r"[1-9][0-9]{0,18}", re.ASCII)

Verdict = Literal[
    "accept_notice",
    "not_found",
    "already_quarantined",
    "not_websocket",
    "not_kis",
    "not_live",
    "not_equity_kr",
    "raw_payload_missing",
    "raw_payload_not_domestic_execution_notice",
    "raw_payload_fields_malformed",
    "fill_notice_cntg_yn_2",
    "cntg_yn_not_accept",
    "raw_order_no_mismatch",
    "raw_symbol_mismatch",
]
BatchStatus = Literal["eligible", "refused", "noop", "committed"]


class QuarantineInputError(ValueError):
    """Operator input (ids, reason, actor) is not acceptable."""


class QuarantineConflictError(RuntimeError):
    """The guarded UPDATE did not touch exactly the verified rows."""


@dataclass(frozen=True, slots=True)
class RowVerdict:
    ledger_id: int
    verdict: Verdict
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return self.verdict == "accept_notice"

    @property
    def already_quarantined(self) -> bool:
        return self.verdict == "already_quarantined"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ledger_id": self.ledger_id,
            "verdict": self.verdict,
            "eligible": self.eligible,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class BatchResult:
    status: BatchStatus
    ids: tuple[int, ...]
    rows: tuple[RowVerdict, ...]
    batch_id: uuid.UUID | None = None
    changed: int = 0

    @property
    def refused_ids(self) -> list[int]:
        return [row.ledger_id for row in self.rows if not row.eligible]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "requested_ids": list(self.ids),
            "refused_ids": [] if self.status == "noop" else self.refused_ids,
            "rows": [row.as_dict() for row in self.rows],
            "batch_id": None if self.batch_id is None else str(self.batch_id),
            "changed": self.changed,
        }


# --------------------------------------------------------------------- input


def parse_ids(values: Iterable[str]) -> tuple[int, ...]:
    """Exact ledger ids from ``--ids`` values (repeatable, comma lists).

    Every token must be a plain positive decimal. Ranges, patterns, signs,
    whitespace, non-ASCII digits, empty tokens and duplicates are refused
    rather than interpreted.
    """
    ids: list[int] = []
    for value in values:
        if not isinstance(value, str):
            raise QuarantineInputError("--ids values must be strings")
        for token in value.split(","):
            if not _ID_TOKEN.fullmatch(token):
                raise QuarantineInputError(
                    f"--ids token {token!r} is not an exact positive decimal id"
                )
            number = int(token)
            if number > _BIGINT_MAX:
                raise QuarantineInputError(f"--ids token {token!r} is out of range")
            if number in ids:
                raise QuarantineInputError(f"--ids repeats id {number}")
            ids.append(number)
    if not ids:
        raise QuarantineInputError("--ids requires at least one id")
    if len(ids) > MAX_IDS:
        raise QuarantineInputError(f"--ids accepts at most {MAX_IDS} ids")
    return tuple(ids)


def validate_text(name: str, value: Any, *, max_chars: int) -> str:
    """A required, bounded, single-line operator string (reason / actor)."""
    if not isinstance(value, str):
        raise QuarantineInputError(f"{name} is required")
    text = value.strip()
    if not text:
        raise QuarantineInputError(f"{name} must not be blank")
    if len(text) > max_chars:
        raise QuarantineInputError(f"{name} exceeds {max_chars} characters")
    if any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in text):
        raise QuarantineInputError(f"{name} must not contain control characters")
    return text


# ------------------------------------------------------------------ verdicts


def fillwire_fill_seq(fields: Sequence[str]) -> int:
    """fillwire ``DeriveFillSeq``: sha256 of the ``^``-joined fields, 31 bits."""
    digest = hashlib.sha256("^".join(fields).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _field(fields: Sequence[str], index: int) -> str:
    return fields[index].strip()


def evaluate_row(ledger_id: int, row: Any | None) -> RowVerdict:
    """Pure eligibility verdict for one requested id.

    ``row`` is an ``ExecutionLedger`` (or anything with the same attributes)
    or ``None`` when the id does not exist.
    """
    if row is None:
        return RowVerdict(ledger_id, "not_found")
    identity: dict[str, Any] = {
        "source": row.source,
        "broker": row.broker,
        "account_mode": row.account_mode,
        "venue": row.venue,
        "instrument_type": str(
            getattr(row.instrument_type, "value", row.instrument_type)
        ),
        "symbol": row.symbol,
        "side": row.side,
        "broker_order_id": row.broker_order_id,
        "fill_seq": row.fill_seq,
        "filled_qty": format(row.filled_qty, "f"),
        "filled_price": format(row.filled_price, "f"),
        "filled_at": row.filled_at.isoformat() if row.filled_at else None,
    }
    if row.quarantined_at is not None:
        identity.update(
            quarantined_at=row.quarantined_at.isoformat(),
            quarantine_reason=row.quarantine_reason,
            quarantined_by=row.quarantined_by,
        )
        return RowVerdict(ledger_id, "already_quarantined", identity)
    if row.source != "websocket":
        return RowVerdict(ledger_id, "not_websocket", identity)
    if row.broker != "kis":
        return RowVerdict(ledger_id, "not_kis", identity)
    if row.account_mode != "live":
        return RowVerdict(ledger_id, "not_live", identity)
    if identity["instrument_type"] != "equity_kr":
        return RowVerdict(ledger_id, "not_equity_kr", identity)

    raw = row.raw_payload_json
    if not isinstance(raw, Mapping) or not raw:
        return RowVerdict(ledger_id, "raw_payload_missing", identity)
    tr = raw.get("tr")
    identity["raw_tr"] = tr if isinstance(tr, str) else None
    if tr != ACCEPT_NOTICE_TR:
        return RowVerdict(
            ledger_id, "raw_payload_not_domestic_execution_notice", identity
        )
    fields = raw.get("fields")
    if (
        not isinstance(fields, list)
        or len(fields) <= FIELD_CNTG_YN
        or not all(isinstance(item, str) for item in fields)
    ):
        return RowVerdict(ledger_id, "raw_payload_fields_malformed", identity)

    cntg_yn = _field(fields, FIELD_CNTG_YN)
    identity["raw_cntg_yn"] = cntg_yn
    identity["raw_order_no"] = _field(fields, FIELD_ORDER_NO)
    identity["raw_symbol"] = _field(fields, FIELD_SYMBOL)
    identity["raw_side_code"] = _field(fields, FIELD_SIDE)
    received_at = raw.get("received_at")
    identity["raw_received_at"] = received_at if isinstance(received_at, str) else None
    recomputed = fillwire_fill_seq(fields)
    identity["raw_fill_seq_recomputed"] = recomputed
    # Informational provenance: fillwire keys the row by this digest of the
    # same frame. A mismatch is reported, not gated (a non-UTF-8 byte in an
    # unrelated field could alter it after JSON encoding).
    identity["raw_fill_seq_matches"] = recomputed == row.fill_seq

    if cntg_yn == CNTG_YN_FILL:
        return RowVerdict(ledger_id, "fill_notice_cntg_yn_2", identity)
    if cntg_yn != CNTG_YN_ACCEPT:
        return RowVerdict(ledger_id, "cntg_yn_not_accept", identity)
    if identity["raw_order_no"] != str(row.broker_order_id).strip():
        return RowVerdict(ledger_id, "raw_order_no_mismatch", identity)
    if identity["raw_symbol"].upper() != str(row.symbol).strip().upper():
        return RowVerdict(ledger_id, "raw_symbol_mismatch", identity)
    return RowVerdict(ledger_id, "accept_notice", identity)


def decide(ids: Sequence[int], verdicts: Sequence[RowVerdict]) -> BatchStatus:
    """All eligible → eligible; all already quarantined → noop; else refused."""
    if verdicts and all(v.already_quarantined for v in verdicts):
        return "noop"
    if verdicts and len(verdicts) == len(ids) and all(v.eligible for v in verdicts):
        return "eligible"
    return "refused"


# ----------------------------------------------------------------------- I/O


async def _evaluate(
    session: AsyncSession, ids: Sequence[int], *, for_update: bool
) -> tuple[RowVerdict, ...]:
    if len(set(ids)) != len(ids):
        raise QuarantineInputError("ids must be unique")
    rows = await ExecutionLedgerRepository(session).rows_by_ids(
        list(ids), for_update=for_update
    )
    return tuple(evaluate_row(ledger_id, rows.get(ledger_id)) for ledger_id in ids)


async def preview_quarantine(session: AsyncSession, ids: Sequence[int]) -> BatchResult:
    """Read-only verdicts. Writes nothing and ends by rolling back."""
    try:
        verdicts = await _evaluate(session, ids, for_update=False)
    finally:
        await session.rollback()
    return BatchResult(decide(ids, verdicts), tuple(ids), verdicts)


async def commit_quarantine(
    session: AsyncSession,
    ids: Sequence[int],
    *,
    reason: str,
    actor: str,
    now: datetime | None = None,
) -> BatchResult:
    """Quarantine exactly ``ids`` in one transaction, or change nothing.

    Rows are locked, re-verified under the lock, and then updated by a guarded
    statement whose row count must equal the batch size; one audit row per
    ledger row is appended in the same transaction. A refused or no-op batch
    rolls back without writing.
    """
    reason_text = validate_text("reason", reason, max_chars=MAX_REASON_CHARS)
    actor_text = validate_text("actor", actor, max_chars=MAX_ACTOR_CHARS)
    try:
        verdicts = await _evaluate(session, ids, for_update=True)
        status = decide(ids, verdicts)
        if status != "eligible":
            await session.rollback()
            return BatchResult(status, tuple(ids), verdicts)

        moment = now or datetime.now(UTC)
        batch_id = uuid.uuid4()
        repo = ExecutionLedgerRepository(session)
        changed = await repo.mark_quarantined(
            list(ids), at=moment, reason=reason_text, actor=actor_text
        )
        if changed != len(ids):
            raise QuarantineConflictError(
                f"guarded update touched {changed} rows, expected {len(ids)}"
            )
        await repo.append_quarantine_events(
            [
                {
                    "batch_id": batch_id,
                    "ledger_id": verdict.ledger_id,
                    # idempotency-key tombstone (re-insert is re-quarantined)
                    "broker": verdict.detail["broker"],
                    "account_mode": verdict.detail["account_mode"],
                    "venue": verdict.detail["venue"],
                    "broker_order_id": verdict.detail["broker_order_id"],
                    "fill_seq": verdict.detail["fill_seq"],
                    "action": "quarantine",
                    "reason": reason_text,
                    "actor": actor_text,
                    "evidence": verdict.detail,
                }
                for verdict in verdicts
            ]
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return BatchResult("committed", tuple(ids), verdicts, batch_id, changed)
