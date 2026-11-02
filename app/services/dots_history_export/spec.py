"""The closed output schema: every exported table, column and value shape."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

SCHEMA_VERSION = "dots-history-export/v2"
SCHEMA_VERSION_REAL = "dots-history-export/real-v1"


class Kind(StrEnum):
    REF = "ref"
    DECIMAL = "decimal"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    TS_UTC = "ts_utc"
    TS_LOCAL = "ts_local"
    DATE = "date"
    ENUM = "enum"
    SYMBOL = "symbol"
    TAG = "tag"
    #: Real mode: a broker order id, correlation id or proposal id copied as is.
    RAW_REF = "raw_ref"
    #: Real mode: a label copied with its spaces and non-ASCII text intact.
    PLAIN = "plain"
    #: Real mode: an account identifier reduced to its last digits.
    MASKED = "masked"


class Mode(StrEnum):
    """``deidentified`` is the ROB-1438 default; ``real`` is ROB-1465."""

    DEIDENTIFIED = "deidentified"
    REAL = "real"


@dataclass(frozen=True)
class Column:
    name: str
    kind: Kind
    ref_prefix: str | None = None
    values: frozenset[str] | None = None


@dataclass(frozen=True)
class TableSpec:
    name: str
    window_column: str
    columns: tuple[Column, ...]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


ACCOUNT_ALIASES = frozenset(
    {
        "kis_live",
        "kis_mock",
        "toss_live",
        "upbit_live",
        "alpaca_paper",
        "alpaca_paper_lab",
        "alpaca_paper_crypto",
        "db_paper",
    }
)
MARKETS = frozenset({"kr", "us", "crypto", "forex", "index", "unknown"})
SIDES = frozenset({"buy", "sell"})
TERMINAL_OUTCOMES = frozenset(
    {
        "filled",
        "partial",
        "expired",
        "cancelled",
        "rejected",
        "replaced",
        "open",
        "not_submitted",
        "anomaly",
        "unknown",
    }
)
LOCAL_TIMEZONES = {
    "kr": "Asia/Seoul",
    "us": "America/New_York",
    "crypto": "Asia/Seoul",
}
DEFAULT_LOCAL_TIMEZONE = "UTC"

#: The order lifecycle sources the brief names that keep no database ledger.
SOURCES_WITHOUT_LEDGER = {
    "kiwoom_mock": (
        "no order ledger table exists; Kiwoom mock orders are readable only "
        "from the broker, which this tool never calls"
    ),
}


#: How a fill row's ``filled_qty`` relates to its order (v2). The writer decides:
#: KIS reconciler rows book the daily order row's order-level total, Upbit
#: reconciler rows with ledger ``fill_seq`` 0 book the order's
#: ``executed_volume``, and an opening seed books a whole position.
FILL_QTY_BASES = frozenset({"incremental", "cumulative", "position_seed", "unknown"})
FILL_SEQ_NULL_REASONS = frozenset({"position_seed", "no_order_id"})
CURRENCIES = frozenset({"KRW", "USD"})

#: Where each account's daily cash comes from (v2). Only the DB paper simulator
#: records a per-day balance; every broker account's cash is readable only from
#: the broker at request time, which this tool never calls.
BALANCE_SOURCES = {"db_paper": "paper.paper_daily_snapshots"}
BALANCE_UNAVAILABLE_REASON = "no_balance_history_table"
BALANCE_UNAVAILABLE = dict.fromkeys(
    sorted(ACCOUNT_ALIASES - BALANCE_SOURCES.keys()),
    "no table stores this account's cash per day; its cash is read from the "
    "broker at request time only",
)


def _ref(name: str, prefix: str) -> Column:
    return Column(name, Kind.REF, ref_prefix=prefix)


def _enum(name: str, values: frozenset[str]) -> Column:
    return Column(name, Kind.ENUM, values=values)


def _tag(name: str) -> Column:
    return Column(name, Kind.TAG)


def _decimal(name: str) -> Column:
    return Column(name, Kind.DECIMAL)


def _times(*names: str) -> tuple[Column, ...]:
    columns: list[Column] = []
    for name in names:
        columns.append(Column(f"{name}_utc", Kind.TS_UTC))
        columns.append(Column(f"{name}_local", Kind.TS_LOCAL))
    return tuple(columns)


_LOCAL_TZ = _enum(
    "local_tz",
    frozenset(LOCAL_TIMEZONES.values()) | {DEFAULT_LOCAL_TIMEZONE},
)

ORDER_COLUMNS: tuple[Column, ...] = (
    _ref("row_ref", "row"),
    _enum("account_alias", ACCOUNT_ALIASES),
    _ref("account_ref", "acc"),
    _enum("market", MARKETS),
    _tag("venue"),
    Column("symbol", Kind.SYMBOL),
    _enum("side", SIDES),
    _tag("operation_kind"),
    _tag("record_kind"),
    _tag("order_type"),
    _tag("time_in_force"),
    _decimal("qty"),
    _decimal("limit_price"),
    _decimal("requested_notional"),
    _tag("currency"),
    _decimal("filled_qty"),
    _decimal("fill_price"),
    _tag("status"),
    _tag("lifecycle_state"),
    _enum("terminal_outcome", TERMINAL_OUTCOMES),
    _tag("strategy_tag"),
    _ref("order_ref", "ord"),
    _ref("original_order_ref", "ord"),
    _ref("replaced_by_order_ref", "ord"),
    _ref("correlation_ref", "cor"),
    _ref("report_item_ref", "rpt"),
    Column("trading_day", Kind.DATE),
    _LOCAL_TZ,
    *_times("created_at", "accepted_at", "filled_at", "expired_at", "cancelled_at"),
)

FILL_COLUMNS: tuple[Column, ...] = (
    _ref("row_ref", "row"),
    _enum("account_alias", ACCOUNT_ALIASES),
    _enum("market", MARKETS),
    _tag("venue"),
    Column("symbol", Kind.SYMBOL),
    _enum("side", SIDES),
    _decimal("filled_qty"),
    _decimal("fill_price"),
    _tag("currency"),
    _enum("fill_source", frozenset({"reconciler", "websocket", "manual_import"})),
    Column("superseded_duplicate", Kind.BOOLEAN),
    Column("fill_seq", Kind.INTEGER),
    _enum("fill_seq_null_reason", FILL_SEQ_NULL_REASONS),
    _enum("fill_qty_basis", FILL_QTY_BASES),
    _decimal("incremental_qty"),
    Column("is_seed", Kind.BOOLEAN),
    _ref("order_ref", "ord"),
    _ref("correlation_ref", "cor"),
    Column("trading_day", Kind.DATE),
    _LOCAL_TZ,
    *_times("filled_at", "created_at"),
)

PROPOSAL_COLUMNS: tuple[Column, ...] = (
    _ref("row_ref", "row"),
    _ref("proposal_ref", "prp"),
    _ref("root_proposal_ref", "prp"),
    Column("revision", Kind.INTEGER),
    _enum("account_alias", ACCOUNT_ALIASES),
    _enum("market", MARKETS),
    Column("symbol", Kind.SYMBOL),
    _enum("side", SIDES),
    _tag("order_type"),
    _tag("action"),
    _tag("lifecycle_state"),
    _tag("strategy_tag"),
    _tag("exit_intent"),
    _tag("void_reason"),
    _ref("target_order_ref", "ord"),
    _ref("correlation_ref", "cor"),
    _LOCAL_TZ,
    *_times("created_at", "validated_at", "approved_at", "valid_until"),
)

RUNG_COLUMNS: tuple[Column, ...] = (
    _ref("row_ref", "row"),
    _ref("rung_ref", "rng"),
    _ref("proposal_ref", "prp"),
    Column("rung_index", Kind.INTEGER),
    _enum("account_alias", ACCOUNT_ALIASES),
    _enum("market", MARKETS),
    Column("symbol", Kind.SYMBOL),
    _enum("side", SIDES),
    _decimal("qty"),
    _decimal("limit_price"),
    _decimal("requested_notional"),
    _tag("state"),
    _decimal("filled_qty"),
    _tag("void_reason"),
    _tag("void_reason_group"),
    _ref("order_ref", "ord"),
    _ref("correlation_ref", "cor"),
    _LOCAL_TZ,
    *_times("created_at", "validated_at", "updated_at"),
)

BALANCE_COLUMNS: tuple[Column, ...] = (
    _ref("row_ref", "row"),
    _enum("account_alias", ACCOUNT_ALIASES),
    _ref("account_ref", "acc"),
    _enum("currency", CURRENCIES),
    Column("balance_date", Kind.DATE),
    Column("available", Kind.BOOLEAN),
    _enum("unavailable_reason", frozenset({BALANCE_UNAVAILABLE_REASON})),
    _enum("source_table", frozenset(BALANCE_SOURCES.values())),
    _decimal("cash"),
    _enum("cash_basis", frozenset({"free_after_open_order_reservations"})),
    _decimal("settled_cash"),
    _decimal("reserved_open_orders"),
    _LOCAL_TZ,
    *_times("snapshot_created_at"),
)

TABLES: tuple[TableSpec, ...] = (
    TableSpec("kis_live_order_ledger", "created_at", ORDER_COLUMNS),
    TableSpec("live_order_ledger", "created_at", ORDER_COLUMNS),
    TableSpec("toss_live_order_ledger", "created_at", ORDER_COLUMNS),
    TableSpec("kis_mock_order_ledger", "created_at", ORDER_COLUMNS),
    TableSpec("alpaca_paper_order_ledger", "created_at", ORDER_COLUMNS),
    TableSpec("paper_pending_orders", "placed_at", ORDER_COLUMNS),
    TableSpec("paper_trades", "executed_at", ORDER_COLUMNS),
    TableSpec("execution_ledger", "filled_at", FILL_COLUMNS),
    TableSpec("order_proposals", "created_at", PROPOSAL_COLUMNS),
    TableSpec("order_proposal_rungs", "proposal.created_at", RUNG_COLUMNS),
    TableSpec("account_balance_daily", "created_at", BALANCE_COLUMNS),
)
TABLE_BY_NAME = {table.name: table for table in TABLES}


# --- Real mode (ROB-1465) -------------------------------------------------
#
# The operator's 2026-11-02 decision drops de-identification except for
# credentials, API keys and full account numbers. Real mode copies the same
# rows with real identifiers and plain labels, adds a first-fill time, and
# adds the columns the ROB-1459 review asked for: lot links and the crypto
# sell-ladder reference price.

LOT_LINK_KINDS = frozenset(
    {"opens_lot", "adds_to_lot", "partial_sell", "full_sell", "unlinked"}
)
LOT_BASIS_KINDS = frozenset({"period_start_seed", "opened_in_window", "unknown"})
REFERENCE_PRICE_SOURCES = frozenset(
    {"crypto_candles_1d_close", "unavailable:no_instrument", "unavailable:no_candle"}
)


def _real(columns: tuple[Column, ...]) -> tuple[Column, ...]:
    """Swap hashed refs for raw ones, hashed labels for plain text, mask accounts."""
    output: list[Column] = []
    for column in columns:
        if column.name == "account_ref":
            output.append(Column(column.name, Kind.MASKED))
        elif column.kind is Kind.REF:
            output.append(Column(column.name, Kind.RAW_REF))
        elif column.kind is Kind.TAG:
            output.append(Column(column.name, Kind.PLAIN))
        else:
            output.append(column)
    return tuple(output)


_LOT_COLUMNS: tuple[Column, ...] = (
    Column("lot_ref", Kind.RAW_REF),
    _enum("lot_link_kind", LOT_LINK_KINDS),
    Column("lot_opened_at_utc", Kind.TS_UTC),
    _decimal("lot_cost_basis"),
    _enum("lot_basis_kind", LOT_BASIS_KINDS),
    Column("linked_fill_ref", Kind.RAW_REF),
)

_RUNG_REFERENCE_COLUMNS: tuple[Column, ...] = (
    _decimal("reference_price"),
    Column("reference_price_asof_utc", Kind.TS_UTC),
    _enum("reference_price_source", REFERENCE_PRICE_SOURCES),
    _decimal("distance_above_reference_pct"),
)

ORDER_COLUMNS_REAL = _real(ORDER_COLUMNS) + _times("first_fill_at")
FILL_COLUMNS_REAL = _real(FILL_COLUMNS) + _LOT_COLUMNS
PROPOSAL_COLUMNS_REAL = _real(PROPOSAL_COLUMNS)
RUNG_COLUMNS_REAL = (
    _real(RUNG_COLUMNS) + _RUNG_REFERENCE_COLUMNS + _times("first_fill_at")
)
BALANCE_COLUMNS_REAL = _real(BALANCE_COLUMNS)

TABLES_REAL: tuple[TableSpec, ...] = (
    TableSpec("kis_live_order_ledger", "created_at", ORDER_COLUMNS_REAL),
    TableSpec("live_order_ledger", "created_at", ORDER_COLUMNS_REAL),
    TableSpec("toss_live_order_ledger", "created_at", ORDER_COLUMNS_REAL),
    TableSpec("kis_mock_order_ledger", "created_at", ORDER_COLUMNS_REAL),
    TableSpec("alpaca_paper_order_ledger", "created_at", ORDER_COLUMNS_REAL),
    TableSpec("paper_pending_orders", "placed_at", ORDER_COLUMNS_REAL),
    TableSpec("paper_trades", "executed_at", ORDER_COLUMNS_REAL),
    TableSpec("execution_ledger", "filled_at", FILL_COLUMNS_REAL),
    TableSpec("order_proposals", "created_at", PROPOSAL_COLUMNS_REAL),
    TableSpec("order_proposal_rungs", "proposal.created_at", RUNG_COLUMNS_REAL),
    TableSpec("account_balance_daily", "created_at", BALANCE_COLUMNS_REAL),
)
TABLE_BY_NAME_REAL = {table.name: table for table in TABLES_REAL}


def tables_for(mode: Mode) -> tuple[TableSpec, ...]:
    return TABLES_REAL if mode is Mode.REAL else TABLES


def schema_version_for(mode: Mode) -> str:
    return SCHEMA_VERSION_REAL if mode is Mode.REAL else SCHEMA_VERSION
