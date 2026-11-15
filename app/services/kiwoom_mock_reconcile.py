"""hk 4722 / hk 4734 (3797 T-P3, T-P4) — Kiwoom mock reconcile kernel.

Design: ``docs/design/3797-kiwoom-mock-ledger.md`` §3.1–§3.4 and §6.1.

What it does: read the open/uncertain ``kiwoom_mock`` (or ``kiwoom_mock_us``)
ledger rows for one market, read the broker's order listings — following
``cont_yn``/``next_key`` pages — classify each row with the pure §3.2 table
(``mock_reconcile``), and apply the verdicts through
:class:`KiwoomMockOrderLedgerService`, the sole ledger writer.

The two markets differ only in their listings and their trading day (hk 4754):
KR reads kt00009 (order status) plus kt00007 (order detail, per trading day);
US reads ust21510 (today's orders *and fills*, the detail role) and ust21050
(open orders, the status role). No US listing covers a past ET trading day, so
a past US row is never classified from absence — it stays ``unknown``.

Modes (design §3.3):

* dry run — classify only, zero writes;
* terminal-only — expired / cancelled / rejected and uncertain→accepted;
  ``filled``/``partial`` are reported as ``deferred_fill_booking``;
* full mode (KR only) — the same, plus the broker-evidence fill delta booked
  into ``execution_ledger`` (through :class:`ExecutionLedgerRepository`), one
  reconcile-run row with ``broker='kiwoom'``, and — hk 4802 — the journal
  projection **per booked delta**: a buy delta creates or grows the order's
  ``kiwoom_mock`` lot journal, a sell delta closes FIFO lots (split on a
  partial sell) with realized P&L, and the order's cumulative trade row is
  upserted. Journal effects and the ``journaled_qty``/``journaled_avg``
  progress markers commit in one transaction with the delta booking's row
  lock, so a retried or overlapping run never journals a delta twice; a row
  whose booked fill is not fully journaled stays in the scan until it is.

Writes require exact-true gates, checked before any ledger read or broker
call: ``KIWOOM_MOCK_TERMINAL_AUTO_RECONCILE_ENABLED`` (or the US one) for
terminal-only, and both full-mode switches for full mode. Broker reads are
serial with a configurable minimum spacing (§3.1: mock TRs are 1 request/s). An
incomplete, truncated or page-capped listing is never absence: it degrades
every row to ``unknown`` for that run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Protocol

from sqlalchemy import text

from app.schemas.execution_ledger import ReconcileRunRecord
from app.services.brokers.kiwoom.mock_reconcile import (
    US_ORDER_LIST_KEY,
    BrokerEvidence,
    LedgerOrder,
    classify_verdict,
    evidence_from_payloads,
    return_code_ok,
    us_evidence_from_payloads,
    us_past_day_evidence,
)
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.kiwoom_mock_order_ledger_service import (
    ACCOUNT_MODE_KIWOOM_MOCK,
    ACCOUNT_MODE_KIWOOM_MOCK_US,
    FillBooking,
    KiwoomMockOrderLedgerService,
    KiwoomMockTransitionRefused,
    journaled_progress,
    kr_trading_day,
    us_trading_day,
)

logger = logging.getLogger(__name__)

__all__ = (
    "DEFAULT_MAX_LISTING_PAGES",
    "DEFAULT_MIN_REQUEST_INTERVAL_SECONDS",
    "FULL_GATE_ENVS",
    "TERMINAL_GATE_ENV",
    "US_TERMINAL_GATE_ENV",
    "KiwoomMockReconcileBroker",
    "KiwoomMockReconcileReader",
    "KiwoomMockUsReconcileBroker",
    "KiwoomMockUsReconcileReader",
    "full_gate_enabled",
    "kiwoom_mock_reconcile_orders_impl",
    "terminal_gate_enabled",
)

#: Exact-value gate for terminal-only application (default off). The string must
#: be exactly ``true`` — mirroring the H5 alert gate, not pydantic bool parsing.
TERMINAL_GATE_ENV: Final = "KIWOOM_MOCK_TERMINAL_AUTO_RECONCILE_ENABLED"
#: The US account keeps its own gate: it is a different credential namespace and
#: a different account, so arming one market must never arm the other.
US_TERMINAL_GATE_ENV: Final = "KIWOOM_MOCK_US_TERMINAL_AUTO_RECONCILE_ENABLED"

#: Full mode needs both switches exactly ``true`` (design §3.3): the feature
#: switch and the safety-review acknowledgement.
FULL_GATE_ENVS: Final = (
    "KIWOOM_MOCK_AUTO_RECONCILE_ENABLED",
    "KIWOOM_MOCK_AUTO_RECONCILE_SAFETY_REVIEW_PASSED",
)

#: Market -> the ledger account mode and the write gate it uses.
_MARKET_ACCOUNT_MODE: Final = {
    "kr": ACCOUNT_MODE_KIWOOM_MOCK,
    "us": ACCOUNT_MODE_KIWOOM_MOCK_US,
}
_MARKET_GATE_ENV: Final = {
    "kr": TERMINAL_GATE_ENV,
    "us": US_TERMINAL_GATE_ENV,
}

#: Mock TR limit is one request per second and per market (§3.1).
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS: Final = 1.0

#: Page cap for one listing. A listing still continuing after this many pages is
#: reported incomplete (``*_page_cap_reached``) instead of silently truncated.
DEFAULT_MAX_LISTING_PAGES: Final = 20

#: One advisory-lock key shared by every Kiwoom mock full reconcile run: two
#: overlapping runs (manual CLI plus a retry) must not interleave their reads
#: and bookings. The winner holds the lock until its run finishes.
_FULL_RUN_LOCK_KEY: Final = "auto_trader:kiwoom_mock_full_reconcile"
_FULL_RUN_LOCK_SQL: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended(CAST(:key AS text), 0))"
)

_TERMINAL_VERDICTS: Final = frozenset({"expired", "cancelled", "rejected"})

#: Every booked Kiwoom mock fill journals under this account label (operator
#: decision at-3796-3797-design-answers Q3); the journal's ``account_type`` is
#: ``mock`` — the account's own type.
JOURNAL_ACCOUNT: Final = "kiwoom_mock"
JOURNAL_ACCOUNT_TYPE: Final = "mock"

#: Ledger statuses a row with a committed fill can hold once it left the open
#: scan. Such a row is scanned only for journal recovery (the detail query's
#: ``filled_qty > journaled_qty`` clause): the verdict classifier would
#: misread a terminal source, and the ledger row itself is the authority on
#: what is still unjournaled.
_JOURNAL_RECOVERY_STATUSES: Final = frozenset(
    {"filled", "expired", "cancelled", "rejected"}
)


def terminal_gate_enabled(
    environ: Mapping[str, str] | None = None, *, market: str = "kr"
) -> bool:
    """``False`` unless this market's gate env var is exactly ``true``."""
    try:
        gate = _MARKET_GATE_ENV[market]
    except KeyError as exc:
        raise ValueError(f"unknown kiwoom mock market {market!r}") from exc
    source = os.environ if environ is None else environ
    return source.get(gate) == "true"


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


class KiwoomMockUsReconcileReader(Protocol):
    """The two US listings. ``None`` means the read failed (never absence)."""

    async def read_today_orders(
        self, *, cont_yn: str | None = None, next_key: str | None = None
    ) -> Mapping[str, Any] | None: ...

    async def read_open_orders(
        self, *, cont_yn: str | None = None, next_key: str | None = None
    ) -> Mapping[str, Any] | None: ...


class KiwoomMockUsReconcileBroker:
    """Default US broker reader: the US-credential mock account client."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _account(self) -> Any:
        from app.services.brokers.kiwoom.us_account import KiwoomUsAccountClient
        from app.services.brokers.kiwoom.us_client import KiwoomMockUsClient

        if self._client is None:
            # Default-disabled: raises when the US mock gate or credentials are
            # missing, before any network call.
            self._client = KiwoomMockUsClient.from_app_settings()
        return KiwoomUsAccountClient(self._client)

    async def read_today_orders(
        self, *, cont_yn: str | None = None, next_key: str | None = None
    ) -> Mapping[str, Any] | None:
        return await self._account().get_today_orders(
            cont_yn=cont_yn, next_key=next_key
        )

    async def read_open_orders(
        self, *, cont_yn: str | None = None, next_key: str | None = None
    ) -> Mapping[str, Any] | None:
        return await self._account().get_open_orders(cont_yn=cont_yn, next_key=next_key)


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
    trade_date: datetime | None = None
    #: Journaled quantity as the candidate list snapshot saw it (pre-lock).
    journaled_qty: Decimal = Decimal(0)


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


def _us_evidence(
    today_listing: _Listing | None, open_listing: _Listing | None
) -> BrokerEvidence:
    """US evidence from the two listings, or the reason one is unusable."""
    for listing, written in (
        (today_listing, "us_today_orders"),
        (open_listing, "us_open_orders"),
    ):
        if listing is None:
            return BrokerEvidence(
                complete=False, reason=f"{written}_unreadable", rows=()
            )
        if listing.reason is not None:
            return BrokerEvidence(complete=False, reason=listing.reason, rows=())
    return us_evidence_from_payloads(
        today_orders=today_listing.payload,
        open_orders=open_listing.payload,
    )


@asynccontextmanager
async def _hold_full_run_lock(
    session_factory: Callable[[], Any],
) -> AsyncIterator[None]:
    """Hold one advisory lock for the whole full-mode run.

    Two overlapping runs must not both book the same delta. The delta itself is
    idempotent (``execution_ledger``'s unique key plus the row's recorded
    quantity), and this run-level lock additionally keeps the two runs from
    interleaving at all (the broker readings and the per-row transactions), so
    each delta is booked once and the run row counts it once.

    The lock is transaction-scoped and therefore needs a transaction of its own
    that stays open for the run: every write the run makes commits in *other*
    sessions, so a lock taken on one of those would be released by its first
    commit. It holds no row lock and no data, only the advisory key.
    """
    async with session_factory() as db:
        await db.execute(_FULL_RUN_LOCK_SQL, {"key": _FULL_RUN_LOCK_KEY})
        try:
            yield
        finally:
            await db.rollback()


async def _journal_buy_delta(
    db: Any,
    booking: FillBooking,
    *,
    quantity: Decimal,
    price: Decimal,
    trade_id: int | None,
) -> dict[str, Any]:
    """Grow the order's active buy lot by ``quantity``, or open a new one.

    The order's lot is found through its ``journal_id``. A still-active lot
    absorbs the unjournaled quantity at a quantity-weighted entry price (hk
    4802, operator decision 1); a fully closed (or missing) lot is replaced by
    a new active lot for the order — the sell that closed it already took its
    realized P&L. The caller owns the transaction (never commits).
    """
    from app.models.trade_journal import JournalStatus, TradeJournal
    from app.models.trading import InstrumentType

    lot = None
    if booking.journal_id is not None:
        lot = await db.get(TradeJournal, booking.journal_id)
    if lot is not None and lot.status == JournalStatus.active:
        active_qty = lot.quantity or Decimal(0)
        active_price = lot.entry_price or Decimal(0)
        new_qty = active_qty + quantity
        lot.entry_price = (active_qty * active_price + quantity * price) / new_qty
        lot.quantity = new_qty
        lot.amount = new_qty * lot.entry_price
        await db.flush()
        return {"journal_grown": True, "journal_id": lot.id}
    lot = TradeJournal(
        symbol=booking.symbol,
        instrument_type=InstrumentType(booking.instrument_type),
        side="buy",
        entry_price=price,
        quantity=quantity,
        amount=price * quantity,
        thesis=(booking.thesis or "").strip() or "reconciled fill",
        strategy=(booking.strategy or "").strip() or "reconciled fill",
        indicators_snapshot=booking.indicators_snapshot,
        notes=booking.notes,
        account=JOURNAL_ACCOUNT,
        account_type=JOURNAL_ACCOUNT_TYPE,
        trade_id=trade_id,
        correlation_id=booking.correlation_id,
        status=JournalStatus.active,
    )
    db.add(lot)
    await db.flush()
    return {"journal_created": True, "journal_id": lot.id}


async def _journal_unjournaled(
    db: Any,
    service: KiwoomMockOrderLedgerService,
    booking: FillBooking,
) -> dict[str, Any]:
    """Journal the order's unjournaled booked quantity inside the transaction.

    ``unjournaled = filled_qty - journaled_qty`` is the quantity the committed
    booking still owes the journal: the fresh delta on a normal booking, or
    the remainder a previous sell could not match. Its price is the implied
    residual ``(filled·avg − journaled·journaled_avg) / unjournaled`` — for a
    fresh delta that is exactly the delta's implied price, so the journal's
    cost basis equals the booked notional.

    A buy journals the whole remainder into the order's lot; a sell closes
    FIFO lots and a shortfall is NOT journaled: the progress marker advances
    only by the matched part, a manual-review reason is left on the row, and
    the next run sees the rest again. Imported lazily so the reconcile module
    stays light for the CLI.
    """
    from app.mcp_server.tooling import order_journal

    unjournaled = booking.cumulative_qty - booking.journaled_qty
    if unjournaled <= 0:
        return {"skipped": "already_journaled"}
    residual = (
        booking.cumulative_qty * booking.cumulative_avg
        - booking.journaled_qty * booking.journaled_avg
    )
    price = residual / unjournaled
    if price <= 0:
        raise ValueError("unjournaled_price_unavailable")
    average = booking.cumulative_avg if booking.cumulative_avg > 0 else price
    trade_id = await order_journal._record_trade_for_fill(
        db,
        symbol=booking.symbol,
        instrument_type=booking.instrument_type,
        side=str(booking.side),
        price=float(average),
        quantity=float(booking.cumulative_qty),
        total_amount=float(booking.cumulative_qty * average),
        fee=0.0,
        currency=booking.currency,
        account=JOURNAL_ACCOUNT,
        order_id=booking.broker_order_id,
    )
    details: dict[str, Any] = {"trade_id": trade_id}
    manual_review_reason: str | None = None
    if booking.side == "buy":
        grown = await _journal_buy_delta(
            db, booking, quantity=unjournaled, price=price, trade_id=trade_id
        )
        details.update(grown)
        journaled_now = unjournaled
    else:
        close = await order_journal._close_journals_on_sell(
            symbol=booking.symbol,
            sell_quantity=float(unjournaled),
            sell_price=float(price),
            exit_reason=booking.exit_reason or booking.reason,
            account_type=JOURNAL_ACCOUNT_TYPE,
            account=JOURNAL_ACCOUNT,
            db=db,
        )
        details["journals_closed"] = close["journals_closed"]
        details["closed_journal_ids"] = close["closed_ids"]
        details["realized_pnl_pct"] = close["total_pnl_pct"]
        details["realized_pnl_basis"] = close.get("realized_pnl_basis", "journal_entry")
        unmatched = Decimal(str(close["unmatched_quantity"]))
        details["unmatched_quantity"] = float(unmatched)
        journaled_now = unjournaled - unmatched
        if unmatched > 0:
            manual_review_reason = f"unmatched_sell_quantity:{unmatched}"
    new_qty = booking.journaled_qty + journaled_now
    new_avg = (
        (booking.journaled_qty * booking.journaled_avg + journaled_now * price)
        / new_qty
        if new_qty > 0
        else booking.journaled_avg
    )
    await service.record_journal_progress(
        ledger_id=booking.ledger_id,
        trade_id=trade_id,
        journal_id=details.get("journal_id"),
        journaled_qty=new_qty,
        journaled_avg=new_avg,
        manual_review_reason=manual_review_reason,
        commit=False,
    )
    details["journaled_qty"] = str(new_qty)
    details["journaled_avg"] = str(new_avg)
    if manual_review_reason is not None:
        details["manual_review"] = manual_review_reason
    return details


async def kiwoom_mock_reconcile_orders_impl(
    *,
    market: str = "kr",
    dry_run: bool = True,
    terminal_only: bool = False,
    limit: int = 100,
    min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
    max_pages: int = DEFAULT_MAX_LISTING_PAGES,
    session_factory: Callable[[], Any] | None = None,
    reader: Any | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify and apply Kiwoom mock order verdicts for one market.

    ``dry_run`` issues zero writes. ``terminal_only`` applies closed-outcome
    transitions only and leaves fills deferred. Full mode
    (``dry_run=False, terminal_only=False``, KR only) also books the
    broker-evidence fill delta into ``execution_ledger`` and journals each
    booked delta — buy lots are created/grown, sell deltas FIFO-close lots
    with realized P&L (hk 4802). Every write path needs its exact-true operator
    gate(s): ``KIWOOM_MOCK_TERMINAL_AUTO_RECONCILE_ENABLED`` (or
    ``KIWOOM_MOCK_US_TERMINAL_AUTO_RECONCILE_ENABLED``) for terminal-only and
    both full-mode switches for full mode.

    ``market`` selects the ledger account mode, the trading-day clock, the
    listings and the gate. KR and US never share a scan.
    """
    if market not in _MARKET_ACCOUNT_MODE:
        raise ValueError(f"market must be 'kr' or 'us'; got {market!r}")
    account_mode = _MARKET_ACCOUNT_MODE[market]
    if terminal_only and dry_run:
        raise ValueError("terminal_only requires dry_run=False")
    if min_request_interval_seconds < 0:
        raise ValueError("min_request_interval_seconds must be >= 0")
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")
    full_mode = not dry_run and not terminal_only
    if full_mode and market != "kr":
        # Full-mode booking is KR only in this ticket (T-P4). Refusing instead
        # of writing in a half-implemented scope keeps the US scan terminal-only.
        raise ValueError(
            "full-mode booking is not implemented for US (T-P4 is KR only); "
            "pass terminal_only=True"
        )
    scope = "dry_run" if dry_run else ("terminal_only" if terminal_only else "full")
    if not dry_run:
        # Gate on the write condition itself, not on the caller's flag, and
        # before any ledger read or broker call (design §3.3).
        if terminal_only and not terminal_gate_enabled(market=market):
            return {
                "success": False,
                "error": "terminal_gate_disabled",
                "error_class": "terminal_gate_disabled",
                "account_mode": account_mode,
                "market": market,
                "scope": scope,
            }
        if full_mode and not full_gate_enabled():
            return {
                "success": False,
                "error": "full_gate_disabled",
                "error_class": "full_gate_disabled",
                "required_env": list(FULL_GATE_ENVS),
                "account_mode": account_mode,
                "market": market,
                "scope": scope,
            }

    if session_factory is None:
        from app.core.db import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    if sleep is None:
        sleep = asyncio.sleep
    if reader is None:
        reader = (
            KiwoomMockReconcileBroker()
            if market == "kr"
            else KiwoomMockUsReconcileBroker()
        )

    async def _run_once() -> dict[str, Any]:
        return await _reconcile_orders(
            market=market,
            account_mode=account_mode,
            scope=scope,
            dry_run=dry_run,
            terminal_only=terminal_only,
            limit=limit,
            min_request_interval_seconds=min_request_interval_seconds,
            max_pages=max_pages,
            session_factory=session_factory,
            reader=reader,
            sleep=sleep,
            now=now,
        )

    if full_mode:
        async with _hold_full_run_lock(session_factory):
            return await _run_once()
    return await _run_once()


async def _reconcile_orders(
    *,
    market: str,
    account_mode: str,
    scope: str,
    dry_run: bool,
    terminal_only: bool,
    limit: int,
    min_request_interval_seconds: float,
    max_pages: int,
    session_factory: Callable[[], Any],
    reader: Any,
    sleep: Callable[[float], Awaitable[None]],
    now: datetime | None,
) -> dict[str, Any]:
    """One reconcile run: read, classify, apply (the caller owns the gate)."""
    full_mode = not dry_run and not terminal_only

    # ---- read the ledger candidates (one short session) -------------------
    try:
        async with session_factory() as db:
            service = KiwoomMockOrderLedgerService(db)
            rows, open_total = await service.list_reconcile_candidates(
                limit=limit,
                account_mode=account_mode,
                include_journal_recovery=full_mode,
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
                    trade_date=row.trade_date,
                    journaled_qty=journaled_progress(row)[0],
                )
                for row in rows
            ]
            acknowledged_cancels = await service.list_acknowledged_cancels(
                original_order_ids={
                    c.broker_order_id for c in candidates if c.broker_order_id
                },
                account_mode=account_mode,
            )
    except Exception as exc:
        logger.exception("Failed to list kiwoom mock ledger candidates: %s", exc)
        return {
            "success": False,
            "error": type(exc).__name__,
            "error_class": type(exc).__name__,
            "account_mode": account_mode,
            "market": market,
            "scope": scope,
        }

    moment = now or datetime.now(UTC)
    today = kr_trading_day(moment) if market == "kr" else us_trading_day(moment)
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
            "account_mode": account_mode,
            "market": market,
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
    detail_listings: dict[str, _Listing] = {}
    us_today_listing: _Listing | None = None
    us_open_listing: _Listing | None = None
    if market == "kr":
        if today in days:
            status_listing = await _collect(
                lambda cont_yn, next_key: reader.read_order_status(
                    cont_yn=cont_yn, next_key=next_key
                ),
                list_key="acnt_ord_cntr_prst_array",
                label="order_status",
            )
        for day in days:
            detail_listings[day] = await _collect(
                lambda cont_yn, next_key, day=day: reader.read_order_detail(
                    order_date=day, cont_yn=cont_yn, next_key=next_key
                ),
                list_key="acnt_ord_cntr_prps_dtl",
                label="order_detail",
            )
    elif today in days:
        # Both US listings are today-scoped (ust21510 lists today's orders and
        # fills, ust21050 the currently open ones): read them once.
        us_today_listing = await _collect(
            lambda cont_yn, next_key: reader.read_today_orders(
                cont_yn=cont_yn, next_key=next_key
            ),
            list_key=US_ORDER_LIST_KEY,
            label="us_today_orders",
        )
        us_open_listing = await _collect(
            lambda cont_yn, next_key: reader.read_open_orders(
                cont_yn=cont_yn, next_key=next_key
            ),
            list_key=US_ORDER_LIST_KEY,
            label="us_open_orders",
        )

    # ---- classify + apply --------------------------------------------------
    reconciled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    unresolved_count = 0
    booked_count = 0
    already_booked_count = 0
    run_now = moment
    run_started_at = run_now
    run_id = uuid.uuid4()

    async def _write_transition(
        candidate: _Candidate,
        *,
        action: str,
        reason: str,
        filled_quantity: int | None = None,
    ) -> tuple[str, str | None]:
        """Apply one accepted terminal transition; report what happened.

        ``filled_quantity`` is set for a terminal verdict that carried a
        positive cumulative fill: :meth:`record_fill` already moved the row off
        the no-fill source statuses, so the close goes through
        ``close_filled_as_terminal``, which accepts a ``partial``/``filled`` row
        only once the row's booked quantity equals this exact fill.
        """
        async with session_factory() as db:
            service = KiwoomMockOrderLedgerService(db)
            try:
                if action == "promote_accepted":
                    await service.promote_uncertain_to_accepted(
                        ledger_id=candidate.ledger_id,
                        broker_order_id=str(candidate.broker_order_id),
                        detail={"verdict": "accepted", "reason": reason},
                        account_mode=account_mode,
                    )
                    return "promoted_accepted", None
                status = action.removeprefix("mark_")
                if filled_quantity is not None:
                    await service.close_filled_as_terminal(
                        ledger_id=candidate.ledger_id,
                        status=status,
                        reason=reason,
                        filled_quantity=filled_quantity,
                        detail={"verdict": status},
                        account_mode=account_mode,
                    )
                    return action, None
                await service.mark_terminal(
                    ledger_id=candidate.ledger_id,
                    status=status,
                    reason=reason,
                    detail={"verdict": status},
                    account_mode=account_mode,
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

    async def _book_fill(
        candidate: _Candidate, verdict: Any, outcome: dict[str, Any]
    ) -> None:
        """Book the broker-evidence fill delta and journal it, in one transaction.

        The ledger is the authority on fills, so every fill verdict of a full
        run lands its delta there — including the cumulative fill a terminal
        verdict carries. hk 4802: the journaled part of the delta is written in
        the *same* transaction under the booking's row lock — a buy delta
        creates/grows the order's lot, a sell delta FIFO-closes lots — and the
        ``journaled_qty`` marker commits with it, so a retried or overlapping
        run can never journal a delta twice or lose a booked one.
        """
        nonlocal booked_count, already_booked_count
        if verdict.filled_quantity is None or verdict.average_price is None:
            # The classifier never emits filled/partial without both; refuse to
            # invent either value.
            outcome["action"] = "noop_no_evidence"
            outcome["requires_manual_review"] = True
            return
        try:
            async with session_factory() as db:
                service = KiwoomMockOrderLedgerService(db)
                booking = await service.record_fill(
                    ledger_id=candidate.ledger_id,
                    filled_quantity=int(verdict.filled_quantity),
                    average_price=int(verdict.average_price),
                    observed_at=run_now,
                    commit=False,
                )
                journal = await _journal_unjournaled(db, service, booking)
                await db.commit()
        except KiwoomMockTransitionRefused as exc:
            logger.info(
                "kiwoom mock fill refused id=%s: %s", candidate.ledger_id, exc.reason
            )
            outcome["action"] = "noop_transition_refused"
            outcome["transition_refused"] = exc.reason
            outcome["requires_manual_review"] = True
            return
        except Exception as exc:  # noqa: BLE001 — the class is surfaced, never hidden
            logger.exception(
                "kiwoom mock fill+journal failed id=%s", candidate.ledger_id
            )
            # The whole transaction rolled back: neither the delta nor the
            # journal landed, so the next run retries both exactly once.
            outcome["action"] = "fill_journal_failed"
            outcome["journal_error"] = type(exc).__name__
            outcome["requires_manual_review"] = True
            return

        outcome["action"] = booking.action
        outcome["delta_qty"] = str(booking.delta_qty)
        outcome["booked_cumulative_qty"] = str(booking.cumulative_qty)
        outcome["journal"] = journal
        if journal.get("manual_review"):
            outcome["requires_manual_review"] = True
        if booking.execution_ledger_row_id is not None:
            outcome["execution_ledger_row_id"] = booking.execution_ledger_row_id
        if booking.fill_seq is not None:
            outcome["fill_seq"] = booking.fill_seq
        if booking.action == "noop_already_booked":
            already_booked_count += 1
        else:
            booked_count += 1

    async def _journal_recovery(
        candidate: _Candidate,
        outcome: dict[str, Any],
        *,
        keep_action: bool = False,
    ) -> None:
        """Journal whatever a committed fill still owes, without re-booking.

        The row left the open scan (a terminal verdict, or ``filled``) or its
        verdict this run carried no new fill evidence, yet ``journaled_qty``
        is short — e.g. a sell delta that outran the available lots. The
        ledger row is the authority: lock it, journal the remainder in one
        transaction. ``keep_action`` preserves the verdict action the caller
        already recorded (an open row can owe journaled shares under a
        ``pending`` verdict too).
        """
        try:
            async with session_factory() as db:
                service = KiwoomMockOrderLedgerService(db)
                locked = await service.lock_for_journal(ledger_id=candidate.ledger_id)
                details = await _journal_unjournaled(db, service, locked)
                await db.commit()
        except KiwoomMockTransitionRefused as exc:
            logger.info(
                "kiwoom mock journal recovery refused id=%s: %s",
                candidate.ledger_id,
                exc.reason,
            )
            if not keep_action:
                outcome["action"] = "noop_transition_refused"
            outcome["transition_refused"] = exc.reason
            outcome["requires_manual_review"] = True
            return
        except Exception as exc:  # noqa: BLE001 — surface the class, never hide it
            logger.exception(
                "kiwoom mock journal recovery failed id=%s", candidate.ledger_id
            )
            if not keep_action:
                outcome["action"] = "journal_failed"
            outcome["journal_error"] = type(exc).__name__
            outcome["requires_manual_review"] = True
            return
        outcome["journal"] = details
        if details.get("skipped"):
            if not keep_action:
                outcome["action"] = "noop_already_journaled"
        elif details.get("manual_review"):
            if not keep_action:
                outcome["action"] = "journal_unmatched"
            outcome["requires_manual_review"] = True
        elif not keep_action:
            outcome["action"] = "journaled"

    for candidate in candidates:
        if (
            full_mode
            and candidate.status in _JOURNAL_RECOVERY_STATUSES
            and candidate.journaled_qty < candidate.recorded_filled_quantity
        ):
            # A row whose fill is committed but not fully journaled (e.g. a
            # short sell): classify nothing, journal the remainder instead.
            outcome = {
                "ledger_id": candidate.ledger_id,
                "client_order_id": candidate.client_order_id,
                "broker_order_id": candidate.broker_order_id,
                "symbol": candidate.symbol,
                "side": candidate.side,
                "status": candidate.status,
                "verdict": "journal_recovery",
            }
            await _journal_recovery(candidate, outcome)
            if outcome.get("requires_manual_review"):
                unresolved_count += 1
            counts["journal_recovery"] = counts.get("journal_recovery", 0) + 1
            reconciled.append(outcome)
            continue
        if market == "kr":
            is_today = candidate.trading_day == today
            evidence = _evidence_for(
                detail=detail_listings[candidate.trading_day],
                status=status_listing if is_today else None,
            )
        elif candidate.trading_day == today:
            evidence = _us_evidence(us_today_listing, us_open_listing)
        else:
            # No US listing covers a past ET trading day: never classify it
            # from absence (I5, operator Q4).
            evidence = us_past_day_evidence()
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
            # Full mode books the fill, so its price is required for KR. A US
            # fill is never booked here, so the missing average price there must
            # not turn a real fill into an unclassified row.
            require_fill_price=market == "kr",
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
            if verdict.filled_quantity and not full_mode:
                # A scope that cannot book a fill (terminal_only, and US, whose
                # full mode is refused) must never close a row the broker
                # filled: the row would leave the open scan and the shares would
                # be stranded outside every booking reader. It stays open for a
                # full run, exactly like a filled/partial verdict there.
                outcome["action"] = "deferred_fill_booking"
                outcome["requires_session_reconcile"] = True
            elif dry_run:
                outcome["action"] = f"would_mark_{verdict.verdict}"
            else:
                filled_close = (
                    int(verdict.filled_quantity) if verdict.filled_quantity else None
                )
                if filled_close is not None:
                    # A terminal verdict that still filled part of its quantity:
                    # book the delta before the close, so no share is stranded
                    # outside the ledger. The close then requires exactly this
                    # booked quantity.
                    await _book_fill(candidate, verdict, outcome)
                    outcome["fill_action"] = outcome["action"]
                action, refused = await _write_transition(
                    candidate,
                    action=f"mark_{verdict.verdict}",
                    reason=verdict.reason,
                    filled_quantity=filled_close,
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
                await _book_fill(candidate, verdict, outcome)
            else:
                # Terminal-only never books fills (design §3.3).
                outcome["action"] = "deferred_fill_booking"
                outcome["requires_session_reconcile"] = True
        elif verdict.verdict == "pending":
            outcome["action"] = "noop_pending"
        else:
            outcome["action"] = "noop_no_evidence"
            outcome["requires_manual_review"] = True

        # An open row can still owe journaled shares under a non-fill verdict
        # (a short sell's remainder stays ``partial``/``pending``): journal it.
        if (
            full_mode
            and "journal" not in outcome
            and candidate.journaled_qty < candidate.recorded_filled_quantity
        ):
            await _journal_recovery(candidate, outcome, keep_action=True)

        # Work still owed after this run: an unbooked fill or a row a human must
        # resolve. A successful close or booking is resolved.
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
        "account_mode": account_mode,
        "market": market,
        "dry_run": dry_run,
        "scope": scope,
        "counts": counts,
        "reconciled": reconciled,
        "candidate_scan": scan,
        "unresolved_count": unresolved_count,
        "reconcile_run": run_record,
        "message": (
            f"Reconciled {len(reconciled)} kiwoom mock order(s) "
            f"(market={market}, dry_run={dry_run}, scope={scope}): {counts}"
        ),
    }
