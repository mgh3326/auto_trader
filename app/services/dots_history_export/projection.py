"""Pure mapping from ledger rows to de-identified output rows.

Source rows hold only the columns ``reader.py`` selected. No column that can
carry broker payloads, prose or account identifiers reaches the output: raw
responses, notes, thesis, reason and response messages are never even read.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from app.core.symbol import to_db_symbol
from app.services.dots_history_export.leak_check import is_safe_label
from app.services.dots_history_export.spec import (
    DEFAULT_LOCAL_TIMEZONE,
    LOCAL_TIMEZONES,
    TABLE_BY_NAME,
)
from app.services.execution_ledger import supersede

MIN_SALT_BYTES = 32
_REF_HEX_CHARS = 32
_US_EASTERN = ZoneInfo("America/New_York")

SourceRow = Mapping[str, Any]
OutputRow = dict[str, str | None]

#: Identifiers read only so the leak check can refuse them anywhere in the output.
RAW_IDENTIFIER_FIELDS = (
    "order_no",
    "broker_order_id",
    "client_order_id",
    "original_order_id",
    "replaced_by_order_id",
    "target_broker_order_id",
    "idempotency_key",
    "broker_account_id",
)

_INSTRUMENT_MARKETS = {
    "equity_kr": "kr",
    "equity_us": "us",
    "crypto": "crypto",
    "forex": "forex",
    "index": "index",
}
_MARKET_INSTRUMENTS = {
    "kr": "equity_kr",
    "us": "equity_us",
    "crypto": "crypto",
}
_PROPOSAL_ALIASES = {
    "kis_live": "kis_live",
    "kis_mock": "kis_mock",
    "toss_live": "toss_live",
    "upbit": "upbit_live",
    "db_simulated": "db_paper",
}
_FILL_ALIASES = {
    ("kis", "live"): "kis_live",
    ("kis", "mock"): "kis_mock",
    ("toss", "live"): "toss_live",
    ("upbit", "live"): "upbit_live",
}
_STATUS_OUTCOMES = {
    "filled": "filled",
    "partial": "partial",
    "expired": "expired",
    "cancelled": "cancelled",
    "rejected": "rejected",
    "cancel_rejected": "rejected",
    "replace_rejected": "rejected",
    "replaced": "replaced",
    "accepted": "open",
    "pending": "open",
    "anomaly": "anomaly",
    "unknown": "anomaly",
}
_MOCK_LIFECYCLE_OUTCOMES = {
    "planned": "not_submitted",
    "previewed": "not_submitted",
    "submitted": "open",
    "accepted": "open",
    "pending": "open",
    "stale": "open",
    "fill": "filled",
    "reconciled": "filled",
    "failed": "rejected",
    "anomaly": "anomaly",
    "cancelled": "cancelled",
    "expired": "expired",
}
_ALPACA_ORDER_STATUS_OUTCOMES = {
    "filled": "filled",
    "partially_filled": "partial",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "expired": "expired",
    "done_for_day": "expired",
    "rejected": "rejected",
    "replaced": "replaced",
    "new": "open",
    "accepted": "open",
    "pending_new": "open",
    "accepted_for_bidding": "open",
    "pending_cancel": "open",
    "pending_replace": "open",
    "held": "open",
}
_ALPACA_LIFECYCLE_OUTCOMES = {
    "planned": "not_submitted",
    "previewed": "not_submitted",
    "validated": "not_submitted",
    "stale_preview_cleanup_required": "not_submitted",
    "submitted": "open",
    "filled": "filled",
    "position_reconciled": "filled",
    "sell_validated": "filled",
    "closed": "filled",
    "final_reconciled": "filled",
    "canceled": "cancelled",
    "anomaly": "anomaly",
}
_ALPACA_UNSUBMITTED_RECORD_KINDS = frozenset({"plan", "preview", "validation_attempt"})
_UNFILLED_ENDINGS = frozenset({"cancelled", "expired", "replaced"})
_ALPACA_LIFECYCLE_FILLED = frozenset(
    {"filled", "position_reconciled", "sell_validated", "closed", "final_reconciled"}
)
_KIS_MOCK_FILL_STATES = frozenset({"fill", "reconciled"})
_CRYPTO_QUOTE_PREFIXES = frozenset({"KRW", "USDT", "BTC"})
_REASON_CODE = re.compile(r"[a-z][a-z_]*(?::[a-z][a-z_]*)*")
_REASON_HEAD = re.compile(r"[a-z][a-z_]*")


class SaltError(ValueError):
    """The salt is unusable; the message never contains it."""


class RefHasher:
    """Stable keyed hashes: the same salt and id give the same ref in any table."""

    def __init__(self, salt: bytes) -> None:
        if len(salt) < MIN_SALT_BYTES:
            raise SaltError(f"salt must be at least {MIN_SALT_BYTES} bytes")
        self._salt = salt

    def _digest(self, *parts: str) -> str:
        message = "\x1f".join(parts).encode("utf-8")
        return hmac.new(self._salt, message, hashlib.sha256).hexdigest()

    def ref(self, prefix: str, *parts: str) -> str:
        return f"{prefix}_{self._digest(prefix, *parts)[:_REF_HEX_CHARS]}"

    def fingerprint(self) -> str:
        """Lets two exports prove they share a salt without revealing it."""
        return self._digest("salt-fingerprint")[:12]


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime

    def __contains__(self, when: datetime) -> bool:
        return self.since <= _aware(when) < self.until


@dataclass
class SourceRows:
    """One list per source table, each holding exactly what the reader selected."""

    kis_live_order_ledger: list[SourceRow] = field(default_factory=list)
    live_order_ledger: list[SourceRow] = field(default_factory=list)
    toss_live_order_ledger: list[SourceRow] = field(default_factory=list)
    kis_mock_order_ledger: list[SourceRow] = field(default_factory=list)
    alpaca_paper_order_ledger: list[SourceRow] = field(default_factory=list)
    paper_pending_orders: list[SourceRow] = field(default_factory=list)
    paper_trades: list[SourceRow] = field(default_factory=list)
    execution_ledger: list[SourceRow] = field(default_factory=list)
    order_proposals: list[SourceRow] = field(default_factory=list)
    order_proposal_rungs: list[SourceRow] = field(default_factory=list)


@dataclass
class Projection:
    tables: dict[str, list[OutputRow]]
    forbidden: frozenset[str]
    tags_hashed: Counter[str]


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw).strip()
    return text or None


def _decimal(value: Any) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)), "f")


def _instrument(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _market_from_instrument(value: Any) -> str:
    return _INSTRUMENT_MARKETS.get(_instrument(value), "unknown")


def _paper_symbol_market(symbol: str) -> str:
    """paper_pending_orders has no instrument_type; its symbol form tells."""
    if "-" in symbol and symbol.split("-", 1)[0] in _CRYPTO_QUOTE_PREFIXES:
        return "crypto"
    if len(symbol) == 6 and symbol[:1].isdigit():
        return "kr"
    return "us" if symbol else "unknown"


def _local_tz(market: str) -> str:
    return LOCAL_TIMEZONES.get(market, DEFAULT_LOCAL_TIMEZONE)


def _symbol(symbol: Any, market: str) -> str | None:
    text = _text(symbol)
    if text is None:
        return None
    # Rule: US symbols use the DB dot form, converted only by app/core/symbol.py.
    return to_db_symbol(text) if market == "us" else text


def _side(value: Any) -> str | None:
    text = _text(value)
    return text.lower() if text else None


def _trading_day_cell(
    broker: str, instrument: str, when: datetime | None
) -> str | None:
    """The KIS trading day an order number is unique within, else ``None``."""
    if when is None:
        return None
    day = supersede.order_trading_day(broker, instrument, _aware(when))
    if day is None:
        return None
    if isinstance(day, datetime):
        # A US trading date starts at 20:00 ET on the previous calendar day.
        return (day.astimezone(_US_EASTERN).date() + timedelta(days=1)).isoformat()
    # A KR trading day is the KST date in its compact YYYYMMDD form.
    return datetime.strptime(str(day), "%Y%m%d").date().isoformat()


def _fill_key(
    alias: str, instrument: str, order_id: Any, when: datetime
) -> tuple[Any, ...]:
    broker = alias.split("_", 1)[0]
    return (
        alias,
        supersede.normalize_order_id(order_id),
        supersede.order_trading_day(broker, instrument, _aware(when)),
    )


def _status_outcome(status: Any, filled_qty: Any) -> str:
    return _ending_with_fills(_STATUS_OUTCOMES.get(str(status), "unknown"), filled_qty)


def _ending_with_fills(outcome: str, filled_qty: Any) -> str:
    """A cancelled or expired order that filled some quantity was a partial."""
    if outcome in _UNFILLED_ENDINGS and filled_qty is not None:
        if Decimal(str(filled_qty)) > 0:
            return "partial"
    return outcome


def _alpaca_outcome(row: SourceRow) -> str:
    if row.get("record_kind") in _ALPACA_UNSUBMITTED_RECORD_KINDS:
        return "not_submitted"
    lifecycle = str(row.get("lifecycle_state"))
    # The ledger's own flag outranks whatever status the broker last reported.
    if lifecycle == "anomaly":
        return "anomaly"
    status = str(row.get("order_status") or "").lower()
    outcome = _ALPACA_ORDER_STATUS_OUTCOMES.get(status)
    if outcome is None:
        outcome = _ALPACA_LIFECYCLE_OUTCOMES.get(lifecycle, "unknown")
        # A lifecycle label alone is not fill evidence.
        if lifecycle in _ALPACA_LIFECYCLE_FILLED and not row.get("filled_qty"):
            return "unknown"
    return _ending_with_fills(outcome, row.get("filled_qty"))


def _kis_mock_filled_qty(row: SourceRow) -> Decimal | None:
    """Same rule as the ledger's own reader: attributed quantity, capped.

    The reconciler books a partial as ``fill`` with ``attributed_fill_qty`` in
    its detail. A legacy fill row without that key counts as a full fill.
    """
    if row.get("lifecycle_state") not in _KIS_MOCK_FILL_STATES:
        return None
    ordered = Decimal(str(row["quantity"]))
    raw = row.get("attributed_fill_qty")
    if raw is None:
        return ordered
    try:
        attributed = Decimal(str(raw))
    except InvalidOperation:
        return ordered
    if not attributed.is_finite():
        return ordered
    return max(Decimal("0"), min(attributed, ordered))


def _kis_mock_outcome(row: SourceRow) -> str:
    if row.get("status") == "rejected":
        return "rejected"
    filled = _kis_mock_filled_qty(row)
    if filled is None:
        return _MOCK_LIFECYCLE_OUTCOMES.get(str(row.get("lifecycle_state")), "unknown")
    if filled <= 0:
        return "anomaly"
    return "filled" if filled >= Decimal(str(row["quantity"])) else "partial"


class _Projector:
    def __init__(self, hasher: RefHasher, window: Window) -> None:
        self.hasher = hasher
        self.window = window
        self.tags_hashed: Counter[str] = Counter()
        self.table = ""

    def tag(self, value: Any) -> str | None:
        text = _text(value)
        if text is None:
            return None
        if is_safe_label(text):
            return text
        # Prose, a name or anything identifier-shaped has no place in a label
        # column. A stable hash keeps the grouping and carries none of the text.
        self.tags_hashed[self.table] += 1
        return self.hasher.ref("oth", text)

    def reason_code(self, value: Any) -> str | None:
        """A void reason's code; its detail can be raw exception text.

        ``submit_exception:{exc}`` keeps ``submit_exception``. A reason made only
        of code words, such as the expired-inference marker, is kept whole.
        """
        text = _text(value)
        if text is None:
            return None
        if _REASON_CODE.fullmatch(text):
            return self.tag(text)
        head = text.split(":", 1)[0]
        if _REASON_HEAD.fullmatch(head) and is_safe_label(head):
            self.tags_hashed[self.table] += 1
            return head
        return self.tag(text)

    def order_ref(self, alias: str, order_id: Any) -> str | None:
        normalized = supersede.normalize_order_id(order_id)
        return self.hasher.ref("ord", alias, normalized) if normalized else None

    def key_ref(self, prefix: str, value: Any) -> str | None:
        text = _text(value)
        return self.hasher.ref(prefix, text.lower()) if text else None

    def row_ref(self, row_id: Any) -> str:
        return self.hasher.ref("row", self.table, str(row_id))

    def times(self, tz_name: str, **instants: datetime | None) -> OutputRow:
        zone = ZoneInfo(tz_name)
        cells: OutputRow = {}
        for name, when in instants.items():
            if when is None:
                cells[f"{name}_utc"] = cells[f"{name}_local"] = None
                continue
            instant = _aware(when)
            cells[f"{name}_utc"] = (
                instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
            )
            cells[f"{name}_local"] = instant.astimezone(zone).isoformat()
        return cells

    def order_row(
        self,
        row: SourceRow,
        *,
        alias: str,
        market: str,
        strategy: Any,
        order_id: Any,
        created_at: datetime,
        accepted_at: datetime | None,
        filled_at: datetime | None,
        expired_at: datetime | None,
        cancelled_at: datetime | None,
        outcome: str,
        trading_day: str | None = None,
        **cells: Any,
    ) -> OutputRow:
        tz_name = _local_tz(market)
        output: OutputRow = dict.fromkeys(TABLE_BY_NAME[self.table].column_names)
        correlation = row.get("correlation_id") or row.get("lifecycle_correlation_id")
        output.update(
            row_ref=self.row_ref(row["id"]),
            account_alias=alias,
            market=market,
            symbol=_symbol(cells.pop("symbol"), market),
            side=_side(cells.pop("side")),
            status=self.tag(row.get("status") or row.get("order_status")),
            lifecycle_state=self.tag(row.get("lifecycle_state")),
            terminal_outcome=outcome,
            strategy_tag=self.tag(strategy),
            order_ref=self.order_ref(alias, order_id),
            correlation_ref=self.key_ref("cor", correlation),
            report_item_ref=self.key_ref("rpt", row.get("report_item_uuid")),
            trading_day=trading_day,
            local_tz=tz_name,
        )
        output.update(
            self.times(
                tz_name,
                created_at=created_at,
                accepted_at=accepted_at,
                filled_at=filled_at,
                expired_at=expired_at,
                cancelled_at=cancelled_at,
            )
        )
        for name in ("venue", "order_type", "time_in_force", "currency"):
            output[name] = self.tag(cells.pop(name, None))
        for name in ("operation_kind", "record_kind"):
            output[name] = self.tag(cells.pop(name, None))
        for name in (
            "qty",
            "limit_price",
            "requested_notional",
            "filled_qty",
            "fill_price",
        ):
            output[name] = _decimal(cells.pop(name, None))
        output.update(cells)
        return output


@dataclass(frozen=True)
class _FillTimes:
    """Last in-effect fill per order, so an order row never claims a fill alone."""

    latest: dict[tuple[Any, ...], datetime]

    @classmethod
    def build(cls, fills: Iterable[SourceRow]) -> _FillTimes:
        latest: dict[tuple[Any, ...], datetime] = {}
        for fill in fills:
            alias = _FILL_ALIASES.get((fill["broker"], fill["account_mode"]))
            if alias is None:
                continue
            filled_at = _aware(fill["filled_at"])
            key = _fill_key(
                alias,
                _instrument(fill["instrument_type"]),
                fill["broker_order_id"],
                filled_at,
            )
            if key not in latest or filled_at > latest[key]:
                latest[key] = filled_at
        return cls(latest)

    def lookup(
        self, alias: str, instrument: str, order_id: Any, when: datetime
    ) -> datetime | None:
        if not order_id:
            return None
        return self.latest.get(_fill_key(alias, instrument, order_id, when))


def _forbidden_values(sources: SourceRows) -> frozenset[str]:
    found: set[str] = set()
    for rows in vars(sources).values():
        for row in rows:
            for name in RAW_IDENTIFIER_FIELDS:
                value = str(row.get(name) or "").strip()
                if value:
                    found.add(value)
    return frozenset(found)


def _recorded_at(row: SourceRow, state: Any, wanted: str) -> datetime | None:
    """When reconcile recorded this ending; these ledgers keep no broker time."""
    return row.get("reconciled_at") if state == wanted else None


def _project_kis_live(
    p: _Projector, rows: Iterable[SourceRow], fills: _FillTimes
) -> list[OutputRow]:
    output = []
    for row in rows:
        instrument = _instrument(row["instrument_type"])
        market = _market_from_instrument(instrument)
        created = row["created_at"]
        order_no = row.get("order_no")
        outcome = _status_outcome(row["status"], row.get("filled_qty"))
        output.append(
            p.order_row(
                row,
                alias="kis_live",
                market=market,
                strategy=row.get("strategy"),
                order_id=order_no,
                created_at=created,
                accepted_at=(
                    row.get("trade_date")
                    if order_no and row["status"] != "rejected"
                    else None
                ),
                filled_at=fills.lookup("kis_live", instrument, order_no, created),
                expired_at=_recorded_at(row, row["status"], "expired"),
                cancelled_at=_recorded_at(row, row["status"], "cancelled"),
                outcome=outcome,
                trading_day=_trading_day_cell("kis", instrument, created),
                symbol=row["symbol"],
                side=row["side"],
                order_type=row.get("order_type"),
                qty=row.get("quantity"),
                limit_price=row.get("price"),
                requested_notional=row.get("amount"),
                currency=row.get("currency"),
                filled_qty=row.get("filled_qty"),
                fill_price=row.get("avg_fill_price"),
            )
        )
    return output


def _project_live(
    p: _Projector, rows: Iterable[SourceRow], fills: _FillTimes
) -> list[OutputRow]:
    output = []
    for row in rows:
        market = str(row["market"])
        instrument = _MARKET_INSTRUMENTS.get(market, "")
        alias = str(row["account_scope"])
        created = row["created_at"]
        order_no = row.get("order_no")
        outcome = _status_outcome(row["status"], row.get("filled_qty"))
        output.append(
            p.order_row(
                row,
                alias=alias,
                market=market,
                strategy=row.get("strategy"),
                order_id=order_no,
                created_at=created,
                accepted_at=(
                    row.get("trade_date")
                    if order_no and row["status"] != "rejected"
                    else None
                ),
                filled_at=fills.lookup(alias, instrument, order_no, created),
                expired_at=_recorded_at(row, row["status"], "expired"),
                cancelled_at=_recorded_at(row, row["status"], "cancelled"),
                outcome=outcome,
                trading_day=_trading_day_cell(str(row["broker"]), instrument, created),
                symbol=row["symbol"],
                side=row["side"],
                venue=row.get("exchange"),
                order_type=row.get("order_kind"),
                qty=row.get("quantity"),
                limit_price=row.get("price"),
                requested_notional=row.get("amount"),
                currency=row.get("currency"),
                filled_qty=row.get("filled_qty"),
                fill_price=row.get("avg_fill_price"),
            )
        )
    return output


def _project_toss(
    p: _Projector, rows: Iterable[SourceRow], fills: _FillTimes
) -> list[OutputRow]:
    output = []
    for row in rows:
        market = str(row["market"])
        instrument = _MARKET_INSTRUMENTS.get(market, "")
        created = row["created_at"]
        order_id = row.get("broker_order_id")
        outcome = _status_outcome(row["status"], row.get("filled_qty"))
        output.append(
            p.order_row(
                row,
                alias="toss_live",
                market=market,
                strategy=row.get("strategy"),
                order_id=order_id,
                created_at=created,
                accepted_at=(
                    row.get("trade_date")
                    if order_id and outcome != "rejected"
                    else None
                ),
                filled_at=fills.lookup("toss_live", instrument, order_id, created),
                expired_at=row.get("expired_at"),
                cancelled_at=_recorded_at(row, row["status"], "cancelled"),
                outcome=outcome,
                symbol=row["symbol"],
                side=row["side"],
                operation_kind=row.get("operation_kind"),
                order_type=row.get("order_type"),
                time_in_force=row.get("time_in_force"),
                qty=row.get("quantity"),
                limit_price=row.get("price"),
                requested_notional=row.get("order_amount"),
                currency=row.get("currency"),
                filled_qty=row.get("filled_qty"),
                fill_price=row.get("avg_fill_price"),
                original_order_ref=p.order_ref(
                    "toss_live", row.get("original_order_id")
                ),
                replaced_by_order_ref=p.order_ref(
                    "toss_live", row.get("replaced_by_order_id")
                ),
            )
        )
    return output


def _project_kis_mock(
    p: _Projector, rows: Iterable[SourceRow], fills: _FillTimes
) -> list[OutputRow]:
    output = []
    for row in rows:
        instrument = _instrument(row["instrument_type"])
        market = _market_from_instrument(instrument)
        created = row["created_at"]
        order_no = row.get("order_no")
        outcome = _kis_mock_outcome(row)
        output.append(
            p.order_row(
                row,
                alias="kis_mock",
                market=market,
                strategy=row.get("strategy"),
                order_id=order_no,
                created_at=created,
                accepted_at=row.get("trade_date")
                if row.get("status") == "accepted"
                else None,
                filled_at=fills.lookup("kis_mock", instrument, order_no, created),
                expired_at=_recorded_at(row, row["lifecycle_state"], "expired"),
                cancelled_at=_recorded_at(row, row["lifecycle_state"], "cancelled"),
                outcome=outcome,
                trading_day=_trading_day_cell("kis", instrument, created),
                symbol=row["symbol"],
                side=row["side"],
                order_type=row.get("order_type"),
                qty=row.get("quantity"),
                limit_price=row.get("price"),
                requested_notional=row.get("amount"),
                currency=row.get("currency"),
                filled_qty=_kis_mock_filled_qty(row),
            )
        )
    return output


def _project_alpaca(p: _Projector, rows: Iterable[SourceRow]) -> list[OutputRow]:
    output = []
    for row in rows:
        market = _market_from_instrument(row["instrument_type"])
        order_id = row.get("broker_order_id")
        outcome = _alpaca_outcome(row)
        output.append(
            p.order_row(
                row,
                alias=str(row["account_mode"]),
                market=market,
                strategy=row.get("strategy_key"),
                order_id=order_id,
                created_at=row["created_at"],
                accepted_at=row.get("submitted_at") if order_id else None,
                # The ledger keeps no fill time column; the fill's own time sits
                # in raw_responses, which this tool does not read.
                filled_at=None,
                expired_at=None,
                cancelled_at=row.get("canceled_at"),
                outcome=outcome,
                symbol=row["execution_symbol"],
                side=row["side"],
                venue=row.get("execution_venue"),
                record_kind=row.get("record_kind"),
                order_type=row.get("order_type"),
                time_in_force=row.get("time_in_force"),
                qty=row.get("requested_qty"),
                limit_price=row.get("requested_price"),
                requested_notional=row.get("requested_notional"),
                currency=row.get("currency"),
                filled_qty=row.get("filled_qty"),
                fill_price=row.get("filled_avg_price"),
            )
        )
    return output


def _paper_order_ref(p: _Projector, kind: str, row_id: Any) -> str:
    return p.hasher.ref("ord", "db_paper", f"{kind}:{row_id}")


def _project_paper_pending(p: _Projector, rows: Iterable[SourceRow]) -> list[OutputRow]:
    outcomes = {"pending": "open", "filled": "filled", "cancelled": "cancelled"}
    output = []
    for row in rows:
        symbol = str(row["symbol"])
        status = str(row["status"])
        cell = p.order_row(
            row,
            alias="db_paper",
            market=_paper_symbol_market(symbol),
            strategy=row.get("strategy_name"),
            order_id=None,
            created_at=row["placed_at"],
            accepted_at=row["placed_at"],
            filled_at=row.get("filled_at"),
            expired_at=None,
            cancelled_at=row.get("cancelled_at"),
            outcome=outcomes.get(status, "unknown"),
            symbol=symbol,
            side=row["side"],
            venue="paper_sim",
            order_type=row.get("order_type"),
            qty=row.get("quantity"),
            limit_price=row.get("limit_price"),
            filled_qty=row.get("quantity") if status == "filled" else None,
            fill_price=row.get("fill_price"),
            account_ref=p.hasher.ref("acc", "db_paper", str(row["account_id"])),
        )
        cell["order_ref"] = _paper_order_ref(p, "pending", row["id"])
        output.append(cell)
    return output


def _project_paper_trades(p: _Projector, rows: Iterable[SourceRow]) -> list[OutputRow]:
    output = []
    for row in rows:
        executed = row["executed_at"]
        pending_id = row.get("pending_order_id")
        limit = row.get("price") if row.get("order_type") == "limit" else None
        cell = p.order_row(
            row,
            alias="db_paper",
            market=_market_from_instrument(row["instrument_type"]),
            strategy=row.get("strategy_name"),
            order_id=None,
            created_at=executed,
            accepted_at=executed,
            filled_at=executed,
            expired_at=None,
            cancelled_at=None,
            outcome="filled",
            symbol=row["symbol"],
            side=row["side"],
            venue="paper_sim",
            order_type=row.get("order_type"),
            qty=row.get("quantity"),
            limit_price=limit,
            currency=row.get("currency"),
            filled_qty=row.get("quantity"),
            fill_price=row.get("price"),
            account_ref=p.hasher.ref("acc", "db_paper", str(row["account_id"])),
        )
        cell["status"] = "executed"
        cell["order_ref"] = (
            _paper_order_ref(p, "pending", pending_id)
            if pending_id is not None
            else _paper_order_ref(p, "trade", row["id"])
        )
        output.append(cell)
    return output


def _project_fills(
    p: _Projector, rows: list[SourceRow], window: Window
) -> list[OutputRow]:
    in_window = [row for row in rows if _aware(row["filled_at"]) in window]
    # Coverage counts only exported rows. A reconciler row outside the window must
    # not flag the one exported row of a fill, or filtering would lose the fill.
    covered = supersede.covered_identities(SimpleNamespace(**row) for row in in_window)
    output = []
    for row in in_window:
        filled_at = _aware(row["filled_at"])
        instrument = _instrument(row["instrument_type"])
        market = _market_from_instrument(instrument)
        alias = _FILL_ALIASES.get(
            (row["broker"], row["account_mode"]),
            f"{row['broker']}_{row['account_mode']}",
        )
        tz_name = _local_tz(market)
        cell: OutputRow = {
            "row_ref": p.row_ref(row["id"]),
            "account_alias": alias,
            "market": market,
            "venue": p.tag(row.get("venue")),
            "symbol": _symbol(row["symbol"], market),
            "side": _side(row["side"]),
            "filled_qty": _decimal(row["filled_qty"]),
            "fill_price": _decimal(row["filled_price"]),
            "currency": p.tag(row.get("currency")),
            "fill_source": _text(row["source"]),
            "superseded_duplicate": str(
                supersede.is_superseded(SimpleNamespace(**row), covered)
            ).lower(),
            "order_ref": p.order_ref(alias, row["broker_order_id"]),
            "correlation_ref": p.key_ref("cor", row.get("correlation_id")),
            "trading_day": _trading_day_cell(str(row["broker"]), instrument, filled_at),
            "local_tz": tz_name,
        }
        cell.update(p.times(tz_name, filled_at=filled_at, created_at=row["created_at"]))
        output.append(cell)
    return output


def _project_proposals(p: _Projector, rows: Iterable[SourceRow]) -> list[OutputRow]:
    output = []
    for row in rows:
        market = _market_from_instrument(row["market"])
        alias = _PROPOSAL_ALIASES.get(
            str(row["account_mode"]), str(row["account_mode"])
        )
        tz_name = _local_tz(market)
        cell: OutputRow = {
            "row_ref": p.row_ref(row["id"]),
            "proposal_ref": p.key_ref("prp", row["proposal_id"]),
            "root_proposal_ref": p.key_ref("prp", row.get("root_proposal_id")),
            "revision": _text(row.get("revision")),
            "account_alias": alias,
            "market": market,
            "symbol": _symbol(row["symbol"], market),
            "side": _side(row["side"]),
            "order_type": p.tag(row.get("order_type")),
            "action": p.tag(row.get("action")),
            "lifecycle_state": p.tag(row.get("lifecycle_state")),
            "strategy_tag": p.tag(row.get("strategy")),
            "exit_intent": p.tag(row.get("exit_intent")),
            "void_reason": p.reason_code(row.get("void_reason")),
            "target_order_ref": p.order_ref(alias, row.get("target_broker_order_id")),
            "correlation_ref": p.key_ref("cor", row.get("correlation_id")),
            "local_tz": tz_name,
        }
        cell.update(
            p.times(
                tz_name,
                created_at=row["created_at"],
                validated_at=row.get("validated_at"),
                approved_at=row.get("approved_at"),
                valid_until=row.get("valid_until"),
            )
        )
        output.append(cell)
    return output


def _project_rungs(p: _Projector, rows: Iterable[SourceRow]) -> list[OutputRow]:
    output = []
    for row in rows:
        market = _market_from_instrument(row["market"])
        alias = _PROPOSAL_ALIASES.get(
            str(row["account_mode"]), str(row["account_mode"])
        )
        tz_name = _local_tz(market)
        cell: OutputRow = {
            "row_ref": p.row_ref(row["id"]),
            "rung_ref": p.hasher.ref("rng", str(row["id"])),
            "proposal_ref": p.key_ref("prp", row["proposal_id"]),
            "rung_index": _text(row["rung_index"]),
            "account_alias": alias,
            "market": market,
            "symbol": _symbol(row["symbol"], market),
            "side": _side(row["side"]),
            "qty": _decimal(row.get("quantity")),
            "limit_price": _decimal(row.get("limit_price")),
            "requested_notional": _decimal(row.get("notional")),
            "state": p.tag(row.get("state")),
            "filled_qty": _decimal(row.get("filled_qty")),
            "void_reason": p.reason_code(row.get("void_reason")),
            "void_reason_group": p.tag(row.get("void_reason_group")),
            "order_ref": p.order_ref(alias, row.get("broker_order_id")),
            "correlation_ref": p.key_ref("cor", row.get("correlation_id")),
            "local_tz": tz_name,
        }
        cell.update(
            p.times(
                tz_name,
                created_at=row["created_at"],
                validated_at=row.get("validated_at"),
                updated_at=row.get("updated_at"),
            )
        )
        output.append(cell)
    return output


def project(sources: SourceRows, *, hasher: RefHasher, window: Window) -> Projection:
    projector = _Projector(hasher, window)
    fills = _FillTimes.build(sources.execution_ledger)
    builders = {
        "kis_live_order_ledger": lambda: _project_kis_live(
            projector, sources.kis_live_order_ledger, fills
        ),
        "live_order_ledger": lambda: _project_live(
            projector, sources.live_order_ledger, fills
        ),
        "toss_live_order_ledger": lambda: _project_toss(
            projector, sources.toss_live_order_ledger, fills
        ),
        "kis_mock_order_ledger": lambda: _project_kis_mock(
            projector, sources.kis_mock_order_ledger, fills
        ),
        "alpaca_paper_order_ledger": lambda: _project_alpaca(
            projector, sources.alpaca_paper_order_ledger
        ),
        "paper_pending_orders": lambda: _project_paper_pending(
            projector, sources.paper_pending_orders
        ),
        "paper_trades": lambda: _project_paper_trades(projector, sources.paper_trades),
        "execution_ledger": lambda: _project_fills(
            projector, sources.execution_ledger, window
        ),
        "order_proposals": lambda: _project_proposals(
            projector, sources.order_proposals
        ),
        "order_proposal_rungs": lambda: _project_rungs(
            projector, sources.order_proposal_rungs
        ),
    }
    tables: dict[str, list[OutputRow]] = {}
    for name, build in builders.items():
        projector.table = name
        tables[name] = build()
    return Projection(
        tables=tables,
        forbidden=_forbidden_values(sources),
        tags_hashed=projector.tags_hashed,
    )
