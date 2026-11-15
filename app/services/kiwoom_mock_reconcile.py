"""hk 4722 (3797 T-P3) — Kiwoom mock terminal-only reconcile kernel.

Design: ``docs/design/3797-kiwoom-mock-ledger.md`` §3.1–§3.3 and §6.1.

What it does: read the open/uncertain KR ``kiwoom_mock`` ledger rows, read the
broker's kt00009 (order status) and kt00007 (order detail) listings, classify
each row with the pure §3.2 table (``mock_reconcile``), and — in terminal-only
mode — apply **only** expired / cancelled / rejected and uncertain→accepted
through :class:`KiwoomMockOrderLedgerService`, the sole ledger writer.

What it never does (T-P4 owns these): book fills, write the execution ledger,
journals or P&L. ``filled``/``partial`` are reported as
``deferred_fill_booking`` and left untouched.

Broker reads are serial with a configurable minimum spacing (§3.1: mock TRs are
1 request/s). A truncated listing is never absence: it degrades every row to
``unknown`` for that run.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from app.services.brokers.kiwoom.mock_reconcile import (
    LedgerOrder,
    classify_verdict,
    evidence_from_payloads,
)
from app.services.kiwoom_mock_order_ledger_service import (
    ACCOUNT_MODE_KIWOOM_MOCK,
    KiwoomMockOrderLedgerService,
    KiwoomMockTransitionRefused,
    kr_trading_day,
)

logger = logging.getLogger(__name__)

__all__ = (
    "DEFAULT_MIN_REQUEST_INTERVAL_SECONDS",
    "TERMINAL_GATE_ENV",
    "KiwoomMockReconcileBroker",
    "KiwoomMockReconcileReader",
    "kiwoom_mock_reconcile_orders_impl",
    "terminal_gate_enabled",
)

#: Exact-value gate for terminal-only application (default off). The string must
#: be exactly ``true`` — mirroring the H5 alert gate, not pydantic bool parsing.
TERMINAL_GATE_ENV: Final = "KIWOOM_MOCK_TERMINAL_AUTO_RECONCILE_ENABLED"

#: Mock TR limit is one request per second and per market (§3.1).
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS: Final = 1.0

_TERMINAL_VERDICTS: Final = frozenset({"expired", "cancelled", "rejected"})


def terminal_gate_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """``False`` unless the gate env var is exactly ``true``."""
    source = os.environ if environ is None else environ
    return source.get(TERMINAL_GATE_ENV) == "true"


class KiwoomMockReconcileReader(Protocol):
    """The two reads of §3.1. ``None`` means the read failed (never absence)."""

    async def read_order_status(self) -> Mapping[str, Any] | None: ...

    async def read_order_detail(
        self, *, order_date: str
    ) -> Mapping[str, Any] | None: ...


class KiwoomMockReconcileBroker:
    """Default broker reader: the existing mock account client, no new transport."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _account(self) -> Any:
        from app.services.brokers.kiwoom.client import KiwoomMockClient
        from app.services.brokers.kiwoom.domestic_account import (
            KiwoomDomesticAccountClient,
        )

        if self._client is None:
            # Default-disabled: raises KiwoomConfigurationError when the mock
            # account gate or credentials are missing, before any network call.
            self._client = KiwoomMockClient.from_app_settings()
        return KiwoomDomesticAccountClient(self._client)

    async def read_order_status(self) -> Mapping[str, Any] | None:
        return await self._account().get_order_status()

    async def read_order_detail(self, *, order_date: str) -> Mapping[str, Any] | None:
        # kt00007 qry_tp "1" = 주문순: every order row for that order date.
        return await self._account().get_order_detail(order_date=order_date, qry_tp="1")


@dataclass(frozen=True, slots=True)
class _Candidate:
    ledger_id: int
    client_order_id: str
    status: str
    broker_order_id: str | None
    quantity: int
    symbol: str
    side: str | None
    trading_day: str


async def _read(
    reader: KiwoomMockReconcileReader,
    call: Callable[[], Awaitable[Mapping[str, Any] | None]],
    *,
    label: str,
) -> Mapping[str, Any] | None:
    try:
        return await call()
    except Exception as exc:  # noqa: BLE001 — the evidence is the exception class
        logger.warning(
            "kiwoom mock reconcile %s read failed: %s", label, type(exc).__name__
        )
        return None


async def kiwoom_mock_reconcile_orders_impl(
    *,
    dry_run: bool = True,
    terminal_only: bool = False,
    limit: int = 100,
    min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
    session_factory: Callable[[], Any] | None = None,
    reader: KiwoomMockReconcileReader | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify and (terminal-only) close Kiwoom mock KR orders.

    ``dry_run`` issues zero writes. ``terminal_only`` applies closed-outcome
    transitions only and leaves fills deferred. They are mutually exclusive.
    """
    if terminal_only and dry_run:
        raise ValueError("terminal_only requires dry_run=False")
    if min_request_interval_seconds < 0:
        raise ValueError("min_request_interval_seconds must be >= 0")
    if terminal_only and not terminal_gate_enabled():
        # Defence in depth next to the CLI check: never reach the broker without
        # the operator gate.
        return {
            "success": False,
            "error": "terminal_gate_disabled",
            "error_class": "terminal_gate_disabled",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            "market": "kr",
            "scope": "terminal_only",
        }

    if session_factory is None:
        from app.core.db import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    if sleep is None:
        sleep = asyncio.sleep
    reader = reader or KiwoomMockReconcileBroker()

    # ---- read the ledger candidates (one short session) -------------------
    try:
        async with session_factory() as db:
            service = KiwoomMockOrderLedgerService(db)
            rows, open_total = await service.list_reconcile_candidates(limit=limit)
            candidates = [
                _Candidate(
                    ledger_id=row.id,
                    client_order_id=row.client_order_id,
                    status=row.status,
                    broker_order_id=row.broker_order_id,
                    quantity=int(row.quantity or 0),
                    symbol=row.symbol,
                    side=row.side,
                    trading_day=row.trading_day,
                )
                for row in rows
            ]
            acknowledged_cancels = await service.list_acknowledged_cancels(
                original_order_ids={
                    c.broker_order_id for c in candidates if c.broker_order_id
                }
            )
    except Exception as exc:
        logger.exception("Failed to list kiwoom mock ledger candidates: %s", exc)
        return {
            "success": False,
            "error": type(exc).__name__,
            "error_class": type(exc).__name__,
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            "market": "kr",
            "scope": "terminal_only" if terminal_only else "dry_run",
        }

    today = kr_trading_day(now or datetime.now(UTC))
    days = sorted({c.trading_day for c in candidates})
    scan = {
        "open_total": open_total,
        "scanned": len(candidates),
        "limit": limit,
        "truncated": open_total > len(candidates),
    }
    if not candidates:
        return {
            "success": True,
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            "market": "kr",
            "dry_run": dry_run,
            "scope": "terminal_only" if terminal_only else "dry_run",
            "counts": {},
            "reconciled": [],
            "candidate_scan": scan,
            "unresolved_count": 0,
            "message": (
                "No open candidates (all ledger rows terminal) — nothing to "
                f"reconcile (dry_run={dry_run})"
            ),
        }

    # ---- broker reads: serial, spaced (mock TR limit) ----------------------
    request_count = 0

    async def _spaced(then: Callable[[], Awaitable[Mapping[str, Any] | None]]):
        nonlocal request_count
        if request_count:
            await sleep(min_request_interval_seconds)
        request_count += 1
        return await then()

    status_payload: Mapping[str, Any] | None = None
    if today in days:
        status_payload = await _spaced(
            lambda: _read(reader, reader.read_order_status, label="kt00009")
        )

    detail_payloads: dict[str, Mapping[str, Any] | None] = {}
    for day in days:
        detail_payloads[day] = await _spaced(
            lambda day=day: _read(
                reader,
                lambda: reader.read_order_detail(order_date=day),
                label=f"kt00007:{day}",
            )
        )

    # ---- classify + apply --------------------------------------------------
    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    unresolved_count = 0

    async def _write_transition(
        candidate: _Candidate, *, action: str, reason: str
    ) -> str:
        """Apply one accepted terminal transition; report what happened."""
        async with session_factory() as db:
            service = KiwoomMockOrderLedgerService(db)
            try:
                if action == "promote_accepted":
                    await service.promote_uncertain_to_accepted(
                        ledger_id=candidate.ledger_id,
                        broker_order_id=str(candidate.broker_order_id),
                        detail={"verdict": "accepted", "reason": reason},
                    )
                    return "promoted_accepted"
                status = action.removeprefix("mark_")
                await service.mark_terminal(
                    ledger_id=candidate.ledger_id,
                    status=status,
                    reason=reason,
                    detail={"verdict": status},
                )
                return action
            except KiwoomMockTransitionRefused as exc:
                # A concurrent/earlier close is harmless: the row already moved.
                logger.info(
                    "kiwoom mock transition refused id=%s: %s",
                    candidate.ledger_id,
                    exc.reason,
                )
                return "noop_already_terminal"

    for candidate in candidates:
        is_today = candidate.trading_day == today
        evidence = evidence_from_payloads(
            order_status=status_payload if is_today else None,
            order_detail=detail_payloads.get(candidate.trading_day),
            consult_order_status=is_today,
        )
        verdict = classify_verdict(
            ledger=LedgerOrder(
                client_order_id=candidate.client_order_id,
                status=candidate.status,
                broker_order_id=candidate.broker_order_id,
                quantity=candidate.quantity,
                symbol=candidate.symbol,
            ),
            evidence=evidence,
            own_cancel_acknowledged=(candidate.broker_order_id in acknowledged_cancels),
        )

        outcome: dict[str, Any] = {
            "ledger_id": candidate.ledger_id,
            "client_order_id": candidate.client_order_id,
            "broker_order_id": candidate.broker_order_id,
            "symbol": candidate.symbol,
            "side": candidate.side,
            "status": candidate.status,
            "verdict": verdict.verdict,
            "reason": verdict.reason,
        }
        if verdict.filled_quantity is not None:
            outcome["filled_qty"] = verdict.filled_quantity
        if verdict.average_price is not None:
            outcome["avg_price"] = verdict.average_price

        if verdict.verdict in _TERMINAL_VERDICTS:
            if dry_run:
                outcome["action"] = f"would_mark_{verdict.verdict}"
            else:
                outcome["action"] = await _write_transition(
                    candidate,
                    action=f"mark_{verdict.verdict}",
                    reason=verdict.reason,
                )
        elif verdict.verdict == "accepted" and candidate.status == "uncertain":
            if dry_run:
                outcome["action"] = "would_promote_accepted"
            else:
                outcome["action"] = await _write_transition(
                    candidate, action="promote_accepted", reason=verdict.reason
                )
        elif verdict.verdict in {"filled", "partial"}:
            # T-P4 books fills. Terminal-only never does (design §3.3).
            outcome["action"] = "deferred_fill_booking"
            outcome["requires_session_reconcile"] = True
        elif verdict.verdict == "pending":
            outcome["action"] = "noop_pending"
        else:
            outcome["action"] = "noop_no_evidence"
            outcome["requires_manual_review"] = True

        # Work still owed after this run: a fill to book (T-P4) or a row a
        # human must resolve. An already-terminal no-op is resolved.
        if outcome["action"] in {"deferred_fill_booking", "noop_no_evidence"}:
            unresolved_count += 1

        counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
        reconciled.append(outcome)

    if scan["truncated"]:
        # A capped scan leaves rows unexamined: report the run as work remaining.
        unresolved_count += 1

    return {
        "success": True,
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        "market": "kr",
        "dry_run": dry_run,
        "scope": "terminal_only" if terminal_only else "dry_run",
        "counts": counts,
        "reconciled": reconciled,
        "candidate_scan": scan,
        "unresolved_count": unresolved_count,
        "message": (
            f"Reconciled {len(reconciled)} kiwoom mock order(s) "
            f"(dry_run={dry_run}, terminal_only={terminal_only}): {counts}"
        ),
    }
