"""hk 4722 (3797 T-P3) — Kiwoom mock terminal-only reconcile kernel.

Design: ``docs/design/3797-kiwoom-mock-ledger.md`` §3.1–§3.3 and §6.1.

What it does: read the open/uncertain KR ``kiwoom_mock`` ledger rows, read the
broker's kt00009 (order status) and kt00007 (order detail) listings — following
``cont_yn``/``next_key`` pages — classify each row with the pure §3.2 table
(``mock_reconcile``), and — in terminal-only mode — apply **only** expired /
cancelled / rejected and uncertain→accepted through
:class:`KiwoomMockOrderLedgerService`, the sole ledger writer.

What it never does (T-P4 owns these): book fills, write the execution ledger,
journals or P&L. ``filled``/``partial`` are reported as
``deferred_fill_booking`` and left untouched.

Writes require the operator gate, not just ``terminal_only``: ``dry_run=False``
without ``terminal_only`` is rejected (there is no full mode in T-P3) and the
gate is checked whenever a write is possible. Broker reads are serial with a
configurable minimum spacing (§3.1: mock TRs are 1 request/s). An incomplete,
truncated or page-capped listing is never absence: it degrades every row to
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
    BrokerEvidence,
    LedgerOrder,
    classify_verdict,
    evidence_from_payloads,
    return_code_ok,
)
from app.services.kiwoom_mock_order_ledger_service import (
    ACCOUNT_MODE_KIWOOM_MOCK,
    KiwoomMockOrderLedgerService,
    KiwoomMockTransitionRefused,
    kr_trading_day,
)

logger = logging.getLogger(__name__)

__all__ = (
    "DEFAULT_MAX_LISTING_PAGES",
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

#: Page cap for one listing. A listing still continuing after this many pages is
#: reported incomplete (``*_page_cap_reached``) instead of silently truncated.
DEFAULT_MAX_LISTING_PAGES: Final = 20

_TERMINAL_VERDICTS: Final = frozenset({"expired", "cancelled", "rejected"})


def terminal_gate_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """``False`` unless the gate env var is exactly ``true``."""
    source = os.environ if environ is None else environ
    return source.get(TERMINAL_GATE_ENV) == "true"


class KiwoomMockReconcileReader(Protocol):
    """The two paged reads of §3.1. ``None`` means the read failed (never absence)."""

    async def read_order_status(
        self, *, cont_yn: str | None = None, next_key: str | None = None
    ) -> Mapping[str, Any] | None: ...

    async def read_order_detail(
        self,
        *,
        order_date: str,
        cont_yn: str | None = None,
        next_key: str | None = None,
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

    async def read_order_status(
        self, *, cont_yn: str | None = None, next_key: str | None = None
    ) -> Mapping[str, Any] | None:
        return await self._account().get_order_status(
            cont_yn=cont_yn, next_key=next_key
        )

    async def read_order_detail(
        self,
        *,
        order_date: str,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> Mapping[str, Any] | None:
        # kt00007 qry_tp "1" = 주문순: every order row for that order date.
        return await self._account().get_order_detail(
            order_date=order_date,
            qry_tp="1",
            cont_yn=cont_yn,
            next_key=next_key,
        )


@dataclass(frozen=True, slots=True)
class _Candidate:
    ledger_id: int
    client_order_id: str
    status: str
    broker_order_id: str | None
    quantity: int
    recorded_filled_quantity: int
    symbol: str
    side: str | None
    trading_day: str


@dataclass(frozen=True, slots=True)
class _Listing:
    """One merged listing (all pages) or the reason it is incomplete."""

    payload: Mapping[str, Any] | None
    reason: str | None


def _merged_listing(list_key: str, rows: list[Any]) -> Mapping[str, Any]:
    """Build one complete listing out of every page's actual broker rows."""
    return {
        "return_code": 0,
        "continuation": {"cont_yn": "N", "next_key": ""},
        list_key: rows,
    }


def _evidence_for(*, detail: _Listing, status: _Listing | None) -> BrokerEvidence:
    """Evidence from the consulted listings; an incomplete one short-circuits."""
    if status is not None and status.reason is not None:
        return BrokerEvidence(complete=False, reason=status.reason, rows=())
    if detail.reason is not None:
        return BrokerEvidence(complete=False, reason=detail.reason, rows=())
    return evidence_from_payloads(
        order_status=status.payload if status is not None else None,
        order_detail=detail.payload,
        consult_order_status=status is not None,
    )


async def kiwoom_mock_reconcile_orders_impl(
    *,
    dry_run: bool = True,
    terminal_only: bool = False,
    limit: int = 100,
    min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
    max_pages: int = DEFAULT_MAX_LISTING_PAGES,
    session_factory: Callable[[], Any] | None = None,
    reader: KiwoomMockReconcileReader | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify and (terminal-only) close Kiwoom mock KR orders.

    ``dry_run`` issues zero writes. ``terminal_only`` applies closed-outcome
    transitions only and leaves fills deferred. Any write path needs both
    ``terminal_only=True`` and the exact-true operator gate.
    """
    if not dry_run and not terminal_only:
        # T-P3 has no full mode. Refusing here (instead of silently writing in a
        # non-terminal scope) keeps the gate impossible to bypass.
        raise ValueError(
            "full-mode booking is not implemented (T-P4); pass terminal_only=True"
        )
    if terminal_only and dry_run:
        raise ValueError("terminal_only requires dry_run=False")
    if min_request_interval_seconds < 0:
        raise ValueError("min_request_interval_seconds must be >= 0")
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")
    scope = "dry_run" if dry_run else "terminal_only"
    if not dry_run and not terminal_gate_enabled():
        # Gate on the write condition itself, not on the caller's flag.
        return {
            "success": False,
            "error": "terminal_gate_disabled",
            "error_class": "terminal_gate_disabled",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            "market": "kr",
            "scope": scope,
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
                    recorded_filled_quantity=int(row.filled_qty or 0),
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
            "scope": scope,
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
            "scope": scope,
            "counts": {},
            "reconciled": [],
            "candidate_scan": scan,
            "unresolved_count": 0,
            "message": (
                "No open candidates (all ledger rows terminal) — nothing to "
                f"reconcile (dry_run={dry_run})"
            ),
        }

    # ---- broker reads: serial, spaced, paged (mock TR limit) --------------
    request_count = 0

    async def _spaced(
        call: Callable[[], Awaitable[Mapping[str, Any] | None]], label: str
    ) -> Mapping[str, Any] | None:
        nonlocal request_count
        if request_count:
            await sleep(min_request_interval_seconds)
        request_count += 1
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001 — the evidence is the exception class
            logger.warning(
                "kiwoom mock reconcile %s read failed: %s", label, type(exc).__name__
            )
            return None

    async def _collect(
        fetch: Callable[[str | None, str | None], Awaitable[Mapping[str, Any] | None]],
        *,
        list_key: str,
        label: str,
    ) -> _Listing:
        rows: list[Any] = []
        cont_yn: str | None = None
        next_key: str | None = None
        for _ in range(max_pages):
            payload = await _spaced(lambda c=cont_yn, k=next_key: fetch(c, k), label)
            if payload is None:
                return _Listing(None, f"{label}_unreadable")
            if not return_code_ok(payload):
                return _Listing(None, f"{label}_broker_error")
            page_rows = payload.get(list_key)
            continuation = payload.get("continuation")
            if not isinstance(page_rows, list) or not isinstance(continuation, Mapping):
                return _Listing(None, f"{label}_unparseable")
            rows.extend(page_rows)
            token = str(continuation.get("cont_yn") or "").strip().upper()
            if token in {"", "N"}:
                return _Listing(_merged_listing(list_key, rows), None)
            cont_yn = str(continuation.get("cont_yn") or "")
            next_key = str(continuation.get("next_key") or "")
        return _Listing(None, f"{label}_page_cap_reached")

    status_listing: _Listing | None = None
    if today in days:
        status_listing = await _collect(
            lambda cont_yn, next_key: reader.read_order_status(
                cont_yn=cont_yn, next_key=next_key
            ),
            list_key="acnt_ord_cntr_prst_array",
            label="order_status",
        )

    detail_listings: dict[str, _Listing] = {}
    for day in days:
        detail_listings[day] = await _collect(
            lambda cont_yn, next_key, day=day: reader.read_order_detail(
                order_date=day, cont_yn=cont_yn, next_key=next_key
            ),
            list_key="acnt_ord_cntr_prps_dtl",
            label="order_detail",
        )

    # ---- classify + apply --------------------------------------------------
    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    unresolved_count = 0

    async def _write_transition(
        candidate: _Candidate, *, action: str, reason: str
    ) -> tuple[str, str | None]:
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
                    return "promoted_accepted", None
                status = action.removeprefix("mark_")
                await service.mark_terminal(
                    ledger_id=candidate.ledger_id,
                    status=status,
                    reason=reason,
                    detail={"verdict": status},
                )
                return action, None
            except KiwoomMockTransitionRefused as exc:
                # The snapshot moved (or the row is not closable). Never report
                # a close that did not happen; surface it for review.
                logger.info(
                    "kiwoom mock transition refused id=%s: %s",
                    candidate.ledger_id,
                    exc.reason,
                )
                return "noop_transition_refused", exc.reason

    for candidate in candidates:
        is_today = candidate.trading_day == today
        evidence = _evidence_for(
            detail=detail_listings[candidate.trading_day],
            status=status_listing if is_today else None,
        )
        verdict = classify_verdict(
            ledger=LedgerOrder(
                client_order_id=candidate.client_order_id,
                status=candidate.status,
                broker_order_id=candidate.broker_order_id,
                quantity=candidate.quantity,
                symbol=candidate.symbol,
                recorded_filled_quantity=candidate.recorded_filled_quantity,
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
                action, refused = await _write_transition(
                    candidate,
                    action=f"mark_{verdict.verdict}",
                    reason=verdict.reason,
                )
                outcome["action"] = action
                if refused is not None:
                    outcome["transition_refused"] = refused
                    outcome["requires_manual_review"] = True
        elif verdict.verdict == "accepted" and candidate.status == "uncertain":
            if dry_run:
                outcome["action"] = "would_promote_accepted"
            else:
                action, refused = await _write_transition(
                    candidate, action="promote_accepted", reason=verdict.reason
                )
                outcome["action"] = action
                if refused is not None:
                    outcome["transition_refused"] = refused
                    outcome["requires_manual_review"] = True
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
        # human must resolve. A successful close is resolved.
        if outcome["action"] in {
            "deferred_fill_booking",
            "noop_no_evidence",
            "noop_transition_refused",
        }:
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
        "scope": scope,
        "counts": counts,
        "reconciled": reconciled,
        "candidate_scan": scan,
        "unresolved_count": unresolved_count,
        "message": (
            f"Reconciled {len(reconciled)} kiwoom mock order(s) "
            f"(dry_run={dry_run}, terminal_only={terminal_only}): {counts}"
        ),
    }
