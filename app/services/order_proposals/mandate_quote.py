"""Read-only venue sell-side best ask for mandate plan proposals (3795 T4d).

The mandate near-ask band (design §4.1 condition 6) is measured against the
venue's own best ask, never against ``current_price``. This module is the only
place that reads that ask:

* a venue has a reader only when its book is the book the order meets. A venue
  without one has **no ask** — there is no substitute (``current_price``, the
  Toss/KIS ``current_price`` fallback of §2.3 F7, another venue's book);
* the ask is an observation: its time is the instant the request *started*
  (earliest possible book state, so the age the gate sees is never understated),
  and every read — also a failed or refused one — leaves a closed-vocabulary
  ``QuoteReadRecord`` (Q10 evidence E1: read -> send elapsed time);
* the module only reads. It imports no order, ledger or approval module
  (``tests/services/order_proposals/test_mandate_quote.py``).

Freshness is *not* decided here: ``proposal_gate.check_near_ask`` applies the
5 second bound at the use site.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal

from app.services.mandate.proposal_gate import PlanProposalContext, ProposalQuote

logger = logging.getLogger(__name__)

READ_OUTCOMES: Final = (
    "ok",
    "no_ask",
    "invalid_ask",
    "crossed_book",
    "no_price",
    "unsupported_venue",
    "read_failed",
)
ReadOutcome = Literal[
    "ok",
    "no_ask",
    "invalid_ask",
    "crossed_book",
    "no_price",
    "unsupported_venue",
    "read_failed",
]

# ``(account_mode, market)`` -> KIS ``FID_COND_MRKT_DIV_CODE``. Kiwoom's mock
# account is KRX-only (``brokers/kiwoom/constants.py``), so the KRX book is the
# book its order meets. KIS live / Toss route through SOR (a different book),
# US has no reader here: they are deliberately absent (fail-closed).
KIS_BOOK_BY_ACCOUNT: Final[dict[tuple[str, str], tuple[str, str]]] = {
    ("kiwoom_mock", "equity_kr"): ("krx", "J"),
}


@dataclass(frozen=True)
class QuoteReadRecord:
    """One venue ask read, for Q10 evidence E1 (no account number, no secret)."""

    account_mode: str
    market: str
    symbol: str
    venue: str | None
    source: str | None
    outcome: ReadOutcome
    observed_at: datetime
    read_completed_at: datetime
    read_ms: int
    best_ask: str | None = None
    best_bid: str | None = None
    broker_book_time: str | None = None
    error_class: str | None = None

    def to_json(self) -> str:
        row = asdict(self)
        row["observed_at"] = self.observed_at.isoformat()
        row["read_completed_at"] = self.read_completed_at.isoformat()
        return json.dumps(row, sort_keys=True, separators=(",", ":"))


RecordSink = Callable[[QuoteReadRecord], None]


def log_sink(record: QuoteReadRecord) -> None:
    logger.info("mandate_quote_read %s", record.to_json())


class JsonlSink:
    """Append one JSON line per read. Never raises: a record is not an input."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __call__(self, record: QuoteReadRecord) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(record.to_json() + "\n")
        except OSError:
            logger.warning("mandate_quote_read record not written", exc_info=True)


@dataclass(frozen=True)
class VenueBook:
    """Closed, parsed result of one KIS orderbook read."""

    best_ask: Decimal | None
    best_bid: Decimal | None
    broker_book_time: str | None
    current_price: Decimal | None
    outcome: ReadOutcome


def _positive_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _levels(output: dict[str, Any], prefix: str) -> list[tuple[Decimal, Decimal]]:
    levels: list[tuple[Decimal, Decimal]] = []
    for idx in range(1, 11):
        price = _positive_decimal(output.get(f"{prefix}p{idx}"))
        if price is None:
            continue
        raw_quantity = output.get(f"{prefix}p_rsqn{idx}")
        if raw_quantity is None:
            raw_quantity = output.get(f"{prefix}p{idx}_rsqn")
        quantity = _positive_decimal(raw_quantity)
        if quantity is None:  # a level with no resting quantity is not an ask
            continue
        levels.append((price, quantity))
    return levels


def parse_kis_book(output1: dict[str, Any], current_price_raw: Any = None) -> VenueBook:
    """Best ask = lowest ask level that has a positive price and quantity.

    A book with no ask, or whose best bid is not below its best ask (locked or
    crossed: a half-updated or contradictory book), has no usable ask.
    """
    broker_time = output1.get("aspr_acpt_hour")
    broker_book_time = str(broker_time).strip() if broker_time else None
    asks = _levels(output1, "ask")
    bids = _levels(output1, "bid")
    best_bid = max((price for price, _ in bids), default=None)
    price = _positive_decimal(current_price_raw)
    if not asks:
        # Distinguish an empty book from garbage prices in the raw fields.
        raw_prices = [output1.get(f"askp{idx}") for idx in range(1, 11)]
        invalid = any(
            raw not in (None, "", 0, "0") and _positive_decimal(raw) is None
            for raw in raw_prices
        )
        return VenueBook(
            None,
            best_bid,
            broker_book_time,
            price,
            "invalid_ask" if invalid else "no_ask",
        )
    best_ask = min(price_ for price_, _ in asks)
    if best_bid is not None and best_bid >= best_ask:
        return VenueBook(None, best_bid, broker_book_time, price, "crossed_book")
    if price is None:
        return VenueBook(best_ask, best_bid, broker_book_time, None, "no_price")
    return VenueBook(best_ask, best_bid, broker_book_time, price, "ok")


async def _read_kis_krx(code: str, market_code: str) -> VenueBook:
    # Imported here: the KIS client pulls the whole broker stack, and this module
    # is imported by the proposal service.
    from app.services.brokers.kis.client import KISClient

    kis = KISClient()
    output1, _output2 = await kis.inquire_orderbook_snapshot(code, market_code)
    price_evidence = await kis.inquire_price_raw_evidence(code, market_code)
    return parse_kis_book(output1, price_evidence.get("stck_prpr"))


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
    route = KIS_BOOK_BY_ACCOUNT.get((account_mode, market))

    def emit(
        outcome: ReadOutcome,
        *,
        book: VenueBook | None = None,
        error_class: str | None = None,
    ) -> datetime:
        completed = clock()
        try:
            sink(
                QuoteReadRecord(
                    account_mode=account_mode,
                    market=market,
                    symbol=symbol,
                    venue=route[0] if route else None,
                    source="kis" if route else None,
                    outcome=outcome,
                    observed_at=started,
                    read_completed_at=completed,
                    read_ms=int((completed - started).total_seconds() * 1000),
                    best_ask=format(book.best_ask, "f")
                    if book and book.best_ask
                    else None,
                    best_bid=format(book.best_bid, "f")
                    if book and book.best_bid
                    else None,
                    broker_book_time=book.broker_book_time if book else None,
                    error_class=error_class,
                )
            )
        except Exception:  # noqa: BLE001 - the record is never an input
            logger.warning("mandate_quote_read sink failed", exc_info=True)
        return completed

    if route is None:
        emit("unsupported_venue")
        return None
    try:
        book = await (book_reader or _read_kis_krx)(symbol, route[1])
    except Exception as exc:  # noqa: BLE001 - an unread book has no ask
        emit("read_failed", error_class=type(exc).__name__)
        return None
    completed = emit(book.outcome, book=book)
    if (
        book.outcome != "ok"
        or book.best_ask is None
        or book.current_price is None
        or started.utcoffset() is None
    ):
        return None
    ceiling = book.best_ask * (1 + premium_bps / 10_000)
    return ProposalQuote(
        current_price=book.current_price,
        best_ask=book.best_ask,
        ask_observed_at=started,
        tick_size=kr_tick_size_at(ceiling),
        read_at=completed,
    )
