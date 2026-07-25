"""Authoritative, read-only evidence assembly for the KR single-share exit lane.

Raw account rows and caller-supplied completeness booleans are deliberately not
an evaluator input.  A producer enumerates the configured account roster first,
asks each read port for evidence over that exact roster, and issues a sealed
context.  The trading-policy evaluator accepts only that context.

The ports are read-only capability boundaries.  Live broker adapters are not
constructed here; offline tests use fakes, and any future live composition must
provide all four authoritative capabilities explicitly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
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
    """Read-only interface implemented only by an identity-registered context."""

    snapshot_id: str
    market: str
    captured_at: dt.datetime
    produced_at: dt.datetime
    mode: ContextMode
    target: SingleShareExitTarget
    roster_id: str
    roster_version: str
    roster_hash: str
    derived_roster_hash: str
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
    def roster_is_exact(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class _ContextState:
    """Producer-collected data, never itself accepted by the evaluator."""

    snapshot_id: str
    market: str
    captured_at: dt.datetime
    produced_at: dt.datetime
    mode: ContextMode
    target: SingleShareExitTarget
    roster_id: str
    roster_version: str
    roster_hash: str
    derived_roster_hash: str
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


class ProducerContextIntegrityError(RuntimeError):
    """The object is not the exact context issued by its producer authority."""


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
    payload = {
        "roster_id": roster_id,
        "roster_version": roster_version,
        "read_model_identity": read_model_identity,
        "accounts": [
            {
                "broker": account.identity.broker.value,
                "broker_account_id": account.identity.broker_account_id,
                "account_kind": account.account_kind.value,
                "order_routable": account.order_routable,
            }
            for account in ordered
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
    ) -> _ContextState:
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
        return _ContextState(
            snapshot_id=snapshot_id,
            market="kr",
            captured_at=captured_at,
            produced_at=captured_at,
            mode=mode,
            target=target,
            roster_id=roster.roster_id,
            roster_version=roster.roster_version,
            roster_hash=roster.roster_hash,
            derived_roster_hash=derived_hash,
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


def _build_context_authority():
    """Build the only issuance authority without exporting issuance material."""

    issued_records: tuple[tuple[_Producer, object, _ContextState, bytes], ...] = ()
    state_field_names = frozenset(
        item.name for item in dataclasses.fields(_ContextState)
    )

    def canonical(value: object) -> object:
        if dataclasses.is_dataclass(value):
            return {
                item.name: canonical(getattr(value, item.name))
                for item in dataclasses.fields(value)
            }
        if isinstance(value, StrEnum):
            return value.value
        if isinstance(value, dt.datetime):
            return value.astimezone(dt.UTC).isoformat()
        if isinstance(value, dt.date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, tuple):
            return [canonical(item) for item in value]
        return value

    def state_snapshot(state: _ContextState) -> bytes:
        return json.dumps(
            canonical(state),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    def registered_state(candidate: object) -> _ContextState:
        for _issuer, issued_context, state, issued_snapshot in issued_records:
            if candidate is issued_context:
                if state_snapshot(state) != issued_snapshot:
                    raise ProducerContextIntegrityError(
                        "producer-issued context state changed after issuance"
                    )
                return state
        raise ProducerContextIntegrityError(
            "context was not issued by the producer authority"
        )

    def issue(
        issuer: _Producer,
        state: _ContextState,
    ) -> ValidatedSingleShareExitContext:
        nonlocal issued_records
        constructor_capability = object()

        class _IssuedContext:
            __slots__ = ()

            def __init__(self, capability: object) -> None:
                if capability is not constructor_capability:
                    raise ProducerContextIntegrityError(
                        "context construction is producer-only"
                    )

            def __getattribute__(self, name: str) -> object:
                current_state = registered_state(self)
                if name in state_field_names:
                    return getattr(current_state, name)
                if name == "roster_is_exact":
                    return (
                        current_state.expected_account_identities
                        == current_state.observed_account_identities
                    )
                return object.__getattribute__(self, name)

            def __repr__(self) -> str:
                current_state = registered_state(self)
                return (
                    "<ValidatedSingleShareExitContext "
                    f"snapshot_id={current_state.snapshot_id!r}>"
                )

            def __eq__(self, other: object) -> bool:
                registered_state(self)
                return self is other

            def __hash__(self) -> int:
                registered_state(self)
                return object.__hash__(self)

            def __copy__(self):
                registered_state(self)
                raise TypeError("producer-issued contexts cannot be copied")

            def __deepcopy__(self, _memo):
                registered_state(self)
                raise TypeError("producer-issued contexts cannot be deep-copied")

            def __reduce__(self):
                registered_state(self)
                raise TypeError("producer-issued contexts cannot be pickled")

            def __reduce_ex__(self, _protocol):
                registered_state(self)
                raise TypeError("producer-issued contexts cannot be pickled")

            @classmethod
            def __init_subclass__(cls, **_kwargs) -> None:
                raise TypeError("producer-issued context types cannot be subclassed")

        context = _IssuedContext(constructor_capability)
        issued_records += ((issuer, context, state, state_snapshot(state)),)
        return context

    def validate(candidate: object) -> bool:
        try:
            registered_state(candidate)
        except ProducerContextIntegrityError:
            return False
        return True

    class SnapshotProducer(_Producer):
        """Live producer. Its clock is internal and cannot be caller-supplied."""

        async def produce(
            self,
            *,
            target: SingleShareExitTarget,
        ) -> ValidatedSingleShareExitContext:
            state = await self._collect_state(
                target=target,
                mode=ContextMode.LIVE,
                clock=_SystemClock(),
            )
            return issue(self, state)

    class ReplayProducer(_Producer):
        """Replay-only producer; its contexts can never be live-eligible."""

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
            state = await self._collect_state(
                target=target,
                mode=ContextMode.REPLAY,
                clock=self._replay_clock,
            )
            return issue(self, state)

    return SnapshotProducer, ReplayProducer, validate


(
    SingleShareExitSnapshotProducer,
    SingleShareExitReplayProducer,
    is_validated_context,
) = _build_context_authority()
del _build_context_authority


def required_reader_capabilities() -> tuple[str, ...]:
    return _REQUIRED_READER_CAPABILITIES
