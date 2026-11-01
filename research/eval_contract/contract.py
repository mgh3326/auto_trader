"""Evaluation contract v1 types (ROB-1433).

One shared shape for every market adapter:

    Decision -> Intent -> OrderSpec -> FillRow -> Outcome
                                         \\-> FillModelResult -> EvaluationReport

Conventions (each pinned by a test):

* Money and quantities are ``Decimal``; floats never enter a contract row.
* Timestamps are timezone-aware. A decision's ``data_cutoff`` is never after its
  ``decided_at`` (point-in-time: only data available at the decision is used).
* ``FillRow`` uses the column names of ``execution_ledger`` so a simulated fill
  and a broker fill can be compared field by field. Simulated rows carry
  ``account_mode="backtest"`` and a ``simulated:<model>`` source so they can
  never be mistaken for broker evidence.
* ``Outcome`` is a markout ``R_h`` for ``h`` in ``HORIZONS``. A horizon past the
  end of the data is *censored* (all values ``None``), never 0.
* The primary metric is net return versus cash at equal capital. Fill rate is a
  diagnostic only.
* Every report references a registered hypothesis: a ``RegistryRef`` with the
  registry hash and the cumulative test count ``k``. A report without one is
  rejected at construction and at load.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any

CONTRACT_VERSION = "eval-contract/v1"
HORIZONS: tuple[int, ...] = (1, 5, 20)
BRACKET_OPTIMISTIC = "optimistic_touch"
BRACKET_CONSERVATIVE = "conservative_through"
SIMULATED_ACCOUNT_MODE = "backtest"
SIMULATED_BROKER = "simulator"

ORDER_ORIGINS = ("strategy", "engine_time_trim")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """A value violates the evaluation contract."""


class MissingRegistryReference(ContractError):
    """A report does not reference a registered hypothesis (hash + k)."""


class Market(StrEnum):
    KR = "kr"
    US = "us"
    CRYPTO = "crypto"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class DecisionAction(StrEnum):
    ENTER = "enter"
    EXIT = "exit"
    SKIP = "skip"


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ContractError(f"{name} must be a timezone-aware datetime")


def _non_empty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a non-empty string")


def _decimal(value: Decimal, name: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ContractError(f"{name} must be a finite Decimal")
    if positive and value <= 0:
        raise ContractError(f"{name} must be positive")
    if not positive and value < 0:
        raise ContractError(f"{name} must be non-negative")


def _sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ContractError(f"{name} must be a lowercase hex sha256")


# ------------------------------------------------------------------ canonical


def _canonical(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _canonical(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ContractError("non-finite Decimal cannot be hashed")
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        _aware(value, "hashed datetime")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        raise ContractError("floats are not canonical; use Decimal")
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value):
            raise ContractError("canonical mapping keys must be strings")
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ContractError(f"cannot hash {type(value).__name__}")


def sha256_canonical(value: Any) -> str:
    """sha256 of the canonical JSON of ``value`` (sorted keys, Decimal as text)."""

    payload = json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


# --------------------------------------------------------------- the pipeline


@dataclass(frozen=True, slots=True)
class Decision:
    decision_id: str
    hypothesis_id: str
    market: Market
    symbol: str
    action: DecisionAction
    decided_at: datetime
    data_cutoff: datetime  # latest data the decision may have used

    def __post_init__(self) -> None:
        _non_empty(self.decision_id, "decision_id")
        _non_empty(self.hypothesis_id, "hypothesis_id")
        _non_empty(self.symbol, "symbol")
        _aware(self.decided_at, "decided_at")
        _aware(self.data_cutoff, "data_cutoff")
        if self.data_cutoff > self.decided_at:
            raise ContractError("data_cutoff is after decided_at (look-ahead)")


@dataclass(frozen=True, slots=True)
class Intent:
    """What the strategy wants: side, a notional cap and a deadline."""

    intent_id: str
    decision_id: str
    side: Side
    max_notional: Decimal
    valid_until: datetime

    def __post_init__(self) -> None:
        _non_empty(self.intent_id, "intent_id")
        _non_empty(self.decision_id, "decision_id")
        _decimal(self.max_notional, "max_notional", positive=True)
        _aware(self.valid_until, "valid_until")


@dataclass(frozen=True, slots=True)
class OrderSpec:
    """One child order inside an intent. v1 knows DAY limit orders only."""

    order_id: str
    intent_id: str
    market: Market
    symbol: str
    side: Side
    limit_price: Decimal
    quantity: Decimal
    submitted_at: datetime
    expires_at: datetime
    order_type: str = "limit"
    time_in_force: str = "day"
    # "engine_time_trim": a harness-forced exit at the open (no strategy order).
    origin: str = "strategy"

    def __post_init__(self) -> None:
        _non_empty(self.order_id, "order_id")
        _non_empty(self.intent_id, "intent_id")
        _non_empty(self.symbol, "symbol")
        _decimal(self.limit_price, "limit_price", positive=True)
        _decimal(self.quantity, "quantity", positive=True)
        _aware(self.submitted_at, "submitted_at")
        _aware(self.expires_at, "expires_at")
        if self.expires_at <= self.submitted_at:
            raise ContractError("expires_at must be after submitted_at")
        if self.order_type != "limit" or self.time_in_force != "day":
            raise ContractError("contract v1 supports DAY limit orders only")
        if self.origin not in ORDER_ORIGINS:
            raise ContractError(f"origin must be one of {ORDER_ORIGINS}")

    @property
    def notional(self) -> Decimal:
        return self.limit_price * self.quantity


@dataclass(frozen=True, slots=True)
class FillRow:
    """A simulated fill with the column names of ``execution_ledger``."""

    venue: str
    instrument_type: str
    symbol: str
    raw_symbol: str
    side: Side
    broker_order_id: str
    filled_qty: Decimal
    filled_price: Decimal
    filled_notional: Decimal
    fee_amount: Decimal
    fee_currency: str
    filled_at: datetime
    currency: str
    correlation_id: str
    source: str
    fill_seq: int = 0
    broker: str = SIMULATED_BROKER
    account_mode: str = SIMULATED_ACCOUNT_MODE

    def __post_init__(self) -> None:
        for name in (
            "venue",
            "instrument_type",
            "symbol",
            "raw_symbol",
            "broker_order_id",
            "fee_currency",
            "currency",
            "correlation_id",
        ):
            _non_empty(getattr(self, name), name)
        _decimal(self.filled_qty, "filled_qty", positive=True)
        _decimal(self.filled_price, "filled_price", positive=True)
        _decimal(self.filled_notional, "filled_notional", positive=True)
        _decimal(self.fee_amount, "fee_amount")
        _aware(self.filled_at, "filled_at")
        if self.filled_notional != self.filled_qty * self.filled_price:
            raise ContractError("filled_notional != filled_qty * filled_price")
        if self.broker != SIMULATED_BROKER or self.account_mode != (
            SIMULATED_ACCOUNT_MODE
        ):
            raise ContractError("contract fill rows are simulated rows only")
        if not self.source.startswith("simulated:"):
            raise ContractError("source must be 'simulated:<fill model>'")
        if self.fill_seq < 0:
            raise ContractError("fill_seq must be non-negative")

    def as_ledger_row(self) -> dict[str, Any]:
        row = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
        row["side"] = self.side.value
        return row


@dataclass(frozen=True, slots=True)
class CostModel:
    """Side-specific proportional costs; ``sell_tax_rate`` applies on exit."""

    currency: str
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal
    sell_tax_rate: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        _non_empty(self.currency, "currency")
        _decimal(self.buy_fee_rate, "buy_fee_rate")
        _decimal(self.sell_fee_rate, "sell_fee_rate")
        _decimal(self.sell_tax_rate, "sell_tax_rate")

    def buy_cash(self, gross: Decimal) -> Decimal:
        return gross * (Decimal(1) + self.buy_fee_rate)

    def sell_proceeds(self, gross: Decimal) -> Decimal:
        return gross * (Decimal(1) - self.sell_fee_rate - self.sell_tax_rate)


@dataclass(frozen=True, slots=True)
class Outcome:
    """Markout of one buy fill ``h`` sessions after the fill session (day 0).

    ``gross_return`` = close_h / entry - 1. ``net_return`` pays the buy fee at
    entry and the sell fee + tax at close_h. ``cash_return`` is what the same
    capital earns in cash over ``h`` sessions; ``net_vs_cash`` = net - cash.
    """

    order_id: str
    horizon: int
    censored: bool
    gross_return: Decimal | None = None
    net_return: Decimal | None = None
    cash_return: Decimal | None = None
    net_vs_cash: Decimal | None = None

    def __post_init__(self) -> None:
        _non_empty(self.order_id, "order_id")
        if self.horizon not in HORIZONS:
            raise ContractError(f"horizon must be one of {HORIZONS}")
        values = (
            self.gross_return,
            self.net_return,
            self.cash_return,
            self.net_vs_cash,
        )
        if self.censored and any(v is not None for v in values):
            raise ContractError("a censored outcome carries no values")
        if not self.censored and any(v is None for v in values):
            raise ContractError("an observed outcome carries every value")


@dataclass(frozen=True, slots=True)
class RegistryRef:
    """Pointer to a registered hypothesis.

    TODO(U1): U1's hypothesis-registry schema is not merged yet. Until it is,
    this stub only checks shape (id, sha256, k >= 1); after U1 lands, resolve
    ``registry_hash`` against the registry file and take ``k`` from it.
    """

    hypothesis_id: str
    registry_hash: str
    k: int

    def __post_init__(self) -> None:
        _non_empty(self.hypothesis_id, "hypothesis_id")
        _sha256(self.registry_hash, "registry_hash")
        if isinstance(self.k, bool) or not isinstance(self.k, int) or self.k < 1:
            raise ContractError("k must be an int >= 1 (every test-set look counts)")


@dataclass(frozen=True, slots=True)
class Provenance:
    code_ref: str
    data_manifest_sha256: str
    config_sha256: str
    registry: RegistryRef | None
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.registry, RegistryRef):
            raise MissingRegistryReference(
                "report has no registry reference (hypothesis registry hash + k)"
            )
        _non_empty(self.code_ref, "code_ref")
        _sha256(self.data_manifest_sha256, "data_manifest_sha256")
        _sha256(self.config_sha256, "config_sha256")
        if self.contract_version != CONTRACT_VERSION:
            raise ContractError(f"unsupported contract {self.contract_version!r}")


@dataclass(frozen=True, slots=True)
class FillModelResult:
    """Everything one fill model produced on its own decision path.

    Fills change positions and cash, which change later signals, so each model
    carries its own decisions, intents and orders.

    ``status="unavailable"`` (with ``unavailable_reason``) means the harness or
    the model cannot run (missing input, unfitted model). It is reported, not
    dropped, and it carries no numbers.
    """

    fill_model_id: str
    params_sha256: str
    status: str
    decisions: tuple[Decision, ...] = ()
    intents: tuple[Intent, ...] = ()
    orders: tuple[OrderSpec, ...] = ()
    fills: tuple[FillRow, ...] = ()
    outcomes: tuple[Outcome, ...] = ()
    capital: Decimal | None = None
    terminal_value: Decimal | None = None
    cash_baseline: Decimal | None = None
    unavailable_reason: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _non_empty(self.fill_model_id, "fill_model_id")
        _sha256(self.params_sha256, "params_sha256")
        if self.status == "unavailable":
            _non_empty(self.unavailable_reason or "", "unavailable_reason")
            if (
                self.decisions
                or self.intents
                or self.orders
                or self.fills
                or self.outcomes
                or self.capital is not None
                or self.terminal_value is not None
                or self.cash_baseline is not None
            ):
                raise ContractError("an unavailable result carries no numbers")
            return
        if self.status != "ok":
            raise ContractError("status must be 'ok' or 'unavailable'")
        for name in ("capital", "terminal_value", "cash_baseline"):
            value = getattr(self, name)
            if value is None:
                raise ContractError(f"{name} is required for an ok result")
            _decimal(value, name)
        if self.capital is not None and self.capital <= 0:
            raise ContractError("capital must be positive")
        self._check_chain()

    def _check_chain(self) -> None:
        """Ids resolve and time runs forward: decision -> intent -> order -> fill."""

        decisions = {d.decision_id: d for d in self.decisions}
        intents = {i.intent_id: i for i in self.intents}
        orders = {o.order_id: o for o in self.orders}
        if len(decisions) != len(self.decisions) or len(intents) != len(self.intents):
            raise ContractError("duplicate decision or intent ids")
        if len(orders) != len(self.orders):
            raise ContractError("duplicate order ids")
        for i in self.intents:
            d = decisions.get(i.decision_id)
            if d is None:
                raise ContractError("an intent references an unknown decision")
            if i.valid_until <= d.decided_at:
                raise ContractError("an intent expires before its decision")
        for o in self.orders:
            i = intents.get(o.intent_id)
            if i is None:
                raise ContractError("an order references an unknown intent")
            if o.side is not i.side:
                raise ContractError("an order's side differs from its intent")
            if o.submitted_at < decisions[i.decision_id].decided_at:
                raise ContractError("an order is submitted before its decision")
            if o.expires_at > i.valid_until:
                raise ContractError("an order outlives its intent")
        for f in self.fills:
            o = orders.get(f.broker_order_id)
            if o is None:
                raise ContractError("a fill references an unknown order")
            if not o.submitted_at <= f.filled_at <= o.expires_at:
                raise ContractError("a fill lies outside its order's life")
            if f.side is not o.side or f.symbol != o.symbol:
                raise ContractError("a fill differs from its order (side/symbol)")
            if f.filled_qty > o.quantity:
                raise ContractError("a fill exceeds its order quantity")
        filled = {f.broker_order_id for f in self.fills}
        if any(out.order_id not in filled for out in self.outcomes):
            raise ContractError("an outcome references an unfilled order")

    @property
    def net_vs_cash(self) -> Decimal | None:
        """Primary metric: (terminal - cash baseline) / capital at equal capital."""

        if self.status != "ok":
            return None
        assert self.terminal_value is not None and self.cash_baseline is not None
        assert self.capital is not None
        return (self.terminal_value - self.cash_baseline) / self.capital

    @property
    def filled_order_ids(self) -> frozenset[str]:
        return frozenset(f.broker_order_id for f in self.fills)

    @property
    def fill_rate(self) -> Decimal | None:
        """Diagnostic only: share of orders with any fill."""

        if self.status != "ok" or not self.orders:
            return None
        return Decimal(len(self.filled_order_ids)) / Decimal(len(self.orders))


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    hypothesis_id: str
    market: Market
    provenance: Provenance
    results: tuple[FillModelResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, Provenance):
            raise MissingRegistryReference("report has no provenance")
        registry = self.provenance.registry
        if registry is None:  # pragma: no cover - Provenance already refuses
            raise MissingRegistryReference("report has no registry reference")
        if registry.hypothesis_id != self.hypothesis_id:
            raise ContractError("report hypothesis differs from its registry entry")
        ids = [r.fill_model_id for r in self.results]
        if len(set(ids)) != len(ids):
            raise ContractError("duplicate fill model results")
        for end in (BRACKET_OPTIMISTIC, BRACKET_CONSERVATIVE):
            if end not in ids:
                raise ContractError(f"report must carry the bracket end {end!r}")
        if any(
            d.hypothesis_id != self.hypothesis_id
            for r in self.results
            for d in r.decisions
        ):
            raise ContractError("a decision belongs to another hypothesis")

    def result(self, fill_model_id: str) -> FillModelResult:
        for r in self.results:
            if r.fill_model_id == fill_model_id:
                return r
        raise KeyError(fill_model_id)

    @property
    def sha256(self) -> str:
        return sha256_canonical(self)

    @staticmethod
    def check_registry_header(header: Mapping[str, Any]) -> RegistryRef:
        """Validate a serialised report header before anything else is read.

        A stored report must name its registry entry; a header without
        ``registry.registry_hash`` is rejected, never defaulted. Any future
        report loader calls this first (v1 has no loader).
        """

        registry = header.get("registry")
        if not isinstance(registry, Mapping):
            raise MissingRegistryReference("report header has no 'registry' object")
        if not registry.get("registry_hash"):
            raise MissingRegistryReference("report header has no registry_hash")
        return RegistryRef(
            hypothesis_id=registry.get("hypothesis_id", ""),
            registry_hash=registry["registry_hash"],
            k=registry.get("k", 0),
        )
