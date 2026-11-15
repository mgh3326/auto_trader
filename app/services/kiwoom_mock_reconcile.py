"""hk 4722 / hk 4734 (3797 T-P3, T-P4) — Kiwoom mock reconcile kernel.

Design: ``docs/design/3797-kiwoom-mock-ledger.md`` §3.1–§3.4 and §6.1.

What it does: read the open/uncertain KR ``kiwoom_mock`` ledger rows, read the
broker's kt00009 (order status) and kt00007 (order detail) listings — following
``cont_yn``/``next_key`` pages — classify each row with the pure §3.2 table
(``mock_reconcile``), and apply the verdicts through
:class:`KiwoomMockOrderLedgerService`, the sole ledger writer.

Modes (design §3.3):

* dry run — classify only, zero writes;
* terminal-only — expired / cancelled / rejected and uncertain→accepted;
  ``filled``/``partial`` are reported as ``deferred_fill_booking``;
* full mode — the same, plus the broker-evidence fill delta booked into
  ``execution_ledger`` (through :class:`ExecutionLedgerRepository`) and its
  trade row / journal written with ``account='kiwoom_mock'`` at the KIS
  kernel's booking boundary, and one reconcile-run row with
  ``broker='kiwoom'``.

Writes require exact-true gates, checked before any ledger read or broker
call: ``KIWOOM_MOCK_TERMINAL_AUTO_RECONCILE_ENABLED`` for terminal-only, and
both full-mode switches for full mode. Broker reads are serial with a
configurable minimum spacing (§3.1: mock TRs are 1 request/s). An incomplete,
truncated or page-capped listing is never absence: it degrades every row to
``unknown`` for that run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Protocol

from app.schemas.execution_ledger import ReconcileRunRecord
from app.services.brokers.kiwoom.mock_reconcile import (
    BrokerEvidence,
    LedgerOrder,
    classify_verdict,
    evidence_from_payloads,
    return_code_ok,
)
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.kiwoom_mock_order_ledger_service import (
    ACCOUNT_MODE_KIWOOM_MOCK,
    FillBooking,
    KiwoomMockOrderLedgerService,
    KiwoomMockTransitionRefused,
    kr_trading_day,
)

logger = logging.getLogger(__name__)

__all__ = (
    "DEFAULT_MAX_LISTING_PAGES",
    "DEFAULT_MIN_REQUEST_INTERVAL_SECONDS",
    "FULL_GATE_ENVS",
    "JOURNAL_ACCOUNT",
    "JOURNAL_ACCOUNT_TYPE",
    "TERMINAL_GATE_ENV",
    "KiwoomMockReconcileBroker",
    "KiwoomMockReconcileReader",
    "full_gate_enabled",
    "kiwoom_mock_reconcile_orders_impl",
    "terminal_gate_enabled",
)

#: Exact-value gate for terminal-only application (default off). The string must
#: be exactly ``true`` — mirroring the H5 alert gate, not pydantic bool parsing.
TERMINAL_GATE_ENV: Final = "KIWOOM_MOCK_TERMINAL_AUTO_RECONCILE_ENABLED"

#: Full mode needs both switches exactly ``true`` (design §3.3): the feature
#: switch and the safety-review acknowledgement.
FULL_GATE_ENVS: Final = (
    "KIWOOM_MOCK_AUTO_RECONCILE_ENABLED",
    "KIWOOM_MOCK_AUTO_RECONCILE_SAFETY_REVIEW_PASSED",
)

#: Every booked Kiwoom mock fill journals under this account label (operator
#: decision Q3, hk:doc decision/2026-11-15/at-3796-3797-design-answers). The
#: journal's ``account_type`` is ``mock`` — the account's own type.
JOURNAL_ACCOUNT: Final = "kiwoom_mock"
JOURNAL_ACCOUNT_TYPE: Final = "mock"

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


def full_gate_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """``False`` unless *both* full-mode gates are exactly ``true``."""
    source = os.environ if environ is None else environ
    return all(source.get(name) == "true" for name in FULL_GATE_ENVS)


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
    #: Journal inputs and provenance carried from the same row snapshot.
    instrument_type: str = "equity_kr"
    currency: str = "KRW"
    correlation_id: str | None = None
    thesis: str | None = None
    strategy: str | None = None
    reason: str | None = None
    exit_reason: str | None = None
    notes: str | None = None
    indicators_snapshot: dict[str, Any] | None = None
    journal_id: int | None = None
    trade_date: datetime | None = None


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


def _pending_journal_price(booking: FillBooking, pending: Decimal) -> Decimal | None:
    """Price of the still-unjournaled quantity, from the cumulative notionals."""
    if pending <= 0:
        return None
    if booking.journaled_qty <= 0:
        return booking.cumulative_avg
    notional = (
        booking.cumulative_qty * booking.cumulative_avg
        - booking.journaled_qty * booking.journaled_avg
    )
    return notional / pending


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
    """Classify and apply Kiwoom mock KR order verdicts.

    ``dry_run`` issues zero writes. ``terminal_only`` applies closed-outcome
    transitions only and leaves fills deferred. Full mode
    (``dry_run=False, terminal_only=False``) also books the broker-evidence
    fill delta into ``execution_ledger`` and journals it under
    ``account='kiwoom_mock'``. Every write path needs its exact-true operator
    gate(s): ``KIWOOM_MOCK_TERMINAL_AUTO_RECONCILE_ENABLED`` for terminal-only
    and both full-mode switches for full mode.
    """
    if terminal_only and dry_run:
        raise ValueError("terminal_only requires dry_run=False")
    if min_request_interval_seconds < 0:
        raise ValueError("min_request_interval_seconds must be >= 0")
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")
    full_mode = not dry_run and not terminal_only
    scope = "dry_run" if dry_run else ("terminal_only" if terminal_only else "full")
    if not dry_run:
        # Gate on the write condition itself, not on the caller's flag, and
        # before any ledger read or broker call (design §3.3).
        if terminal_only and not terminal_gate_enabled():
            return {
                "success": False,
                "error": "terminal_gate_disabled",
                "error_class": "terminal_gate_disabled",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
                "market": "kr",
                "scope": scope,
            }
        if full_mode and not full_gate_enabled():
            return {
                "success": False,
                "error": "full_gate_disabled",
                "error_class": "full_gate_disabled",
                "required_env": list(FULL_GATE_ENVS),
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
            rows, open_total = await service.list_reconcile_candidates(
                limit=limit, include_journal_recovery=full_mode
            )
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
                    instrument_type=row.instrument_type,
                    currency=row.currency,
                    correlation_id=row.correlation_id,
                    thesis=row.thesis,
                    strategy=row.strategy,
                    reason=row.reason,
                    exit_reason=row.exit_reason,
                    notes=row.notes,
                    indicators_snapshot=row.indicators_snapshot,
                    journal_id=row.journal_id,
                    trade_date=row.trade_date,
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
    booked_count = 0
    already_booked_count = 0
    run_now = now or datetime.now(UTC)
    run_started_at = run_now
    run_id = uuid.uuid4()

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

    async def _book_journal(
        db: Any, booking: FillBooking, *, quantity: Decimal, price: Decimal
    ) -> dict[str, Any]:
        """Write the trade row and journal inside the caller's transaction.

        Same helpers and FIFO semantics as the KIS live kernel; only the account
        label differs (``account='kiwoom_mock'``, ``account_type='mock'``). The
        trade row is the order's cumulative aggregate (a later partial advances
        it instead of being dropped by the unique key), the buy journal is the
        position entry, and a sell closes ``quantity`` of FIFO lots at ``price``.
        Nothing here commits: the caller commits this together with the journal
        progress marker, so a retry can neither repeat a FIFO close nor leave a
        draft journal behind. Imported lazily so the reconcile module stays
        light for the CLI.
        """
        from app.mcp_server.tooling.order_journal import (
            _close_journals_on_sell,
            _create_trade_journal_for_buy,
            _link_journal_to_fill,
            _record_trade_for_fill,
        )

        trade_id = await _record_trade_for_fill(
            db,
            symbol=booking.symbol,
            instrument_type=booking.instrument_type,
            side=str(booking.side),
            price=float(booking.cumulative_avg),
            quantity=float(booking.cumulative_qty),
            total_amount=float(booking.cumulative_qty * booking.cumulative_avg),
            fee=0.0,
            currency=booking.currency,
            account=JOURNAL_ACCOUNT,
            order_id=booking.broker_order_id,
        )
        details: dict[str, Any] = {"trade_id": trade_id}
        journal_id = booking.journal_id
        if booking.side == "buy":
            if journal_id is None:
                cumulative = float(booking.cumulative_qty)
                result = await _create_trade_journal_for_buy(
                    symbol=booking.symbol,
                    market_type=booking.instrument_type,
                    preview={
                        "price": float(booking.cumulative_avg),
                        "quantity": cumulative,
                        "estimated_value": float(
                            booking.cumulative_qty * booking.cumulative_avg
                        ),
                    },
                    thesis=(booking.thesis or "").strip() or "reconciled fill",
                    strategy=(booking.strategy or "").strip() or "reconciled fill",
                    target_price=None,
                    stop_loss=None,
                    min_hold_days=None,
                    notes=booking.notes,
                    indicators_snapshot=booking.indicators_snapshot,
                    account_type=JOURNAL_ACCOUNT_TYPE,
                    account=JOURNAL_ACCOUNT,
                    correlation_id=booking.correlation_id,
                    db=db,
                )
                journal_id = result.get("journal_id")
                details["journal_created"] = journal_id is not None
                if journal_id is not None:
                    await _link_journal_to_fill(
                        booking.symbol,
                        int(trade_id),
                        account_type=JOURNAL_ACCOUNT_TYPE,
                        account=JOURNAL_ACCOUNT,
                        db=db,
                    )
            details["journal_id"] = journal_id
        else:
            close = await _close_journals_on_sell(
                symbol=booking.symbol,
                sell_quantity=float(quantity),
                sell_price=float(price),
                exit_reason=booking.exit_reason or booking.reason,
                account_type=JOURNAL_ACCOUNT_TYPE,
                account=JOURNAL_ACCOUNT,
                db=db,
            )
            details["journals_closed"] = close["journals_closed"]
            details["closed_journal_ids"] = close["closed_ids"]
            details["realized_pnl_pct"] = close["total_pnl_pct"]
            details["realized_pnl_basis"] = close.get(
                "realized_pnl_basis", "journal_entry"
            )
            details["journal_pnl_pct"] = close["total_pnl_pct"]

        return details

    async def _apply_fill(
        candidate: _Candidate, verdict: Any, outcome: dict[str, Any]
    ) -> None:
        """Book the broker-evidence delta and its journal (full mode only)."""
        nonlocal booked_count, already_booked_count
        if verdict.filled_quantity is None or verdict.average_price is None:
            # The classifier never emits filled/partial without both; refuse to
            # invent either value.
            outcome["action"] = "noop_no_evidence"
            outcome["requires_manual_review"] = True
            return
        try:
            async with session_factory() as db:
                booking = await KiwoomMockOrderLedgerService(db).record_fill(
                    ledger_id=candidate.ledger_id,
                    filled_quantity=int(verdict.filled_quantity),
                    average_price=int(verdict.average_price),
                    observed_at=run_now,
                )
        except KiwoomMockTransitionRefused as exc:
            logger.info(
                "kiwoom mock fill refused id=%s: %s", candidate.ledger_id, exc.reason
            )
            outcome["action"] = "noop_transition_refused"
            outcome["transition_refused"] = exc.reason
            outcome["requires_manual_review"] = True
            return

        outcome["action"] = booking.action
        outcome["delta_qty"] = str(booking.delta_qty)
        outcome["booked_cumulative_qty"] = str(booking.cumulative_qty)
        if booking.execution_ledger_row_id is not None:
            outcome["execution_ledger_row_id"] = booking.execution_ledger_row_id
        if booking.fill_seq is not None:
            outcome["fill_seq"] = booking.fill_seq
        if booking.action == "noop_already_booked":
            already_booked_count += 1
        else:
            booked_count += 1

        # The fill evidence is committed; the journal is a projection. Anything
        # still unjournaled is finished here — including a remainder recovered
        # after an earlier failure (the row may already be ``filled``), which is
        # why the quantity comes from the journal progress, not only from this
        # run's delta. A sell is never re-closed for an already journaled
        # quantity (that would consume the FIFO lot twice).
        pending_qty = booking.cumulative_qty - booking.journaled_qty
        if pending_qty <= 0:
            return
        pending_avg = _pending_journal_price(booking, pending_qty)
        if pending_avg is None or pending_avg <= 0:
            outcome["journal_error"] = "pending_price_unavailable"
            outcome["requires_manual_review"] = True
            return
        try:
            # One transaction for the trade row, the journal side effects and
            # the progress marker: a failure rolls all of them back, so the next
            # run repeats the step exactly once (no double FIFO close, no second
            # draft journal). The fill evidence itself is already committed.
            async with session_factory() as db:
                details = await _book_journal(
                    db, booking, quantity=pending_qty, price=pending_avg
                )
                await KiwoomMockOrderLedgerService(db).attach_journal(
                    ledger_id=booking.ledger_id,
                    trade_id=details.get("trade_id"),
                    journal_id=details.get("journal_id"),
                    journaled_qty=booking.cumulative_qty,
                    journaled_avg=booking.cumulative_avg,
                    commit=False,
                )
                await db.commit()
            outcome["journal"] = details
        except Exception as exc:  # noqa: BLE001 — surface the class, never hide it
            logger.exception(
                "kiwoom mock journal booking failed id=%s", candidate.ledger_id
            )
            outcome["journal_error"] = type(exc).__name__
            outcome["requires_manual_review"] = True

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
            if full_mode:
                await _apply_fill(candidate, verdict, outcome)
            else:
                # Terminal-only never books fills (design §3.3).
                outcome["action"] = "deferred_fill_booking"
                outcome["requires_session_reconcile"] = True
        elif verdict.verdict == "pending":
            outcome["action"] = "noop_pending"
        else:
            outcome["action"] = "noop_no_evidence"
            outcome["requires_manual_review"] = True

        # Work still owed after this run: an unbooked fill, a journal that failed
        # after its fill was booked, or a row a human must resolve. A successful
        # close or booking is resolved.
        if outcome["action"] in {
            "deferred_fill_booking",
            "noop_no_evidence",
            "noop_transition_refused",
        }:
            unresolved_count += 1
        elif outcome.get("requires_manual_review"):
            unresolved_count += 1

        counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
        reconciled.append(outcome)

    if scan["truncated"]:
        # A capped scan leaves rows unexamined: report the run as work remaining.
        unresolved_count += 1

    run_record: dict[str, Any] | None = None
    if full_mode:
        window_start = min(
            (c.trade_date for c in candidates if c.trade_date is not None),
            default=run_now,
        )
        try:
            async with session_factory() as db:
                ExecutionLedgerRepository(db).record_run(
                    ReconcileRunRecord(
                        run_id=run_id,
                        broker="kiwoom",
                        window_start=window_start,
                        window_end=run_now,
                        started_at=run_started_at,
                        finished_at=datetime.now(UTC),
                        dry_run=False,
                        committed_insert=booked_count,
                        unchanged=already_booked_count,
                        notes="kiwoom_mock_full",
                    )
                )
                await db.commit()
            run_record = {
                "recorded": True,
                "run_id": str(run_id),
                "window_start": window_start.isoformat(),
                "committed_insert": booked_count,
                "unchanged": already_booked_count,
            }
        except Exception as exc:  # noqa: BLE001 — booked rows are already committed
            logger.exception("kiwoom mock reconcile run row was not recorded")
            run_record = {"recorded": False, "error_class": type(exc).__name__}

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
        "reconcile_run": run_record,
        "message": (
            f"Reconciled {len(reconciled)} kiwoom mock order(s) "
            f"(dry_run={dry_run}, scope={scope}): {counts}"
        ),
    }
