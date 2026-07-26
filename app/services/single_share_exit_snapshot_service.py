"""Read-only evidence assembly for the KR single-share exit shadow lane.

Raw account rows and caller-supplied completeness booleans are not evaluator
inputs. A producer enumerates the configured account roster, asks each read port
for evidence over that roster, derives the roster hash, and returns a private
live or replay concrete context type that the matching evaluator recognizes by
exact type.

This type check is an API boundary, not an evidence-authenticity or security
boundary. Capability values are descriptive labels, and callers supply the read
ports; future live composition must therefore pin trusted adapter provenance
outside this module. In-process Python code must not treat this module as
protection from forged or mutated evidence.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from dataclasses import dataclass, fields
from decimal import Decimal
from enum import StrEnum
from json.encoder import encode_basestring_ascii
from operator import attrgetter
from typing import Protocol

PRODUCER_IDENTITY = "kr_single_share_exit_snapshot_producer/v2"
PRODUCER_CAPABILITY = "validated_exhaustive_kis_toss_snapshot/v2"
ROSTER_CAPABILITY = "configured_kr_account_roster_read/v1"
BROKER_EVIDENCE_CAPABILITY = "exhaustive_broker_account_evidence_read/v1"
OPEN_ACTION_CAPABILITY = "scoped_open_action_evidence_read/v1"
MARKET_EVIDENCE_CAPABILITY = "typed_executable_market_evidence_read/v1"

_REQUIRED_READER_CAPABILITIES = (
    ROSTER_CAPABILITY,
    BROKER_EVIDENCE_CAPABILITY,
    OPEN_ACTION_CAPABILITY,
    MARKET_EVIDENCE_CAPABILITY,
)


class KrBroker(StrEnum):
    KIS = "kis"
    TOSS = "toss"


class AccountKind(StrEnum):
    TAXABLE = "taxable"
    ISA = "isa"
    RETIREMENT = "retirement"


class ResistanceStrength(StrEnum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class QuoteVenue(StrEnum):
    KRX = "krx"
    NXT = "nxt"


class QuoteKind(StrEnum):
    BROKER_LAST_TRADE = "broker_last_trade"
    LIVE_ORDERBOOK_MID = "live_orderbook_mid"
    NXT_EXPECTED_PRICE = "nxt_expected_price"
    PREVIOUS_CLOSE_ECHO = "previous_close_echo"
    INDICATIVE_ONLY = "indicative_only"


class QuoteSource(StrEnum):
    KIS_BROKER = "kis_broker"
    TOSS_BROKER = "toss_broker"
    NXT_EXPECTED_MODEL = "nxt_expected_model"
    PREVIOUS_CLOSE = "previous_close"
    INDICATIVE_MODEL = "indicative_model"


class QuoteProvenance(StrEnum):
    VENUE_LAST_TRADE = "venue_last_trade"
    VENUE_BEST_BID = "venue_best_bid"
    VENUE_BEST_ASK = "venue_best_ask"
    PREVIOUS_CLOSE = "previous_close"
    INDICATIVE_MODEL = "indicative_model"


class ContextMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_kr_symbol(value: str) -> None:
    if len(value) != 6 or not value.isascii() or not value.isdigit():
        raise ValueError("symbol must be a six-digit KR code")


def _require_aware(value: dt.datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True, order=True)
class AccountIdentity:
    broker: KrBroker
    broker_account_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.broker, KrBroker):
            raise ValueError("broker must be a typed KrBroker")
        _require_text(self.broker_account_id, "broker_account_id")


@dataclass(frozen=True, slots=True)
class ConfiguredAccount:
    identity: AccountIdentity
    account_kind: AccountKind
    order_routable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.account_kind, AccountKind):
            raise ValueError("account_kind must be typed")


@dataclass(frozen=True, slots=True)
class AuthoritativeAccountRoster:
    roster_id: str
    roster_version: str
    roster_hash: str
    read_model_identity: str
    read_model_capability: str
    accounts: tuple[ConfiguredAccount, ...]

    def __post_init__(self) -> None:
        _require_text(self.roster_id, "roster_id")
        _require_text(self.roster_version, "roster_version")
        _require_text(self.roster_hash, "roster_hash")
        _require_text(self.read_model_identity, "read_model_identity")
        _require_text(self.read_model_capability, "read_model_capability")


@dataclass(frozen=True, slots=True)
class SingleShareExitTarget:
    symbol: str
    broker: KrBroker
    broker_account_id: str
    lot_id: str

    def __post_init__(self) -> None:
        _require_kr_symbol(self.symbol)
        if not isinstance(self.broker, KrBroker):
            raise ValueError("broker must be a typed KrBroker")
        _require_text(self.broker_account_id, "broker_account_id")
        _require_text(self.lot_id, "lot_id")

    @property
    def account_identity(self) -> AccountIdentity:
        return AccountIdentity(self.broker, self.broker_account_id)


@dataclass(frozen=True, slots=True)
class AccountLotEvidence:
    symbol: str
    lot_id: str
    sellable_quantity: Decimal
    average_cost: Decimal

    def __post_init__(self) -> None:
        _require_kr_symbol(self.symbol)
        _require_text(self.lot_id, "lot_id")
        if self.sellable_quantity < 0:
            raise ValueError("sellable_quantity must be non-negative")
        if self.average_cost <= 0:
            raise ValueError("average_cost must be positive")


@dataclass(frozen=True, slots=True)
class BrokerOpenOrderEvidence:
    order_id: str
    symbol: str
    side: str

    def __post_init__(self) -> None:
        _require_text(self.order_id, "order_id")
        _require_kr_symbol(self.symbol)
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")


@dataclass(frozen=True, slots=True)
class BrokerAccountEvidence:
    identity: AccountIdentity
    holdings_observed_at: dt.datetime
    lots: tuple[AccountLotEvidence, ...]
    open_orders_observed_at: dt.datetime
    open_orders: tuple[BrokerOpenOrderEvidence, ...]

    def __post_init__(self) -> None:
        _require_aware(self.holdings_observed_at, "holdings_observed_at")
        _require_aware(self.open_orders_observed_at, "open_orders_observed_at")


@dataclass(frozen=True, slots=True)
class OpenActionEvidence:
    action_id: str
    symbol: str
    side: str
    broker_account_id: str
    status: str

    def __post_init__(self) -> None:
        _require_text(self.action_id, "action_id")
        _require_kr_symbol(self.symbol)
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        _require_text(self.broker_account_id, "broker_account_id")
        if self.status not in {"open", "in_progress"}:
            raise ValueError("status must be unresolved")


@dataclass(frozen=True, slots=True)
class ScopedOpenActionsEvidence:
    observed_at: dt.datetime
    actions: tuple[OpenActionEvidence, ...]

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "open_actions observed_at")


@dataclass(frozen=True, slots=True)
class TypedQuoteEvidence:
    symbol: str
    venue: QuoteVenue
    quote_kind: QuoteKind
    source: QuoteSource
    observed_at: dt.datetime
    executable: bool
    firm: bool
    last_price: Decimal | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    last_provenance: QuoteProvenance | None = None
    bid_provenance: QuoteProvenance | None = None
    ask_provenance: QuoteProvenance | None = None

    def __post_init__(self) -> None:
        _require_kr_symbol(self.symbol)
        _require_aware(self.observed_at, "quote observed_at")
        if not isinstance(self.venue, QuoteVenue):
            raise ValueError("venue must be typed")
        if not isinstance(self.quote_kind, QuoteKind):
            raise ValueError("quote_kind must be typed")
        if not isinstance(self.source, QuoteSource):
            raise ValueError("quote source must be typed")
        prices = (self.last_price, self.bid_price, self.ask_price)
        if any(price is not None and price <= 0 for price in prices):
            raise ValueError("quote prices must be positive")


@dataclass(frozen=True, slots=True)
class ResistanceEvidence:
    symbol: str
    price: Decimal
    sources: tuple[str, ...]
    strength: ResistanceStrength
    computed_at: dt.datetime
    ohlcv_through_date: dt.date

    def __post_init__(self) -> None:
        _require_kr_symbol(self.symbol)
        if self.price <= 0:
            raise ValueError("resistance price must be positive")
        if not self.sources or any(not source.strip() for source in self.sources):
            raise ValueError("resistance sources must be non-empty")
        if not isinstance(self.strength, ResistanceStrength):
            raise ValueError("resistance strength must be typed")
        _require_aware(self.computed_at, "resistance computed_at")


@dataclass(frozen=True, slots=True)
class MarketEvidence:
    quote: TypedQuoteEvidence
    resistance: ResistanceEvidence | None


class ConfiguredAccountRosterReadModel(Protocol):
    identity: str
    capability: str

    async def read_configured_kr_accounts(self) -> AuthoritativeAccountRoster: ...


class BrokerAccountEvidenceReadModel(Protocol):
    identity: str
    capability: str

    async def read_accounts(
        self,
        *,
        symbol: str,
        expected_accounts: tuple[ConfiguredAccount, ...],
    ) -> tuple[BrokerAccountEvidence, ...]: ...


class ScopedOpenActionReadModel(Protocol):
    identity: str
    capability: str

    async def read_open_actions(
        self,
        *,
        symbol: str,
        side: str,
        broker_account_id: str,
    ) -> ScopedOpenActionsEvidence: ...


class TypedMarketEvidenceReadModel(Protocol):
    identity: str
    capability: str

    async def read_market_evidence(self, *, symbol: str) -> MarketEvidence: ...


class ReplayClock(Protocol):
    def now(self) -> dt.datetime: ...


class ValidatedSingleShareExitContext(Protocol):
    """Read-only interface returned by the producer API."""

    snapshot_id: str
    market: str
    captured_at: dt.datetime
    produced_at: dt.datetime
    target: SingleShareExitTarget
    roster_id: str
    roster_version: str
    roster_hash: str
    roster_read_model_identity: str
    roster_read_model_capability: str
    expected_account_identities: tuple[AccountIdentity, ...]
    observed_account_identities: tuple[AccountIdentity, ...]
    configured_accounts: tuple[ConfiguredAccount, ...]
    accounts: tuple[BrokerAccountEvidence, ...]
    quote: TypedQuoteEvidence
    resistance: ResistanceEvidence | None
    open_actions: ScopedOpenActionsEvidence
    producer_identity: str
    producer_capability: str
    reader_identities: tuple[str, ...]
    reader_capabilities: tuple[str, ...]

    @property
    def mode(self) -> ContextMode: ...

    @property
    def derived_roster_hash(self) -> str: ...

    @property
    def roster_is_exact(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class _ValidatedSingleShareExitContextBase:
    """Shared immutable payload for the two private evaluator context types."""

    snapshot_id: str
    market: str
    captured_at: dt.datetime
    produced_at: dt.datetime
    target: SingleShareExitTarget
    roster_id: str
    roster_version: str
    roster_hash: str
    roster_read_model_identity: str
    roster_read_model_capability: str
    expected_account_identities: tuple[AccountIdentity, ...]
    observed_account_identities: tuple[AccountIdentity, ...]
    configured_accounts: tuple[ConfiguredAccount, ...]
    accounts: tuple[BrokerAccountEvidence, ...]
    quote: TypedQuoteEvidence
    resistance: ResistanceEvidence | None
    open_actions: ScopedOpenActionsEvidence
    producer_identity: str
    producer_capability: str
    reader_identities: tuple[str, ...]
    reader_capabilities: tuple[str, ...]

    @property
    def derived_roster_hash(self) -> str:
        """Recompute the roster digest; never trust a second caller-carried hash."""

        return compute_account_roster_hash(
            roster_id=self.roster_id,
            roster_version=self.roster_version,
            read_model_identity=self.roster_read_model_identity,
            accounts=self.configured_accounts,
        )

    @property
    def roster_is_exact(self) -> bool:
        return self.expected_account_identities == self.observed_account_identities


class _ValidatedLiveSingleShareExitContext(_ValidatedSingleShareExitContextBase):
    """Private live context; mode is derived from this exact concrete type."""

    # Different layouts prevent ``object.__setattr__(replay, "__class__", live)``
    # from turning a replay object into a live object without storing a mutable
    # or caller-copyable mode field.
    __slots__ = ("_live_context_layout",)

    @property
    def mode(self) -> ContextMode:
        return ContextMode.LIVE


class _ValidatedReplaySingleShareExitContext(_ValidatedSingleShareExitContextBase):
    """Private replay context; mode is derived from this exact concrete type."""

    __slots__ = ("_replay_context_layout_a", "_replay_context_layout_b")

    @property
    def mode(self) -> ContextMode:
        return ContextMode.REPLAY


_CONTEXT_FIELD_NAMES = tuple(
    field.name for field in fields(_ValidatedSingleShareExitContextBase)
)
_CONTEXT_FIELDS_GETTER = attrgetter(*_CONTEXT_FIELD_NAMES)


def compute_account_roster_hash(
    *,
    roster_id: str,
    roster_version: str,
    read_model_identity: str,
    accounts: tuple[ConfiguredAccount, ...],
) -> str:
    ordered = sorted(
        accounts,
        key=lambda account: (
            account.identity.broker.value,
            account.identity.broker_account_id,
        ),
    )
    # Preserve the byte-for-byte canonical JSON contract without rebuilding and
    # sorting a nested dict on every evaluation. ``encode_basestring_ascii`` is
    # the string encoder used by the stdlib JSON encoder, so quotes, control
    # characters, and non-ASCII account IDs retain the prior representation.
    encoded_accounts = ",".join(
        (
            '{"account_kind":'
            f"{encode_basestring_ascii(account.account_kind.value)},"
            '"broker":'
            f"{encode_basestring_ascii(account.identity.broker.value)},"
            '"broker_account_id":'
            f"{encode_basestring_ascii(account.identity.broker_account_id)},"
            '"order_routable":'
            f"{'true' if account.order_routable else 'false'}"
            "}"
        )
        for account in ordered
    )
    payload = (
        f'{{"accounts":[{encoded_accounts}],"read_model_identity":'
        f"{encode_basestring_ascii(read_model_identity)},"
        f'"roster_id":{encode_basestring_ascii(roster_id)},'
        f'"roster_version":{encode_basestring_ascii(roster_version)}}}'
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def executable_quote_price(quote: TypedQuoteEvidence) -> Decimal | None:
    broker_sources = {QuoteSource.KIS_BROKER, QuoteSource.TOSS_BROKER}
    if (
        not quote.executable
        or not quote.firm
        or quote.source not in broker_sources
        or quote.venue not in {QuoteVenue.KRX, QuoteVenue.NXT}
    ):
        return None
    if quote.quote_kind is QuoteKind.BROKER_LAST_TRADE:
        if (
            quote.last_price is None
            or quote.last_provenance is not QuoteProvenance.VENUE_LAST_TRADE
        ):
            return None
        return quote.last_price
    if quote.quote_kind is QuoteKind.LIVE_ORDERBOOK_MID:
        if (
            quote.bid_price is None
            or quote.ask_price is None
            or quote.bid_price > quote.ask_price
            or quote.bid_provenance is not QuoteProvenance.VENUE_BEST_BID
            or quote.ask_provenance is not QuoteProvenance.VENUE_BEST_ASK
        ):
            return None
        return (quote.bid_price + quote.ask_price) / Decimal("2")
    return None


class _SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)


class _Producer:
    def __init__(
        self,
        *,
        roster_reader: ConfiguredAccountRosterReadModel,
        broker_reader: BrokerAccountEvidenceReadModel,
        open_action_reader: ScopedOpenActionReadModel,
        market_reader: TypedMarketEvidenceReadModel,
    ) -> None:
        self._roster_reader = roster_reader
        self._broker_reader = broker_reader
        self._open_action_reader = open_action_reader
        self._market_reader = market_reader

    async def _collect_state(
        self,
        *,
        target: SingleShareExitTarget,
        mode: ContextMode,
        clock: ReplayClock,
        context_type: type[_ValidatedSingleShareExitContextBase],
    ) -> _ValidatedSingleShareExitContextBase:
        roster = await self._roster_reader.read_configured_kr_accounts()
        accounts, open_actions, market = await asyncio.gather(
            self._broker_reader.read_accounts(
                symbol=target.symbol,
                expected_accounts=roster.accounts,
            ),
            self._open_action_reader.read_open_actions(
                symbol=target.symbol,
                side="sell",
                broker_account_id=target.broker_account_id,
            ),
            self._market_reader.read_market_evidence(symbol=target.symbol),
        )
        now = clock.now()
        _require_aware(now, "producer clock")
        captured_at = now.astimezone(dt.UTC)
        expected = tuple(
            sorted(
                (account.identity for account in roster.accounts),
                key=lambda identity: (
                    identity.broker.value,
                    identity.broker_account_id,
                ),
            )
        )
        observed = tuple(
            sorted(
                (account.identity for account in accounts),
                key=lambda identity: (
                    identity.broker.value,
                    identity.broker_account_id,
                ),
            )
        )
        derived_hash = compute_account_roster_hash(
            roster_id=roster.roster_id,
            roster_version=roster.roster_version,
            read_model_identity=roster.read_model_identity,
            accounts=roster.accounts,
        )
        snapshot_seed = "|".join(
            (
                roster.roster_hash,
                captured_at.isoformat(),
                target.symbol,
                target.broker.value,
                target.broker_account_id,
                target.lot_id,
                mode.value,
                derived_hash,
            )
        )
        snapshot_id = hashlib.sha256(snapshot_seed.encode()).hexdigest()[:24]
        reader_identities = (
            self._roster_reader.identity,
            self._broker_reader.identity,
            self._open_action_reader.identity,
            self._market_reader.identity,
        )
        reader_capabilities = (
            self._roster_reader.capability,
            self._broker_reader.capability,
            self._open_action_reader.capability,
            self._market_reader.capability,
        )
        return context_type(
            snapshot_id=snapshot_id,
            market="kr",
            captured_at=captured_at,
            produced_at=captured_at,
            target=target,
            roster_id=roster.roster_id,
            roster_version=roster.roster_version,
            roster_hash=roster.roster_hash,
            roster_read_model_identity=roster.read_model_identity,
            roster_read_model_capability=roster.read_model_capability,
            expected_account_identities=expected,
            observed_account_identities=observed,
            configured_accounts=roster.accounts,
            accounts=accounts,
            quote=market.quote,
            resistance=market.resistance,
            open_actions=open_actions,
            producer_identity=PRODUCER_IDENTITY,
            producer_capability=PRODUCER_CAPABILITY,
            reader_identities=reader_identities,
            reader_capabilities=reader_capabilities,
        )


class SingleShareExitSnapshotProducer(_Producer):
    """Live producer. Its clock is internal and cannot be caller-supplied."""

    async def produce(
        self,
        *,
        target: SingleShareExitTarget,
    ) -> ValidatedSingleShareExitContext:
        return await self._collect_state(
            target=target,
            mode=ContextMode.LIVE,
            clock=_SystemClock(),
            context_type=_ValidatedLiveSingleShareExitContext,
        )


class SingleShareExitReplayProducer(_Producer):
    """Replay producer whose exact context type is rejected by the live wrapper."""

    def __init__(
        self,
        *,
        roster_reader: ConfiguredAccountRosterReadModel,
        broker_reader: BrokerAccountEvidenceReadModel,
        open_action_reader: ScopedOpenActionReadModel,
        market_reader: TypedMarketEvidenceReadModel,
        replay_clock: ReplayClock,
    ) -> None:
        super().__init__(
            roster_reader=roster_reader,
            broker_reader=broker_reader,
            open_action_reader=open_action_reader,
            market_reader=market_reader,
        )
        self._replay_clock = replay_clock

    async def produce(
        self,
        *,
        target: SingleShareExitTarget,
    ) -> ValidatedSingleShareExitContext:
        return await self._collect_state(
            target=target,
            mode=ContextMode.REPLAY,
            clock=self._replay_clock,
            context_type=_ValidatedReplaySingleShareExitContext,
        )


def _is_initialized_exact_context(
    candidate: object,
    expected_type: type[_ValidatedSingleShareExitContextBase],
) -> bool:
    if type(candidate) is not expected_type:
        return False
    try:
        _CONTEXT_FIELDS_GETTER(candidate)
    except AttributeError:
        return False
    return True


def is_validated_live_context(candidate: object) -> bool:
    """Return whether an initialized exact live context was supplied."""

    return _is_initialized_exact_context(
        candidate, _ValidatedLiveSingleShareExitContext
    )


def is_validated_replay_context(candidate: object) -> bool:
    """Return whether an initialized exact replay context was supplied."""

    return _is_initialized_exact_context(
        candidate, _ValidatedReplaySingleShareExitContext
    )


def is_validated_context(candidate: object) -> bool:
    """Return whether either initialized exact producer context was supplied."""

    return is_validated_live_context(candidate) or is_validated_replay_context(
        candidate
    )


def required_reader_capabilities() -> tuple[str, ...]:
    return _REQUIRED_READER_CAPABILITIES
