"""Operator reconcile of the filled D2 remediation roots (#1268, #1122 blocker 2).

The ``d2_remediation_single`` writer dispatched three SELL LIMIT orders on the
Binance **Spot Demo** account and recorded them up to ``submitted``; the roots
were later moved to ``filled``. Nothing in the D2 runbook moves a filled
remediation root on to its terminal state, so the three roots stay open
(``filled`` is a blocking root state) and the H5 truth gate's
``demo_ledger_no_open_roots`` check fails on them.

This is the service-layer lever for exactly that and nothing else. It moves an
exact set of ledger ids ``filled → closed → reconciled`` through
:class:`BinanceDemoLedgerService` (the only legal path from ``filled`` to a
terminal state that is not ``anomaly``), in one transaction, and only after
both of these hold for **every** id:

1. **The row proves it is a D2 remediation root.** A ``spot`` root on
   ``demo-api.binance.com`` in ``filled``, written by ``d2_remediation_single``
   under exception ``binance-demo-remediation-20260820`` and remediation
   ``d2-binance-demo-both-remediation-v2.1-20260818``, with
   ``canary_or_strategy_use=forbidden``, the sealed credential fingerprint, a
   broker order id, and a client order id, instrument, side, type, quantity and
   price equal to one of the three frozen :data:`D2_BOUND_ORDERS`.
2. **The broker proves the order filled.** A read-only
   ``GET /api/v3/order`` on the Spot Demo host (the same client and call the D2
   writer uses for readback) returns that exact client order id and broker
   order id with ``status=FILLED``, the bound symbol, side, type, ``origQty``,
   ``executedQty``, limit price and ``timeInForce``; any fill actual already on
   the row must equal the broker's.

One ineligible id, one missing or mismatching piece of broker evidence, or one
unreadable broker answer refuses the whole batch and writes nothing. A batch
whose ids were all already reconciled by this tool is a no-op. Nothing is ever
deleted, and no row is written outside the ``record_*`` transitions. Each root
keeps one audit record (``extra_metadata["d2_root_reconcile"]``: batch id,
reason, actor, time and the broker evidence) written by the ``closed``
transition; the fill actuals the ledger treats as immutable are never touched.

No order, cancel or other signed mutation is reachable from here: the only
broker call is ``get_order_status``. The client must point at the Spot Demo
host and carry the sealed D2 credential fingerprint, or nothing is read.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.spot_demo.d2_remediation_single import (
    D2_BOUND_ORDERS,
    D2_CREDENTIAL_FINGERPRINT,
    D2_EXCEPTION_ID,
    D2_PRODUCT,
    D2_REMEDIATION_ID,
    D2_VENUE,
    D2_VENUE_HOST,
    WRITER_NAME,
    D2BoundOrder,
    broker_identifier_problem,
)
from app.services.brokers.binance.spot_demo.host_allowlist import assert_spot_demo_host

TOOL_NAME: Final[str] = "d2_root_reconcile"
AUDIT_KEY: Final[str] = "d2_root_reconcile"
AUDIT_SCHEMA: Final[str] = "d2-root-reconcile.v1"

#: Only three D2 roots can exist, one per bound order.
MAX_IDS: Final[int] = len(D2_BOUND_ORDERS)
MAX_REASON_CHARS: Final[int] = 500
MAX_ACTOR_CHARS: Final[int] = 100
_BIGINT_MAX: Final[int] = 2**63 - 1
_ID_TOKEN = re.compile(r"[1-9][0-9]{0,18}", re.ASCII)

_BOUND_BY_CID: Final[dict[str, D2BoundOrder]] = {
    order.client_order_id: order for order in D2_BOUND_ORDERS
}

RowVerdictCode = Literal[
    "d2_filled_root",
    "already_reconciled",
    "not_found",
    "not_spot",
    "not_spot_demo_host",
    "not_root",
    "not_filled",
    "not_d2_writer",
    "exception_mismatch",
    "remediation_mismatch",
    "canary_use_not_forbidden",
    "credential_fingerprint_mismatch",
    "not_bound_order",
    "instrument_mismatch",
    "side_mismatch",
    "order_type_mismatch",
    "qty_mismatch",
    "price_mismatch",
    "broker_order_id_missing",
]
EvidenceVerdictCode = Literal[
    "broker_filled_match",
    "evidence_order_not_found",
    "evidence_read_failed",
    "evidence_not_mapping",
    "evidence_client_order_id",
    "evidence_order_id",
    "evidence_symbol",
    "evidence_side",
    "evidence_type",
    "evidence_status_not_filled",
    "evidence_orig_qty",
    "evidence_executed_qty",
    "evidence_price",
    "evidence_time_in_force",
    "evidence_fill_actual_conflict",
]
BatchStatus = Literal["eligible", "refused", "noop", "committed"]


class D2RootReconcileInputError(ValueError):
    """Operator input or the broker client is not acceptable; nothing was read."""


class D2RootReconcileConflictError(RuntimeError):
    """The transitions did not leave exactly the verified roots reconciled."""


class OrderStatusReader(Protocol):
    """The one broker read this module performs."""

    @property
    def credential_fingerprint(self) -> str: ...

    async def get_order_status(
        self, *, symbol: str, client_order_id: str
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class EvidenceVerdict:
    verdict: EvidenceVerdictCode
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def matches(self) -> bool:
        return self.verdict == "broker_filled_match"

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "matches": self.matches, **self.detail}


@dataclass(frozen=True, slots=True)
class RowVerdict:
    ledger_id: int
    verdict: RowVerdictCode
    detail: dict[str, Any] = field(default_factory=dict)
    evidence: EvidenceVerdict | None = None

    @property
    def row_eligible(self) -> bool:
        return self.verdict == "d2_filled_root"

    @property
    def eligible(self) -> bool:
        return self.row_eligible and self.evidence is not None and self.evidence.matches

    @property
    def already_reconciled(self) -> bool:
        return self.verdict == "already_reconciled"

    @property
    def client_order_id(self) -> str | None:
        value = self.detail.get("client_order_id")
        return value if isinstance(value, str) else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ledger_id": self.ledger_id,
            "verdict": self.verdict,
            "eligible": self.eligible,
            "detail": self.detail,
            "broker_evidence": None
            if self.evidence is None
            else self.evidence.as_dict(),
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
            raise D2RootReconcileInputError("--ids values must be strings")
        for token in value.split(","):
            if not _ID_TOKEN.fullmatch(token):
                raise D2RootReconcileInputError(
                    f"--ids token {token!r} is not an exact positive decimal id"
                )
            number = int(token)
            if number > _BIGINT_MAX:
                raise D2RootReconcileInputError(
                    f"--ids token {token!r} is out of range"
                )
            if number in ids:
                raise D2RootReconcileInputError(f"--ids repeats id {number}")
            ids.append(number)
    if not ids:
        raise D2RootReconcileInputError("--ids requires at least one id")
    if len(ids) > MAX_IDS:
        raise D2RootReconcileInputError(f"--ids accepts at most {MAX_IDS} ids")
    return tuple(ids)


def validate_text(name: str, value: Any, *, max_chars: int) -> str:
    """A required, bounded, single-line operator string (reason / actor)."""
    if not isinstance(value, str):
        raise D2RootReconcileInputError(f"{name} is required")
    text = value.strip()
    if not text:
        raise D2RootReconcileInputError(f"{name} must not be blank")
    if len(text) > max_chars:
        raise D2RootReconcileInputError(f"{name} exceeds {max_chars} characters")
    if any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in text):
        raise D2RootReconcileInputError(f"{name} must not contain control characters")
    return text


def assert_spot_demo_reader(client: Any) -> None:
    """The broker reader must be the Spot Demo account the D2 orders used.

    Checked before any read: a non-Spot-Demo host (live, testnet, futures) or a
    different credential refuses the batch without a single request.
    """
    base_url = getattr(client, "_base_url", None)
    host = urlsplit(str(base_url or "")).hostname or ""
    try:
        assert_spot_demo_host(host)
    except Exception:
        raise D2RootReconcileInputError(
            f"broker client host {host!r} is not the Spot Demo host"
        ) from None
    if getattr(client, "credential_fingerprint", None) != D2_CREDENTIAL_FINGERPRINT:
        raise D2RootReconcileInputError(
            "broker client credential is not the sealed D2 Spot Demo account"
        )


# ------------------------------------------------------------------ verdicts


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (DecimalException, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def evaluate_row(ledger_id: int, row: Any | None, instrument: Any | None) -> RowVerdict:
    """Pure eligibility verdict for one requested id, from the row alone.

    ``row`` is a ``BinanceDemoOrderLedger`` (or anything with the same
    attributes) or ``None`` when the id does not exist; ``instrument`` is its
    ``CryptoInstrument`` or ``None``.
    """
    if row is None:
        return RowVerdict(ledger_id, "not_found")
    metadata = row.extra_metadata if isinstance(row.extra_metadata, Mapping) else {}
    identity: dict[str, Any] = {
        "client_order_id": row.client_order_id,
        "broker_order_id": row.broker_order_id,
        "product": row.product,
        "venue_host": row.venue_host,
        "parent_client_order_id": row.parent_client_order_id,
        "lifecycle_state": row.lifecycle_state,
        "side": row.side,
        "order_type": row.order_type,
        "qty": _fmt(row.qty),
        "price": _fmt(row.price),
        "filled_at": row.filled_at.isoformat() if row.filled_at else None,
        "instrument_venue": getattr(instrument, "venue", None),
        "instrument_product": getattr(instrument, "product", None),
        "instrument_symbol": getattr(instrument, "venue_symbol", None),
        "writer": metadata.get("writer"),
        "d2_exception_id": metadata.get("d2_exception_id"),
        "remediation_id": metadata.get("remediation_id"),
        "operation_id": metadata.get("operation_id"),
    }
    audit = metadata.get(AUDIT_KEY)
    if (
        row.lifecycle_state == "reconciled"
        and isinstance(audit, Mapping)
        and audit.get("tool") == TOOL_NAME
    ):
        identity["prior_reconcile"] = {
            "batch_id": audit.get("batch_id"),
            "at": audit.get("at"),
            "actor": audit.get("actor"),
        }
        return RowVerdict(ledger_id, "already_reconciled", identity)
    if row.product != D2_PRODUCT:
        return RowVerdict(ledger_id, "not_spot", identity)
    if row.venue_host != D2_VENUE_HOST:
        return RowVerdict(ledger_id, "not_spot_demo_host", identity)
    if row.parent_client_order_id is not None:
        return RowVerdict(ledger_id, "not_root", identity)
    if row.lifecycle_state != "filled":
        return RowVerdict(ledger_id, "not_filled", identity)
    if metadata.get("writer") != WRITER_NAME:
        return RowVerdict(ledger_id, "not_d2_writer", identity)
    if metadata.get("d2_exception_id") != D2_EXCEPTION_ID:
        return RowVerdict(ledger_id, "exception_mismatch", identity)
    if metadata.get("remediation_id") != D2_REMEDIATION_ID:
        return RowVerdict(ledger_id, "remediation_mismatch", identity)
    if metadata.get("canary_or_strategy_use") != "forbidden":
        return RowVerdict(ledger_id, "canary_use_not_forbidden", identity)
    if metadata.get("credential_fingerprint") != D2_CREDENTIAL_FINGERPRINT:
        return RowVerdict(ledger_id, "credential_fingerprint_mismatch", identity)
    bound = _BOUND_BY_CID.get(row.client_order_id)
    if bound is None:
        return RowVerdict(ledger_id, "not_bound_order", identity)
    identity["bound_order"] = {
        "symbol": bound.symbol,
        **bound.request_params(),
    }
    if (
        instrument is None
        or instrument.venue != D2_VENUE
        or instrument.product != D2_PRODUCT
        or instrument.venue_symbol != bound.symbol
    ):
        return RowVerdict(ledger_id, "instrument_mismatch", identity)
    if row.side != bound.side:
        return RowVerdict(ledger_id, "side_mismatch", identity)
    if row.order_type != bound.order_type:
        return RowVerdict(ledger_id, "order_type_mismatch", identity)
    if row.qty != bound.quantity:
        return RowVerdict(ledger_id, "qty_mismatch", identity)
    if row.price != bound.price:
        return RowVerdict(ledger_id, "price_mismatch", identity)
    if broker_identifier_problem(row.broker_order_id, field="broker_order_id"):
        return RowVerdict(ledger_id, "broker_order_id_missing", identity)
    return RowVerdict(ledger_id, "d2_filled_root", identity)


def evaluate_evidence(
    row: Any, body: Mapping[str, Any] | BaseException
) -> EvidenceVerdict:
    """Pure verdict on one broker order read for an eligible D2 root.

    ``body`` is the redacted ``GET /api/v3/order`` response, or the exception
    the read raised. Only a positive, fully matching ``FILLED`` answer passes;
    absence, an unreadable answer and every mismatch refuse.
    """
    if isinstance(body, BinanceDemoOrderNotFound):
        return EvidenceVerdict("evidence_order_not_found")
    if isinstance(body, BaseException):
        return EvidenceVerdict(
            "evidence_read_failed", {"error_class": type(body).__name__}
        )
    if not isinstance(body, Mapping):
        return EvidenceVerdict("evidence_not_mapping")
    bound = _BOUND_BY_CID[row.client_order_id]
    metadata = row.extra_metadata if isinstance(row.extra_metadata, Mapping) else {}
    orig_qty = _decimal(body.get("origQty"))
    executed_qty = _decimal(body.get("executedQty"))
    price = _decimal(body.get("price"))
    detail: dict[str, Any] = {
        "clientOrderId": body.get("clientOrderId"),
        "orderId": body.get("orderId"),
        "symbol": body.get("symbol"),
        "side": body.get("side"),
        "type": body.get("type"),
        "status": body.get("status"),
        "origQty": _fmt(orig_qty),
        "executedQty": _fmt(executed_qty),
        "price": _fmt(price),
        "cummulativeQuoteQty": _fmt(_decimal(body.get("cummulativeQuoteQty"))),
        "timeInForce": body.get("timeInForce"),
        "time": body.get("time"),
        "updateTime": body.get("updateTime"),
    }
    raw_cid = body.get("clientOrderId")
    if (
        broker_identifier_problem(raw_cid, field="clientOrderId")
        or str(raw_cid) != row.client_order_id
    ):
        return EvidenceVerdict("evidence_client_order_id", detail)
    raw_oid = body.get("orderId")
    if broker_identifier_problem(raw_oid, field="orderId") or str(raw_oid) != str(
        row.broker_order_id
    ):
        return EvidenceVerdict("evidence_order_id", detail)
    if body.get("symbol") != bound.symbol:
        return EvidenceVerdict("evidence_symbol", detail)
    if body.get("side") != bound.side:
        return EvidenceVerdict("evidence_side", detail)
    if body.get("type") != bound.order_type:
        return EvidenceVerdict("evidence_type", detail)
    if body.get("status") != "FILLED":
        return EvidenceVerdict("evidence_status_not_filled", detail)
    if orig_qty != bound.quantity:
        return EvidenceVerdict("evidence_orig_qty", detail)
    if executed_qty != bound.quantity:
        return EvidenceVerdict("evidence_executed_qty", detail)
    if price != bound.price:
        return EvidenceVerdict("evidence_price", detail)
    if body.get("timeInForce") != bound.time_in_force:
        return EvidenceVerdict("evidence_time_in_force", detail)
    recorded_qty = metadata.get("filled_qty")
    if recorded_qty is not None and _decimal(recorded_qty) != executed_qty:
        return EvidenceVerdict("evidence_fill_actual_conflict", detail)
    return EvidenceVerdict("broker_filled_match", detail)


def decide(ids: Sequence[int], verdicts: Sequence[RowVerdict]) -> BatchStatus:
    """All eligible → eligible; all already reconciled → noop; else refused."""
    if (
        verdicts
        and len(verdicts) == len(ids)
        and all(v.already_reconciled for v in verdicts)
    ):
        return "noop"
    if verdicts and len(verdicts) == len(ids) and all(v.eligible for v in verdicts):
        return "eligible"
    return "refused"


# ----------------------------------------------------------------------- I/O


async def _evaluate(
    session: AsyncSession,
    client: OrderStatusReader,
    ids: Sequence[int],
    *,
    for_update: bool,
) -> tuple[RowVerdict, ...]:
    if len(set(ids)) != len(ids):
        raise D2RootReconcileInputError("ids must be unique")
    found = await BinanceDemoLedgerService(session).rows_with_instruments_by_ids(
        list(ids), for_update=for_update
    )
    verdicts: list[RowVerdict] = []
    for ledger_id in ids:
        row, instrument = found.get(ledger_id, (None, None))
        verdict = evaluate_row(ledger_id, row, instrument)
        if verdict.row_eligible:
            bound = _BOUND_BY_CID[row.client_order_id]
            try:
                body: Mapping[str, Any] | BaseException = await client.get_order_status(
                    symbol=bound.symbol, client_order_id=row.client_order_id
                )
            except Exception as exc:  # noqa: BLE001 - any failed read refuses
                body = exc
            verdict = RowVerdict(
                ledger_id,
                verdict.verdict,
                verdict.detail,
                evaluate_evidence(row, body),
            )
        verdicts.append(verdict)
    return tuple(verdicts)


async def preview_reconcile(
    session: AsyncSession, client: OrderStatusReader, ids: Sequence[int]
) -> BatchResult:
    """Read-only verdicts (ledger SELECTs + broker GETs). Writes nothing."""
    assert_spot_demo_reader(client)
    try:
        verdicts = await _evaluate(session, client, ids, for_update=False)
    finally:
        await session.rollback()
    return BatchResult(decide(ids, verdicts), tuple(ids), verdicts)


async def commit_reconcile(
    session: AsyncSession,
    client: OrderStatusReader,
    ids: Sequence[int],
    *,
    reason: str,
    actor: str,
    now: dt.datetime | None = None,
) -> BatchResult:
    """Reconcile exactly ``ids`` in one transaction, or change nothing.

    Rows are locked and re-verified under the lock, the broker is read for each
    one, and only then does every root move ``filled → closed → reconciled``
    through the ledger service, carrying its audit record. A refused or no-op
    batch rolls back without writing.
    """
    reason_text = validate_text("reason", reason, max_chars=MAX_REASON_CHARS)
    actor_text = validate_text("actor", actor, max_chars=MAX_ACTOR_CHARS)
    assert_spot_demo_reader(client)
    try:
        verdicts = await _evaluate(session, client, ids, for_update=True)
        status = decide(ids, verdicts)
        if status != "eligible":
            await session.rollback()
            return BatchResult(status, tuple(ids), verdicts)

        moment = now or dt.datetime.now(dt.UTC)
        batch_id = uuid.uuid4()
        ledger = BinanceDemoLedgerService(session)
        changed = 0
        for verdict in verdicts:
            client_order_id = verdict.client_order_id
            if client_order_id is None or verdict.evidence is None:
                raise D2RootReconcileConflictError(
                    f"ledger id {verdict.ledger_id} lost its verified identity"
                )
            audit = {
                "schema": AUDIT_SCHEMA,
                "tool": TOOL_NAME,
                "batch_id": str(batch_id),
                "ledger_id": verdict.ledger_id,
                "reason": reason_text,
                "actor": actor_text,
                "at": moment.isoformat(),
                "from_state": "filled",
                "to_state": "reconciled",
                "broker_evidence": verdict.evidence.detail,
            }
            await ledger.record_closed(
                client_order_id=client_order_id,
                now=moment,
                extra_metadata_merge={AUDIT_KEY: audit},
            )
            row = await ledger.record_reconciled(
                client_order_id=client_order_id, now=moment
            )
            if row.lifecycle_state == "reconciled":
                changed += 1
        if changed != len(ids):
            raise D2RootReconcileConflictError(
                f"reconciled {changed} roots, expected {len(ids)}"
            )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return BatchResult("committed", tuple(ids), verdicts, batch_id, changed)


__all__ = [
    "AUDIT_KEY",
    "AUDIT_SCHEMA",
    "MAX_ACTOR_CHARS",
    "MAX_IDS",
    "MAX_REASON_CHARS",
    "TOOL_NAME",
    "BatchResult",
    "D2RootReconcileConflictError",
    "D2RootReconcileInputError",
    "EvidenceVerdict",
    "OrderStatusReader",
    "RowVerdict",
    "assert_spot_demo_reader",
    "commit_reconcile",
    "decide",
    "evaluate_evidence",
    "evaluate_row",
    "parse_ids",
    "preview_reconcile",
    "validate_text",
]
