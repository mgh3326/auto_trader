"""Read-only venue sell-side best ask for mandate plan proposals (3795 T4d).

The mandate near-ask band (design §4.1 condition 6) is measured against the
venue's own best ask, never against ``current_price``. This module is the only
place that reads that ask:

* a venue has a reader only when its book is the book the order meets. A venue
  without one has **no ask** — there is no substitute (``current_price``, the
  Toss/KIS ``current_price`` fallback of §2.3 F7, another venue's book);
* the ask is an observation dated by the **broker's own book time**
  (``aspr_acpt_hour`` + its date), never by when we asked: a book that was
  already old when it arrived must look old to the gate. A book with no
  verifiable time has no ask. Request/response times are kept separately for
  latency, and every read — also a failed or refused one — leaves a
  closed-vocabulary ``QuoteReadRecord`` with an opaque ``observation_id`` that
  the quote carries, so a later use (``QuoteUseRecord``, Q10 evidence E1: read ->
  send elapsed time) names the exact read it consumed;
* the module only reads. It imports no order, ledger or approval module
  (``tests/services/order_proposals/test_mandate_quote.py``).

Freshness is *not* decided here: ``proposal_gate.check_near_ask`` applies the
5 second bound at the use site.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal

from app.core.timezone import KST
from app.services.mandate.proposal_gate import PlanProposalContext, ProposalQuote

logger = logging.getLogger(__name__)

READ_OUTCOMES: Final = (
    "ok",
    "no_ask",
    "invalid_level",
    "crossed_book",
    "no_price",
    "no_book_time",
    "unsupported_venue",
    "read_failed",
)
ReadOutcome = Literal[
    "ok",
    "no_ask",
    "invalid_level",
    "crossed_book",
    "no_price",
    "no_book_time",
    "unsupported_venue",
    "read_failed",
]

# The broker stamps the book to the second; a stamp this far ahead of our own
# response clock is a clock disagreement, not a fresher book.
BOOK_CLOCK_SKEW: Final = timedelta(seconds=2)

# ``(account_mode, market)`` -> (venue label, KIS ``FID_COND_MRKT_DIV_CODE``).
# Kiwoom's mock account is KRX-only (``brokers/kiwoom/constants.py``).
KIS_BOOK_BY_ACCOUNT: Final[dict[tuple[str, str], tuple[str, str]]] = {
    ("kiwoom_mock", "equity_kr"): ("krx", "J"),
}
# KIS live KR picks its route per symbol exactly like the order path
# (``domestic_orders._domestic_exchange_route``): an NXT-eligible symbol goes
# through SOR (the unified book), any other symbol to KRX.
KIS_LIVE_ROUTED: Final = frozenset({("kis_live", "equity_kr")})
_KIS_KRX: Final = ("krx", "J")
_KIS_UNIFIED: Final = ("unified", "UN")
# Toss, US, crypto and every other pair: no reader, so no ask (fail-closed).


def has_book_source(account_mode: str, market: str) -> bool:
    key = (account_mode, market)
    return key in KIS_BOOK_BY_ACCOUNT or key in KIS_LIVE_ROUTED


async def _route_for(
    account_mode: str, market: str, symbol: str
) -> tuple[str, str] | None:
    key = (account_mode, market)
    if key in KIS_BOOK_BY_ACCOUNT:
        return KIS_BOOK_BY_ACCOUNT[key]
    if key in KIS_LIVE_ROUTED:
        from app.services.kr_symbol_universe_service import is_nxt_eligible

        return _KIS_UNIFIED if await is_nxt_eligible(symbol) else _KIS_KRX
    return None


@dataclass(frozen=True)
class QuoteReadRecord:
    """One venue ask read, for Q10 evidence E1 (no account number, no secret)."""

    observation_id: str
    account_mode: str
    market: str
    symbol: str
    venue: str | None
    source: str | None
    outcome: ReadOutcome
    requested_at: datetime
    read_completed_at: datetime
    read_ms: int
    book_at: datetime | None = None
    book_age_ms: int | None = None
    best_ask: str | None = None
    best_bid: str | None = None
    error_class: str | None = None
    kind: str = "read"

    def to_json(self) -> str:
        row = asdict(self)
        for key in ("requested_at", "read_completed_at", "book_at"):
            value = getattr(self, key)
            row[key] = value.isoformat() if value is not None else None
        return json.dumps(row, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class QuoteUseRecord:
    """One consumption of an observation: the read -> use elapsed time (E1)."""

    observation_id: str
    stage: str
    checked_at: datetime
    age_ms: int
    gate_result: str
    kind: str = "use"

    def to_json(self) -> str:
        row = asdict(self)
        row["checked_at"] = self.checked_at.isoformat()
        return json.dumps(row, sort_keys=True, separators=(",", ":"))


Record = QuoteReadRecord | QuoteUseRecord
RecordSink = Callable[[Record], None]


def log_sink(record: Record) -> None:
    logger.info("mandate_quote_%s %s", record.kind, record.to_json())


class JsonlSink:
    """Append one JSON line per record. Never raises: a record is not an input.

    ``failed`` counts lost records so a measurement tool can refuse to call a
    run with lost data a success; the in-service sink ignores it (fail-open).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self.written = 0
        self.failed = 0

    def __call__(self, record: Record) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(record.to_json() + "\n")
        except OSError:
            self.failed += 1
            logger.warning("mandate_quote record not written", exc_info=True)
        else:
            self.written += 1


def record_quote_use(
    quote: ProposalQuote,
    *,
    stage: str,
    checked_at: datetime,
    gate_result: str,
    sink: RecordSink = log_sink,
) -> None:
    """Record that ``quote`` was used at ``checked_at`` (never raises)."""
    if quote.observation_id is None:
        return
    try:
        sink(
            QuoteUseRecord(
                observation_id=quote.observation_id,
                stage=stage,
                checked_at=checked_at,
                age_ms=int((checked_at - quote.ask_observed_at).total_seconds() * 1000),
                gate_result=gate_result,
            )
        )
    except Exception:  # noqa: BLE001 - the record is never an input
        logger.warning("mandate_quote use record failed", exc_info=True)


@dataclass(frozen=True)
class VenueBook:
    """Closed, parsed result of one KIS orderbook read."""

    best_ask: Decimal | None
    best_bid: Decimal | None
    broker_book_time: str | None
    current_price: Decimal | None
    outcome: ReadOutcome
    broker_book_date: str | None = None


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _levels(
    output: dict[str, Any], prefix: str
) -> tuple[list[tuple[Decimal, Decimal]], bool]:
    """Resting levels and whether any occupied level is malformed.

    A level is *unoccupied* when its price and quantity are both blank or zero.
    Any other level must carry a positive finite price and a finite
    non-negative quantity; otherwise the book is untrustworthy (skipping the
    bad level would promote a deeper level to best), so the caller fails closed.
    A parseable zero quantity is an empty level, not an ask.
    """
    levels: list[tuple[Decimal, Decimal]] = []
    for idx in range(1, 11):
        raw_price = output.get(f"{prefix}p{idx}")
        raw_quantity = output.get(f"{prefix}p_rsqn{idx}")
        if raw_quantity is None:
            raw_quantity = output.get(f"{prefix}p{idx}_rsqn")
        price = _decimal(raw_price)
        quantity = _decimal(raw_quantity)
        price_empty = _blank(raw_price) or price == 0
        quantity_empty = _blank(raw_quantity) or quantity == 0
        if price_empty and quantity_empty:
            continue
        if price is None or price <= 0:
            return levels, True
        if quantity is None or quantity < 0:
            return levels, True
        if quantity == 0:
            continue
        levels.append((price, quantity))
    return levels, False


def parse_kis_book(
    output1: dict[str, Any],
    current_price_raw: Any = None,
    output2: dict[str, Any] | None = None,
) -> VenueBook:
    """Best ask = lowest ask level that has a positive price and quantity.

    A book with no ask, a malformed occupied level, or whose best bid is not
    below its best ask (locked or crossed: a half-updated or contradictory book)
    has no usable ask.
    """
    broker_time = output1.get("aspr_acpt_hour")
    book_time = str(broker_time).strip() if not _blank(broker_time) else None
    book_date_raw = output1.get("stck_bsop_date")
    if _blank(book_date_raw) and output2 is not None:
        book_date_raw = output2.get("stck_bsop_date")
    book_date = str(book_date_raw).strip() if not _blank(book_date_raw) else None
    asks, ask_bad = _levels(output1, "ask")
    bids, bid_bad = _levels(output1, "bid")
    best_bid = max((price for price, _ in bids), default=None)
    price = _positive_decimal(current_price_raw)

    def book(best_ask: Decimal | None, outcome: ReadOutcome) -> VenueBook:
        return VenueBook(best_ask, best_bid, book_time, price, outcome, book_date)

    if ask_bad or bid_bad:
        return book(None, "invalid_level")
    if not asks:
        return book(None, "no_ask")
    best_ask = min(level for level, _ in asks)
    if best_bid is not None and best_bid >= best_ask:
        return book(None, "crossed_book")
    if price is None:
        return book(best_ask, "no_price")
    return book(best_ask, "ok")


def resolve_book_at(
    time_text: str | None, date_text: str | None, received_at: datetime
) -> datetime | None:
    """The broker's own book time as an aware datetime, or ``None``.

    Without a broker date the date is the response's KST date, or the day
    before when that would put the stamp ahead of the response. A stamp more
    than ``BOOK_CLOCK_SKEW`` ahead of the response, or one that does not parse,
    is unverifiable.
    """
    if time_text is None or received_at.utcoffset() is None:
        return None
    try:
        stamp = datetime.strptime(time_text, "%H%M%S").time()
        day: date | None = (
            datetime.strptime(date_text, "%Y%m%d").date() if date_text else None
        )
    except ValueError:
        return None
    received_kst = received_at.astimezone(KST)
    candidate = datetime.combine(day or received_kst.date(), stamp, tzinfo=KST)
    if day is None and candidate > received_kst + BOOK_CLOCK_SKEW:
        candidate -= timedelta(days=1)
    return None if candidate > received_kst + BOOK_CLOCK_SKEW else candidate


async def _read_kis_book(code: str, market_code: str) -> VenueBook:
    # Imported here: the KIS client pulls the whole broker stack, and this module
    # is imported by the proposal service.
    from app.services.brokers.kis.client import KISClient

    kis = KISClient()
    output1, output2 = await kis.inquire_orderbook_snapshot(code, market_code)
    price_evidence = await kis.inquire_price_raw_evidence(code, market_code)
    return parse_kis_book(output1, price_evidence.get("stck_prpr"), output2)


def kr_tick_size_at(price: Decimal) -> Decimal:
    """KRX stock tick at ``price`` (an unclassified security keeps the stock table)."""
    from app.mcp_server.tick_size import get_tick_size_kr

    return Decimal(get_tick_size_kr(float(price)))


BookReader = Callable[[str, str], Awaitable[VenueBook]]


async def read_plan_quote(
    context: PlanProposalContext,
    *,
    sink: RecordSink = log_sink,
    book_reader: BookReader | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ProposalQuote | None:
    """Return the venue quote for a plan, or ``None`` (no substitute) if unusable."""
    return await read_venue_quote(
        account_mode=context.sleeve.account_mode,
        market=context.sleeve.market,
        symbol=context.plan.symbol,
        premium_bps=context.terms.entry_max_premium_bps,
        sink=sink,
        book_reader=book_reader,
        clock=clock,
    )


async def read_venue_quote(
    *,
    account_mode: str,
    market: str,
    symbol: str,
    premium_bps: Decimal,
    sink: RecordSink = log_sink,
    book_reader: BookReader | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ProposalQuote | None:
    started = clock()
    observation_id = uuid.uuid4().hex
    route: tuple[str, str] | None = None
    route_error: str | None = None
    try:
        route = await _route_for(account_mode, market, symbol)
    except Exception as exc:  # noqa: BLE001 - an unresolved route has no book
        route_error = type(exc).__name__

    def emit(
        outcome: ReadOutcome,
        *,
        book: VenueBook | None = None,
        book_at: datetime | None = None,
        error_class: str | None = None,
    ) -> datetime:
        completed = clock()
        try:
            sink(
                QuoteReadRecord(
                    observation_id=observation_id,
                    account_mode=account_mode,
                    market=market,
                    symbol=symbol,
                    venue=route[0] if route else None,
                    source="kis" if route else None,
                    outcome=outcome,
                    requested_at=started,
                    read_completed_at=completed,
                    read_ms=int((completed - started).total_seconds() * 1000),
                    book_at=book_at,
                    book_age_ms=(
                        int((completed - book_at).total_seconds() * 1000)
                        if book_at is not None
                        else None
                    ),
                    best_ask=format(book.best_ask, "f")
                    if book and book.best_ask
                    else None,
                    best_bid=format(book.best_bid, "f")
                    if book and book.best_bid
                    else None,
                    error_class=error_class,
                )
            )
        except Exception:  # noqa: BLE001 - the record is never an input
            logger.warning("mandate_quote_read sink failed", exc_info=True)
        return completed

    if route is None:
        emit(
            "read_failed" if route_error else "unsupported_venue",
            error_class=route_error,
        )
        return None
    try:
        book = await (book_reader or _read_kis_book)(symbol, route[1])
    except Exception as exc:  # noqa: BLE001 - an unread book has no ask
        emit("read_failed", error_class=type(exc).__name__)
        return None
    received = clock()
    book_at = (
        resolve_book_at(book.broker_book_time, book.broker_book_date, received)
        if book.outcome == "ok"
        else None
    )
    outcome: ReadOutcome = book.outcome
    if outcome == "ok" and book_at is None:
        outcome = "no_book_time"
    completed = emit(outcome, book=book, book_at=book_at)
    if (
        outcome != "ok"
        or book.best_ask is None
        or book.current_price is None
        or book_at is None
        or started.utcoffset() is None
    ):
        return None
    ceiling = book.best_ask * (1 + premium_bps / 10_000)
    return ProposalQuote(
        current_price=book.current_price,
        best_ask=book.best_ask,
        # the book's own time; never later than the moment we received it
        ask_observed_at=min(book_at, received),
        tick_size=kr_tick_size_at(ceiling),
        read_at=completed,
        observation_id=observation_id,
    )
