"""ROB-1379 — one order identity for cross-source duplicate fills.

The ledger unique key ``(broker, account_mode, venue, broker_order_id,
fill_seq)`` is the idempotency key and stays exactly as it is. It cannot see
cross-source duplicates because every writer derives its own ``fill_seq`` for
the same execution: fillwire hashes the whole H0STCNI0 frame, the in-app
websocket monitor hashes its own seed, and the reconciler hashes
``ord_dt|ord_tmd|...`` of the broker's daily order row. So one fill can land as
a websocket row *and* a reconciler row.

This module owns the order-level identity that links those rows and the two
rules built on it:

* **Read side (``supersede_provisional``).** A ``websocket`` row is provisional.
  Once an authoritative row (``reconciler`` / ``manual_import``) exists for the
  same order identity, every websocket row of that order is a duplicate and is
  not counted. The rows stay on disk; quantity readers simply do not add them.
* **Write side (``terminal_cover``).** A websocket fill that arrives after the
  reconciler already booked its order as *terminal* (the broker said nothing of
  the order can fill any more) adds no information, so the writer does not
  insert it and answers ``unchanged`` with the authoritative row id. A fill of
  an order that may still be live is always inserted — a new partial fill must
  never be lost because an older partial was already reconciled.

The identity deliberately ignores ``fill_seq``, ``venue`` and the exact
timestamp: the writers derive them independently (for KIS US the websocket
event carries no exchange code, so the monitor stores ``krx`` while the
reconciler stores ``NASD``). A KIS order number is reused across days, so for
KIS the identity carries the trading day (KST date for KR, the US trading date
for US — the same rule as ``kis_lots._supersede_key`` for US). Upbit order ids
are UUIDs and carry no day.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.core.symbol import to_db_symbol
from app.core.timezone import trade_day_kst

PROVISIONAL_SOURCE = "websocket"
AUTHORITATIVE_SOURCES = frozenset({"reconciler", "manual_import"})

#: Upbit order states the reconciler books; both are terminal (no further fill).
UPBIT_TERMINAL_STATES = frozenset({"done", "cancel"})

OrderIdentity = tuple[Any, ...]


class LedgerFillLike(Protocol):
    broker: str
    account_mode: str
    instrument_type: Any
    symbol: str
    side: str
    broker_order_id: str
    currency: str
    filled_at: datetime
    source: str


def normalize_order_id(order_id: object) -> str:
    """Leading-zero-normalized order id (same rule as query_service/kis_lots)."""
    raw = str(order_id or "").strip()
    return raw.lstrip("0") or raw


def _instrument(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _symbol_key(instrument_type: str, symbol: object) -> str:
    text = str(symbol or "").strip()
    if instrument_type == "equity_us":
        # BRK/B, BRK-B and BRK.B are one US symbol (app/core/symbol.py only).
        return to_db_symbol(text).upper()
    return text.upper()


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _trading_day(broker: str, instrument_type: str, filled_at: datetime) -> Any:
    if broker != "kis":
        return None
    if instrument_type == "equity_us":
        # Imported lazily: kis_lots imports the repository, which imports us.
        from app.services.execution_ledger.kis_lots import us_trading_day_window

        return us_trading_day_window(_aware(filled_at))[0]
    return trade_day_kst(_aware(filled_at))


def order_identity(row: LedgerFillLike) -> OrderIdentity:
    """The cross-source identity of the broker order one ledger row belongs to."""
    instrument_type = _instrument(row.instrument_type)
    return (
        row.broker,
        row.account_mode,
        instrument_type,
        _symbol_key(instrument_type, row.symbol),
        row.side,
        str(row.currency or "").upper(),
        normalize_order_id(row.broker_order_id),
        _trading_day(row.broker, instrument_type, row.filled_at),
    )


def is_authoritative(row: LedgerFillLike) -> bool:
    return row.source in AUTHORITATIVE_SOURCES


def covered_identities(rows: Iterable[LedgerFillLike]) -> set[OrderIdentity]:
    """Order identities some authoritative row books."""
    return {order_identity(row) for row in rows if is_authoritative(row)}


def is_superseded(row: LedgerFillLike, covered: set[OrderIdentity]) -> bool:
    """A websocket row whose order an authoritative row already books."""
    return row.source == PROVISIONAL_SOURCE and order_identity(row) in covered


def supersede_provisional[R: LedgerFillLike](
    rows: Sequence[R], *, authority: Iterable[LedgerFillLike] | None = None
) -> tuple[list[R], int]:
    """Return ``(rows without superseded websocket rows, superseded count)``.

    ``authority`` defaults to ``rows``; a reader whose sum window is narrower
    than the order's lifetime passes a wider authoritative set so a reconciler
    row stamped at order time can still cover a websocket fill stamped later.
    Input order is preserved.
    """
    covered = covered_identities(rows if authority is None else authority)
    kept = [row for row in rows if not is_superseded(row, covered)]
    return kept, len(rows) - len(kept)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def terminal_final_quantity(row: Any) -> Decimal | None:
    """Final cumulative filled quantity when ``row`` proves its order is terminal.

    Only a ``reconciler`` row carrying the broker's own order row can prove it:

    * KIS KR (``inquire-daily-ccld``): ``rmn_qty`` is exactly 0 and the row is
      the order aggregate (``tot_ccld_qty`` equals the booked quantity);
    * KIS US (overseas daily orders): ``nccs_qty`` is exactly 0 and
      ``ft_ccld_qty`` equals the booked quantity;
    * Upbit: the order ``state`` is ``done``/``cancel`` and ``executed_volume``
      is the order's cumulative fill.

    Anything else — a still-live order, a missing or unreadable field, a
    per-fill row, a manual import — is ``None``: not proven terminal.
    """
    if getattr(row, "source", None) != "reconciler":
        return None
    raw = getattr(row, "raw_payload_json", None)
    if not isinstance(raw, dict):
        return None
    broker = getattr(row, "broker", None)
    instrument_type = _instrument(getattr(row, "instrument_type", None))
    booked = _decimal(getattr(row, "filled_qty", None))
    if booked is None or booked <= 0:
        return None
    if broker == "kis" and instrument_type in ("equity_kr", "equity_us"):
        remaining_key, total_key = (
            ("rmn_qty", "tot_ccld_qty")
            if instrument_type == "equity_kr"
            else ("nccs_qty", "ft_ccld_qty")
        )
        remaining = _decimal(raw.get(remaining_key))
        total = _decimal(raw.get(total_key))
        if remaining is None or remaining != 0:
            return None
        if total is None or total <= 0 or total != booked:
            return None
        return total
    if broker == "upbit" and instrument_type == "crypto":
        if raw.get("state") not in UPBIT_TERMINAL_STATES:
            return None
        total = _decimal(raw.get("executed_volume"))
        if total is None or total <= 0 or booked > total:
            return None
        return total
    return None


def terminal_cover(fill: LedgerFillLike, candidates: Iterable[Any]) -> Any | None:
    """The authoritative row that already books websocket ``fill``, or ``None``.

    ``candidates`` are in-effect authoritative rows the caller pre-filtered by
    order id. A candidate covers the fill only when it has the same order
    identity, proves its order terminal, and that final quantity can contain
    the fill. The lowest id wins so a replay always answers the same row.
    """
    if fill.source != PROVISIONAL_SOURCE:
        return None
    qty = _decimal(getattr(fill, "filled_qty", None))
    if qty is None or qty <= 0:
        return None
    identity = order_identity(fill)
    covering = []
    for candidate in candidates:
        if not is_authoritative(candidate) or order_identity(candidate) != identity:
            continue
        final = terminal_final_quantity(candidate)
        if final is not None and qty <= final:
            covering.append(candidate)
    if not covering:
        return None
    return min(covering, key=lambda row: int(row.id))
