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
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from app.core.symbol import to_db_symbol
from app.services.dots_history_export.leak_check import is_safe_label
from app.services.dots_history_export.spec import (
    BALANCE_SOURCES,
    BALANCE_UNAVAILABLE,
    BALANCE_UNAVAILABLE_REASON,
    DEFAULT_LOCAL_TIMEZONE,
    LOCAL_TIMEZONES,
    Mode,
    tables_for,
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
#: In real mode the order ids are output, so only a full account number is refused.
REAL_IDENTIFIER_FIELDS = ("broker_account_id",)

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
_REASON_HEAD = re.compile(r"[a-z][a-z_]*")
#: The one void reason whose text after the colon is part of the code. Copied here
#: to keep this tool's imports clear of the broker package; a test pins it to
#: ``kis_leftover_inference.EXPIRED_INFERENCE_VOID_REASON``.
EXPIRED_INFERENCE_MARKER = "expired_inference:kis_regular_day_order_no_broker_original"


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
    paper_daily_snapshots: list[SourceRow] = field(default_factory=list)
    paper_reservations: list[SourceRow] = field(default_factory=list)
    crypto_daily_candles: list[SourceRow] = field(default_factory=list)


@dataclass
class Projection:
    tables: dict[str, list[OutputRow]]
    forbidden: frozenset[str]
    tags_hashed: Counter[str]
    mode: Mode = Mode.DEIDENTIFIED


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


#: Parquet stores decimals as ``decimal128(38, 12)``. A candle close read from a
#: bare ``Numeric`` column can carry more precision than that, so it is rounded.
_DECIMAL_PLACES = Decimal("0.000000000001")


def _decimal_scaled(value: Any) -> str | None:
    if value is None:
        return None
    return format(
        Decimal(str(value)).quantize(_DECIMAL_PLACES, rounding=ROUND_HALF_UP), "f"
    )


def _plain_text(value: str) -> str:
    """Drop line breaks and control characters; keep the words as written."""
    return " ".join(value.split())


def _masked_account(value: str) -> str:
    """The last digits of an account identifier, never the whole number."""
    compact = re.sub(r"[^0-9A-Za-z]", "", value)
    return f"****{compact[-4:]}"


def _pct(numerator: Decimal, denominator: Decimal) -> str | None:
    if denominator <= 0 or not numerator.is_finite() or not denominator.is_finite():
        return None
    return format(((numerator / denominator) - Decimal(1)) * Decimal(100), ".4f")


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
    def __init__(
        self, hasher: RefHasher | None, window: Window, *, real: bool = False
    ) -> None:
        self.hasher = hasher
        self.window = window
        self.real = real
        self.tags_hashed: Counter[str] = Counter()
        self.table = ""
        self.specs = {
            spec.name: spec
            for spec in tables_for(Mode.REAL if real else Mode.DEIDENTIFIED)
        }

    def _hash(self, prefix: str, *parts: str) -> str:
        if self.hasher is None:  # pragma: no cover - guarded by ``real``
            raise RuntimeError("the de-identified mode needs a salt")
        return self.hasher.ref(prefix, *parts)

    def tag(self, value: Any) -> str | None:
        text = _text(value)
        if text is None:
            return None
        if self.real:
            return _plain_text(text)
        if is_safe_label(text):
            return text
        # Prose, a name or anything identifier-shaped has no place in a label
        # column. A stable hash keeps the grouping and carries none of the text.
        self.tags_hashed[self.table] += 1
        return self._hash("oth", text)

    def reason_code(self, value: Any) -> str | None:
        """A void reason's code; the text after a colon can be anything.

        ``submit_exception:{exc}`` keeps ``submit_exception``, even when the detail
        looks like a code word. Only the expired-inference marker is kept whole.
        """
        text = _text(value)
        if text is None:
            return None
        if self.real:
            return _plain_text(text)
        if text == EXPIRED_INFERENCE_MARKER:
            return text
        head, separator, _detail = text.partition(":")
        if _REASON_HEAD.fullmatch(head) and is_safe_label(head):
            if separator:
                self.tags_hashed[self.table] += 1
            return head
        if separator:
            # No recognizable code, so a safe-looking whole value is not safe either.
            self.tags_hashed[self.table] += 1
            return self._hash("oth", text)
        return self.tag(text)

    def order_ref(self, alias: str, order_id: Any) -> str | None:
        if self.real:
            # KIS reuses numeric order numbers with leading zeros, so those are
            # normalized to join their fills. A UUID or base64 broker id is not a
            # number and must be copied exactly, or the join and the id both break.
            raw = str(order_id or "").strip()
            if not raw:
                return None
            return (raw.lstrip("0") or "0") if raw.isdigit() else raw
        normalized = supersede.normalize_order_id(order_id)
        if not normalized:
            return None
        return self._hash("ord", alias, normalized)

    def key_ref(self, prefix: str, value: Any) -> str | None:
        text = _text(value)
        if text is None:
            return None
        return _plain_text(text) if self.real else self._hash(prefix, text.lower())

    def account_ref(self, alias: str, value: Any) -> str | None:
        text = _text(value)
        if text is None:
            return None
        if self.real:
            return _masked_account(text)
        return self._hash("acc", alias, text)

    def row_ref(self, row_id: Any, *extra: Any) -> str:
        if self.real:
            suffix = "".join(f":{item}" for item in extra)
            return f"{self.table}:{row_id}{suffix}"
        return self._hash("row", self.table, str(row_id), *(str(x) for x in extra))

    def rung_ref(self, row_id: Any) -> str:
        return f"rung:{row_id}" if self.real else self._hash("rng", str(row_id))

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
        first_fill_at: datetime | None = None,
        **cells: Any,
    ) -> OutputRow:
        tz_name = _local_tz(market)
        output: OutputRow = dict.fromkeys(self.specs[self.table].column_names)
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
        if self.real:
            output.update(self.times(tz_name, first_fill_at=first_fill_at))
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
    """First and last in-effect fill per order, so an order row never claims one alone."""

    first: dict[tuple[Any, ...], datetime]
    latest: dict[tuple[Any, ...], datetime]

    @classmethod
    def build(cls, fills: Iterable[SourceRow]) -> _FillTimes:
        first: dict[tuple[Any, ...], datetime] = {}
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
            if key not in first or filled_at < first[key]:
                first[key] = filled_at
            if key not in latest or filled_at > latest[key]:
                latest[key] = filled_at
        return cls(first, latest)

    def lookup(
        self, alias: str, instrument: str, order_id: Any, when: datetime
    ) -> datetime | None:
        if not order_id:
            return None
        return self.latest.get(_fill_key(alias, instrument, order_id, when))

    def lookup_first(
        self, alias: str, instrument: str, order_id: Any, when: datetime
    ) -> datetime | None:
        if not order_id:
            return None
        return self.first.get(_fill_key(alias, instrument, order_id, when))


def _forbidden_values(sources: SourceRows, *, real: bool = False) -> frozenset[str]:
    fields = REAL_IDENTIFIER_FIELDS if real else RAW_IDENTIFIER_FIELDS
    found: set[str] = set()
    for rows in vars(sources).values():
        for row in rows:
            for name in fields:
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
                first_fill_at=fills.lookup_first(
                    "kis_live", instrument, order_no, created
                ),
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
                first_fill_at=fills.lookup_first(alias, instrument, order_no, created),
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
                first_fill_at=fills.lookup_first(
                    "toss_live", instrument, order_id, created
                ),
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
                first_fill_at=fills.lookup_first(
                    "kis_mock", instrument, order_no, created
                ),
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
    if p.real:
        return f"db_paper:{kind}:{row_id}"
    return p._hash("ord", "db_paper", f"{kind}:{row_id}")


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
            account_ref=p.account_ref("db_paper", row["account_id"]),
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
            account_ref=p.account_ref("db_paper", row["account_id"]),
        )
        cell["status"] = "executed"
        cell["order_ref"] = (
            _paper_order_ref(p, "pending", pending_id)
            if pending_id is not None
            else _paper_order_ref(p, "trade", row["id"])
        )
        output.append(cell)
    return output


#: Same prefix as ``kis_lots._SEED_ORDER_ID_PREFIX``: an opening seed's order id.
SEED_ORDER_ID_PREFIX = "SEED-"


def is_seed_row(row: SourceRow) -> bool:
    """A ``manual_import`` ``SEED-*`` row: a whole position, not a fill."""
    return row["source"] == "manual_import" and str(
        row.get("broker_order_id") or ""
    ).startswith(SEED_ORDER_ID_PREFIX)


def fill_qty_basis(row: SourceRow) -> str:
    """Whether ``filled_qty`` is one execution or the order's running total.

    Decided by the writer of the row, from ledger fields that exist today:

    * ``websocket`` rows carry one execution notice (KIS ``CNTG_QTY``, Upbit
      ``trade_volume``/``volume``).
    * KIS ``reconciler`` rows book the daily order row's order-level total
      (``tot_ccld_qty`` / ``ft_ccld_qty``); a later reconcile of a still-filling
      order reports the new total again.
    * Toss ``reconciler`` rows book the delta since the previous reconcile.
    * Upbit ``reconciler`` rows with ledger ``fill_seq`` 0 book the order's
      ``executed_volume``; any other integer ``fill_seq`` is one trade, and a
      ``fill_seq`` that is not an integer leaves the basis ``unknown``.
    * An opening seed is a whole position; any other ``manual_import`` is unknown.
    """
    source = row["source"]
    if source == "manual_import":
        return "position_seed" if is_seed_row(row) else "unknown"
    if source == "websocket":
        return "incremental"
    if source != "reconciler":
        return "unknown"
    broker = row["broker"]
    if broker == "kis":
        return "cumulative"
    if broker == "toss":
        return "incremental"
    if broker == "upbit":
        seq = _ledger_fill_seq(row.get("fill_seq"))
        if seq is None:
            return "unknown"
        return "cumulative" if seq == 0 else "incremental"
    return "unknown"


_DECIMAL_DIGITS = re.compile(r"[0-9]+")


def _ledger_fill_seq(value: Any) -> int | None:
    """The ledger's ``fill_seq`` as an int, or ``None`` when it is not one.

    The column is an ``Integer``: writers store a 31-bit hash or a broker
    sequence, and Upbit stores 0 for an order aggregate. Anything else (a
    missing value, a hex digest, a bool) cannot tell an aggregate from a trade,
    so the basis becomes ``unknown`` instead of the export failing.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _DECIMAL_DIGITS.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _fill_order(row: SourceRow) -> tuple[Any, ...]:
    return (_aware(row["filled_at"]), _aware(row["created_at"]), int(row["id"]))


def fill_sequences(rows: list[SourceRow]) -> dict[int, OutputRow]:
    """The v2 per-order columns of each fill row, keyed by ledger row id.

    ``fill_seq`` is the 1-based position of the row among this export's rows of
    the same order (the ROB-1379 order identity), by fill time, then write time,
    then id. ``incremental_qty`` is what the row adds: the row itself for an
    incremental row. For a cumulative row it is the rise over the highest total
    the same source reported earlier for the order: 0 for a pure re-report, and
    empty while the total is below that high-water mark. Measuring from the
    high-water mark, not from the previous report, keeps the sum of a source's
    increments equal to the highest total it reported, so a stale or wrong dip
    (5, 4, 5) adds nothing instead of counting the recovered share twice.
    """
    by_order: dict[Any, list[SourceRow]] = {}
    cells: dict[int, OutputRow] = {}
    for row in sorted(rows, key=_fill_order):
        basis = fill_qty_basis(row)
        cell: OutputRow = {
            "fill_seq": None,
            "fill_seq_null_reason": None,
            "fill_qty_basis": basis,
            "incremental_qty": None,
            "is_seed": str(is_seed_row(row)).lower(),
        }
        cells[int(row["id"])] = cell
        if basis == "position_seed":
            cell["fill_seq_null_reason"] = "position_seed"
            continue
        if not supersede.normalize_order_id(row.get("broker_order_id")):
            cell["fill_seq_null_reason"] = "no_order_id"
        else:
            identity = supersede.order_identity(SimpleNamespace(**row))
            by_order.setdefault(identity, []).append(row)
            cell["fill_seq"] = str(len(by_order[identity]))
        qty = Decimal(str(row["filled_qty"]))
        if basis == "incremental":
            cell["incremental_qty"] = _decimal(qty)
    for order_rows in by_order.values():
        previous: dict[str, Decimal] = {}
        for row in order_rows:
            cell = cells[int(row["id"])]
            if cell["fill_qty_basis"] != "cumulative":
                continue
            qty = Decimal(str(row["filled_qty"]))
            prior = previous.get(row["source"], Decimal("0"))
            if qty >= prior:
                cell["incremental_qty"] = _decimal(qty - prior)
            # The high-water mark, on purpose: see the docstring.
            previous[row["source"]] = max(prior, qty)
    return cells


def _unlinked_lot_cell() -> OutputRow:
    """The lot columns of a row that is not part of the walk.

    That covers a superseded duplicate and a sell larger than the lots the export
    can see. Both are reported, never guessed into a lot.
    """
    return {
        "lot_ref": None,
        "lot_link_kind": "unlinked",
        "lot_opened_at_utc": None,
        "lot_cost_basis": None,
        "lot_basis_kind": "unknown",
        "linked_fill_ref": None,
    }


@dataclass
class _OpenLot:
    """An open lot in the FIFO walk that links add-buys and partial sells."""

    lot_ref: str
    fill_ref: str
    qty: Decimal
    cost_total: Decimal
    opened_at: datetime
    basis_kind: str

    @property
    def unit_cost(self) -> Decimal:
        return self.cost_total / self.qty if self.qty else Decimal("0")


def fill_lot_links(
    rows: list[SourceRow], sequences: Mapping[int, OutputRow]
) -> dict[int, OutputRow]:
    """Lot linkage per fill row: opening, add-buy, partial or full sell.

    The caller passes **non-superseded** rows only, so the walk sees exactly the
    fills a reader counts. Two quantity rules make that set add up:

    * A ``websocket`` row is provisional. Whenever an authoritative row
      (``reconciler`` / ``manual_import``) shares its order identity, the
      websocket row is ``superseded_duplicate = true`` and never reaches here —
      the reconciler's order total already contains that execution (ROB-1379).
    * A cumulative writer (KIS reconciler rows, Upbit aggregate ``fill_seq = 0``)
      contributes ``incremental_qty``; the export derives it as the rise over the
      same source's earlier high-water mark, so a re-reported running total is not
      counted twice. Rows whose basis is ``unknown`` (a non-seed
      ``manual_import``) carry no increment and are skipped.

    An opening seed (``manual_import`` with a ``SEED-`` order id) is the whole
    position at the cutover, so it starts a fresh lot with
    ``lot_basis_kind = period_start_seed``. A buy with an open lot on the same
    account, market and symbol adds to it; a sell reduces it first-in-first-out.
    A sell with no open lot, or larger than every open lot it can see, is
    ``unlinked`` rather than guessed.
    """
    cells: dict[int, OutputRow] = {}
    lots: dict[tuple[str, str, str], list[_OpenLot]] = {}
    for row in sorted(rows, key=_fill_order):
        row_id = int(row["id"])
        alias = _FILL_ALIASES.get(
            (row["broker"], row["account_mode"]),
            f"{row['broker']}_{row['account_mode']}",
        )
        instrument = _instrument(row["instrument_type"])
        key = (alias, _market_from_instrument(instrument), str(row["symbol"]))
        fill_ref = f"execution_ledger:{row_id}"
        cell: OutputRow = _unlinked_lot_cell()
        cells[row_id] = cell
        sequence = sequences[row_id]
        filled_at = _aware(row["filled_at"])
        price = Decimal(str(row["filled_price"]))

        if sequence["is_seed"] == "true":
            qty = Decimal(str(row["filled_qty"]))
            lot = _OpenLot(
                lot_ref=f"lot:{key[0]}:{key[2]}:{row_id}",
                fill_ref=fill_ref,
                qty=qty,
                cost_total=qty * price,
                opened_at=filled_at,
                basis_kind="period_start_seed",
            )
            lots[key] = [lot]
            cell.update(
                lot_ref=lot.lot_ref,
                lot_link_kind="opens_lot",
                lot_opened_at_utc=filled_at.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                lot_cost_basis=_decimal(price),
                lot_basis_kind="period_start_seed",
            )
            continue

        incremental = sequence["incremental_qty"]
        if incremental is None:
            continue
        qty = Decimal(str(incremental))
        if qty <= 0:
            continue

        if _side(row["side"]) == "buy":
            book = lots.setdefault(key, [])
            if not book:
                lot = _OpenLot(
                    lot_ref=f"lot:{key[0]}:{key[2]}:{row_id}",
                    fill_ref=fill_ref,
                    qty=qty,
                    cost_total=qty * price,
                    opened_at=filled_at,
                    basis_kind="opened_in_window",
                )
                book.append(lot)
                cell.update(
                    lot_ref=lot.lot_ref,
                    lot_link_kind="opens_lot",
                    lot_opened_at_utc=filled_at.astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    lot_cost_basis=_decimal(price),
                    lot_basis_kind="opened_in_window",
                )
                continue
            lot = book[-1]
            cell.update(
                lot_ref=lot.lot_ref,
                lot_link_kind="adds_to_lot",
                lot_opened_at_utc=lot.opened_at.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                lot_cost_basis=_decimal_scaled(lot.unit_cost),
                lot_basis_kind=lot.basis_kind,
                linked_fill_ref=lot.fill_ref,
            )
            lot.qty += qty
            lot.cost_total += qty * price
            continue

        book = lots.get(key) or []
        if not book:
            continue
        consumed = book[0]
        consumed_unit_cost = consumed.unit_cost
        remaining = qty
        while remaining > 0 and book:
            lot = book[0]
            take = min(remaining, lot.qty)
            unit = lot.unit_cost
            lot.qty -= take
            lot.cost_total -= take * unit
            remaining -= take
            if lot.qty <= 0:
                book.pop(0)
        if remaining > 0:
            # The order sold more than the lots this export can see, so the row
            # cannot be attributed to one lot. It stays unlinked and the overhang
            # is countable instead of being folded into a lot it does not fit.
            continue
        cell.update(
            lot_ref=consumed.lot_ref,
            lot_link_kind="partial_sell" if book else "full_sell",
            lot_opened_at_utc=consumed.opened_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            lot_cost_basis=_decimal_scaled(consumed_unit_cost),
            lot_basis_kind=consumed.basis_kind,
            linked_fill_ref=consumed.fill_ref,
        )
    return cells


def _project_fills(
    p: _Projector, rows: list[SourceRow], window: Window
) -> list[OutputRow]:
    in_window = [row for row in rows if _aware(row["filled_at"]) in window]
    # Coverage counts only exported rows. A reconciler row outside the window must
    # not flag the one exported row of a fill, or filtering would lose the fill.
    covered = supersede.covered_identities(SimpleNamespace(**row) for row in in_window)
    sequences = fill_sequences(in_window)
    # The lot walk must see exactly the rows a consumer counts: the non-superseded
    # ones. Walking the superseded rows first moved quantities and cost bases that
    # the export itself then tells the reader to exclude (ROB-1465 review).
    links: dict[int, OutputRow] = {}
    if p.real:
        effective = [
            row
            for row in in_window
            if not supersede.is_superseded(SimpleNamespace(**row), covered)
        ]
        links = fill_lot_links(effective, sequences)
        for row in in_window:
            links.setdefault(int(row["id"]), _unlinked_lot_cell())
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
            **sequences[int(row["id"])],
            "order_ref": p.order_ref(alias, row["broker_order_id"]),
            "correlation_ref": p.key_ref("cor", row.get("correlation_id")),
            "trading_day": _trading_day_cell(str(row["broker"]), instrument, filled_at),
            "local_tz": tz_name,
        }
        if links:
            cell.update(links[int(row["id"])])
        cell.update(p.times(tz_name, filled_at=filled_at, created_at=row["created_at"]))
        output.append(cell)
    return output


def _reserved_at(
    reservations: Iterable[SourceRow], account_id: Any, when: datetime
) -> Decimal | None:
    """KRW held by the account's open paper buys at ``when``, or ``None``.

    A resolved order with no fill or cancel time cannot be placed in time, so the
    total is unknown rather than guessed.
    """
    total = Decimal("0")
    for order in reservations:
        if order["account_id"] != account_id or _aware(order["placed_at"]) > when:
            continue
        ended = [
            _aware(moment)
            for moment in (order.get("filled_at"), order.get("cancelled_at"))
            if moment is not None
        ]
        if not ended and order["status"] != "pending":
            return None
        if ended and min(ended) <= when:
            continue
        total += Decimal(str(order["reserved_krw"]))
    return total


def _project_balances(
    p: _Projector, snapshots: Iterable[SourceRow], reservations: list[SourceRow]
) -> list[OutputRow]:
    tz_name = _local_tz("kr")
    output: list[OutputRow] = []
    for row in snapshots:
        created = _aware(row["created_at"])
        account_ref = p.account_ref("db_paper", row["account_id"])
        for currency, column, reserved in (
            ("KRW", "cash_krw", _reserved_at(reservations, row["account_id"], created)),
            # Paper buys reserve KRW only; no table records a USD reservation.
            ("USD", "cash_usd", None),
        ):
            cell: OutputRow = dict.fromkeys(p.specs[p.table].column_names)
            cell.update(
                row_ref=p.row_ref(row["id"], currency),
                account_alias="db_paper",
                account_ref=account_ref,
                currency=currency,
                balance_date=row["snapshot_date"].isoformat(),
                available="true",
                source_table=BALANCE_SOURCES["db_paper"],
                cash=_decimal(row[column]),
                cash_basis="free_after_open_order_reservations",
                reserved_open_orders=_decimal(reserved),
                local_tz=tz_name,
            )
            cell.update(p.times(tz_name, snapshot_created_at=created))
            output.append(cell)
    for alias in BALANCE_UNAVAILABLE:
        cell = dict.fromkeys(p.specs[p.table].column_names)
        cell.update(
            row_ref=p.row_ref("unavailable", alias),
            account_alias=alias,
            available="false",
            unavailable_reason=BALANCE_UNAVAILABLE_REASON,
            local_tz=DEFAULT_LOCAL_TIMEZONE,
        )
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


class _ReferencePrices:
    """The last completed UTC daily close per crypto symbol before an order.

    ``crypto_candles_1d.time`` is the UTC day a daily candle covers (Upbit's
    ``candle_date_time_utc``), so that candle's close is at ``time + 1 day`` and
    it is only usable as a reference once that instant has passed.
    """

    def __init__(self, candles: Iterable[SourceRow]) -> None:
        series: dict[str, list[tuple[datetime, Decimal]]] = {}
        for row in candles:
            symbol = _text(row.get("venue_symbol"))
            if not symbol:
                continue
            series.setdefault(symbol, []).append(
                (_aware(row["time"]), Decimal(str(row["close"])))
            )
        for values in series.values():
            values.sort()
        self._series = series

    def lookup(
        self, symbol: str, created_at: datetime
    ) -> tuple[Decimal | None, datetime | None, str]:
        values = self._series.get(symbol)
        if not values:
            return None, None, "unavailable:no_instrument"
        cutoff = _aware(created_at) - timedelta(days=1)
        found: tuple[datetime, Decimal] | None = None
        for when, close in values:
            if when <= cutoff:
                found = (when, close)
            else:
                break
        if found is None:
            return None, None, "unavailable:no_candle"
        return found[1], found[0] + timedelta(days=1), "crypto_candles_1d_close"


def _project_rungs(
    p: _Projector,
    rows: Iterable[SourceRow],
    fills: _FillTimes | None = None,
    prices: _ReferencePrices | None = None,
) -> list[OutputRow]:
    output = []
    for row in rows:
        market = _market_from_instrument(row["market"])
        alias = _PROPOSAL_ALIASES.get(
            str(row["account_mode"]), str(row["account_mode"])
        )
        tz_name = _local_tz(market)
        cell: OutputRow = {
            "row_ref": p.row_ref(row["id"]),
            "rung_ref": p.rung_ref(row["id"]),
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
        if p.real:
            first_fill = None
            if fills is not None:
                first_fill = fills.lookup_first(
                    alias,
                    _MARKET_INSTRUMENTS.get(market, ""),
                    row.get("broker_order_id"),
                    row["created_at"],
                )
            cell.update(p.times(tz_name, first_fill_at=first_fill))
            cell.update(
                reference_price=None,
                reference_price_asof_utc=None,
                reference_price_source=None,
                distance_above_reference_pct=None,
            )
            if market == "crypto" and _side(row["side"]) == "sell" and prices:
                _apply_reference_price(cell, row, prices)
        output.append(cell)
    return output


def _apply_reference_price(
    cell: OutputRow, row: SourceRow, prices: _ReferencePrices
) -> None:
    price, asof, source = prices.lookup(str(row["symbol"]), row["created_at"])
    cell["reference_price"] = _decimal_scaled(price)
    cell["reference_price_asof_utc"] = (
        asof.astimezone(UTC).isoformat().replace("+00:00", "Z") if asof else None
    )
    cell["reference_price_source"] = source
    limit = row.get("limit_price")
    if price is not None and limit is not None:
        cell["distance_above_reference_pct"] = _pct(
            Decimal(str(limit)), Decimal(str(price))
        )


def project(
    sources: SourceRows,
    *,
    hasher: RefHasher | None,
    window: Window,
    mode: Mode = Mode.DEIDENTIFIED,
) -> Projection:
    real = mode is Mode.REAL
    if not real and hasher is None:
        raise ValueError("the de-identified mode needs a salt")
    projector = _Projector(hasher, window, real=real)
    fills = _FillTimes.build(sources.execution_ledger)
    prices = _ReferencePrices(sources.crypto_daily_candles) if real else None
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
            projector,
            sources.order_proposal_rungs,
            fills if real else None,
            prices,
        ),
        "account_balance_daily": lambda: _project_balances(
            projector, sources.paper_daily_snapshots, sources.paper_reservations
        ),
    }
    tables: dict[str, list[OutputRow]] = {}
    for name, build in builders.items():
        projector.table = name
        tables[name] = build()
    return Projection(
        tables=tables,
        forbidden=_forbidden_values(sources, real=real),
        tags_hashed=projector.tags_hashed,
        mode=mode,
    )
