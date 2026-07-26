from __future__ import annotations

import ast
import asyncio
import copy
import inspect
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.schemas.trading_policy as policy_schema
import app.services.single_share_exit_snapshot_service as snapshot_service
from app.schemas.trading_policy import TradingPolicyDocument
from app.services import trading_policy_service as policy
from app.services.single_share_exit_snapshot_service import (
    BROKER_EVIDENCE_CAPABILITY,
    MARKET_EVIDENCE_CAPABILITY,
    OPEN_ACTION_CAPABILITY,
    ROSTER_CAPABILITY,
    AccountIdentity,
    AccountKind,
    AccountLotEvidence,
    AuthoritativeAccountRoster,
    BrokerAccountEvidence,
    BrokerOpenOrderEvidence,
    ConfiguredAccount,
    KrBroker,
    MarketEvidence,
    OpenActionEvidence,
    QuoteKind,
    QuoteProvenance,
    QuoteSource,
    QuoteVenue,
    ResistanceEvidence,
    ResistanceStrength,
    ScopedOpenActionsEvidence,
    SingleShareExitReplayProducer,
    SingleShareExitSnapshotProducer,
    SingleShareExitTarget,
    TypedQuoteEvidence,
    ValidatedSingleShareExitContext,
    compute_account_roster_hash,
)

_CLOCK_AT = datetime(2026, 7, 23, 6, 50, 30, tzinfo=UTC)  # 15:50:30 KST
_EVIDENCE_AT = datetime(2026, 7, 23, 6, 50, 0, tzinfo=UTC)  # 15:50 KST
_EXPECTED_KRX_BAR = date(2026, 7, 23)


@pytest.fixture(autouse=True)
def _stub_expected_completed_krx_bar(monkeypatch):
    monkeypatch.setattr(
        policy, "_expected_completed_krx_bar", lambda _now: _EXPECTED_KRX_BAR
    )


class _FixedClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


class _RosterReader:
    identity = "offline.authoritative_roster"
    capability = ROSTER_CAPABILITY

    def __init__(
        self,
        accounts: tuple[ConfiguredAccount, ...],
        *,
        declared_hash: str | None = None,
    ):
        self.accounts = accounts
        self.declared_hash = declared_hash

    async def read_configured_kr_accounts(self) -> AuthoritativeAccountRoster:
        roster_id = "kr-live-routable-accounts"
        roster_version = "2026-07-23T15:50:00+09:00"
        roster_hash = self.declared_hash or compute_account_roster_hash(
            roster_id=roster_id,
            roster_version=roster_version,
            read_model_identity=self.identity,
            accounts=self.accounts,
        )
        return AuthoritativeAccountRoster(
            roster_id=roster_id,
            roster_version=roster_version,
            roster_hash=roster_hash,
            read_model_identity=self.identity,
            read_model_capability=self.capability,
            accounts=self.accounts,
        )


class _BrokerReader:
    identity = "offline.exhaustive_broker_evidence"
    capability = BROKER_EVIDENCE_CAPABILITY

    def __init__(self, accounts: tuple[BrokerAccountEvidence, ...]):
        self.accounts = accounts
        self.requested_accounts: tuple[ConfiguredAccount, ...] | None = None

    async def read_accounts(self, *, symbol, expected_accounts):
        self.requested_accounts = expected_accounts
        return self.accounts


class _OpenActionReader:
    identity = "offline.scoped_open_actions"
    capability = OPEN_ACTION_CAPABILITY

    def __init__(self, evidence: ScopedOpenActionsEvidence):
        self.evidence = evidence
        self.scope = None

    async def read_open_actions(self, *, symbol, side, broker_account_id):
        self.scope = (symbol, side, broker_account_id)
        return self.evidence


class _MarketReader:
    identity = "offline.typed_market_evidence"
    capability = MARKET_EVIDENCE_CAPABILITY

    def __init__(self, evidence: MarketEvidence):
        self.evidence = evidence

    async def read_market_evidence(self, *, symbol):
        assert self.evidence.quote.symbol == symbol
        return self.evidence


def _identity(broker: KrBroker, account_id: str) -> AccountIdentity:
    return AccountIdentity(broker, account_id)


def _configured(
    broker: KrBroker,
    account_id: str,
    *,
    routable: bool = True,
    kind: AccountKind = AccountKind.TAXABLE,
) -> ConfiguredAccount:
    return ConfiguredAccount(
        identity=_identity(broker, account_id),
        account_kind=kind,
        order_routable=routable,
    )


def _lot(
    *,
    symbol: str = "257720",
    lot_id: str = "target-lot",
    quantity: str = "1",
    average_cost: str = "31800",
) -> AccountLotEvidence:
    return AccountLotEvidence(
        symbol=symbol,
        lot_id=lot_id,
        sellable_quantity=Decimal(quantity),
        average_cost=Decimal(average_cost),
    )


def _account(
    broker: KrBroker,
    account_id: str,
    *,
    lots: tuple[AccountLotEvidence, ...] = (),
    orders: tuple[BrokerOpenOrderEvidence, ...] = (),
    holdings_at: datetime = _EVIDENCE_AT,
    orders_at: datetime = _EVIDENCE_AT,
) -> BrokerAccountEvidence:
    return BrokerAccountEvidence(
        identity=_identity(broker, account_id),
        holdings_observed_at=holdings_at,
        lots=lots,
        open_orders_observed_at=orders_at,
        open_orders=orders,
    )


def _firm_quote(
    *,
    symbol: str = "257720",
    price: str = "36450",
    observed_at: datetime = _EVIDENCE_AT,
    venue: QuoteVenue = QuoteVenue.NXT,
) -> TypedQuoteEvidence:
    return TypedQuoteEvidence(
        symbol=symbol,
        venue=venue,
        quote_kind=QuoteKind.BROKER_LAST_TRADE,
        source=QuoteSource.KIS_BROKER,
        observed_at=observed_at,
        executable=True,
        firm=True,
        last_price=Decimal(price),
        last_provenance=QuoteProvenance.VENUE_LAST_TRADE,
    )


def _resistance(
    *,
    symbol: str = "257720",
    price: str = "39946.31",
    sources: tuple[str, ...] = ("bb_upper", "fib_50"),
    strength: ResistanceStrength = ResistanceStrength.STRONG,
    computed_at: datetime = _EVIDENCE_AT,
) -> ResistanceEvidence:
    return ResistanceEvidence(
        symbol=symbol,
        price=Decimal(price),
        sources=sources,
        strength=strength,
        computed_at=computed_at,
        ohlcv_through_date=_EXPECTED_KRX_BAR,
    )


def _default_roster() -> tuple[ConfiguredAccount, ...]:
    return (
        _configured(KrBroker.KIS, "kis-main"),
        _configured(KrBroker.TOSS, "toss-main"),
    )


def _default_accounts(
    *,
    target_broker: KrBroker = KrBroker.TOSS,
    target_account_id: str = "toss-main",
    symbol: str = "257720",
    average_cost: str = "31800",
) -> tuple[BrokerAccountEvidence, ...]:
    target_lot = _lot(symbol=symbol, average_cost=average_cost)
    return (
        _account(
            KrBroker.KIS,
            "kis-main",
            lots=(target_lot,) if target_broker is KrBroker.KIS else (),
        ),
        _account(
            KrBroker.TOSS,
            "toss-main",
            lots=(target_lot,) if target_broker is KrBroker.TOSS else (),
        ),
    )


def _make_context(
    *,
    symbol: str = "257720",
    target_broker: KrBroker = KrBroker.TOSS,
    target_account_id: str = "toss-main",
    target_lot_id: str = "target-lot",
    roster: tuple[ConfiguredAccount, ...] | None = None,
    accounts: tuple[BrokerAccountEvidence, ...] | None = None,
    quote: TypedQuoteEvidence | None = None,
    resistance: ResistanceEvidence | None | object = ...,
    actions: tuple[OpenActionEvidence, ...] = (),
    actions_at: datetime = _EVIDENCE_AT,
    clock_at: datetime = _CLOCK_AT,
    declared_roster_hash: str | None = None,
    live: bool = False,
) -> ValidatedSingleShareExitContext:
    configured_accounts = roster or _default_roster()
    broker_accounts = accounts or _default_accounts(
        target_broker=target_broker,
        target_account_id=target_account_id,
        symbol=symbol,
    )
    quote_evidence = quote or _firm_quote(symbol=symbol)
    resistance_evidence = (
        _resistance(symbol=symbol) if resistance is ... else resistance
    )
    roster_reader = _RosterReader(
        configured_accounts,
        declared_hash=declared_roster_hash,
    )
    broker_reader = _BrokerReader(broker_accounts)
    open_action_reader = _OpenActionReader(
        ScopedOpenActionsEvidence(observed_at=actions_at, actions=actions)
    )
    market_reader = _MarketReader(
        MarketEvidence(
            quote=quote_evidence,
            resistance=resistance_evidence,
        )
    )
    producer_args = {
        "roster_reader": roster_reader,
        "broker_reader": broker_reader,
        "open_action_reader": open_action_reader,
        "market_reader": market_reader,
    }
    if live:
        producer = SingleShareExitSnapshotProducer(**producer_args)
    else:
        producer = SingleShareExitReplayProducer(
            **producer_args,
            replay_clock=_FixedClock(clock_at),
        )
    target = SingleShareExitTarget(
        symbol=symbol,
        broker=target_broker,
        broker_account_id=target_account_id,
        lot_id=target_lot_id,
    )
    return asyncio.run(producer.produce(target=target))


def _replay_result(**kwargs):
    return policy.evaluate_single_share_exit_replay(_make_context(**kwargs))


def _with_single_share_rule(doc, rule):
    return doc.model_copy(
        update={
            "decision_rules": {
                **doc.decision_rules,
                "sell.single_share_exit": rule,
            }
        }
    )


def test_raw_public_construction_and_completeness_flags_are_removed():
    assert not hasattr(policy_schema, "SingleShareExitEvidenceSnapshot")
    assert (
        "evaluated_at"
        not in inspect.signature(policy.evaluate_single_share_exit).parameters
    )
    assert (
        "evaluated_at"
        not in inspect.signature(policy.evaluate_single_share_exit_replay).parameters
    )


def test_producer_enumerates_exact_roster_and_scopes_open_actions():
    roster = (
        _configured(KrBroker.KIS, "kis-main"),
        _configured(KrBroker.KIS, "kis-secondary"),
        _configured(KrBroker.TOSS, "toss-main"),
    )
    broker_reader = _BrokerReader(
        (
            _account(KrBroker.KIS, "kis-main"),
            _account(KrBroker.KIS, "kis-secondary"),
            _account(KrBroker.TOSS, "toss-main", lots=(_lot(),)),
        )
    )
    open_reader = _OpenActionReader(
        ScopedOpenActionsEvidence(observed_at=_EVIDENCE_AT, actions=())
    )
    roster_reader = _RosterReader(roster)
    producer = SingleShareExitReplayProducer(
        roster_reader=roster_reader,
        broker_reader=broker_reader,
        open_action_reader=open_reader,
        market_reader=_MarketReader(
            MarketEvidence(quote=_firm_quote(), resistance=_resistance())
        ),
        replay_clock=_FixedClock(_CLOCK_AT),
    )
    context = asyncio.run(
        producer.produce(
            target=SingleShareExitTarget(
                "257720", KrBroker.TOSS, "toss-main", "target-lot"
            )
        )
    )

    assert broker_reader.requested_accounts == roster
    assert open_reader.scope == ("257720", "sell", "toss-main")
    assert context.expected_account_identities == (
        _identity(KrBroker.KIS, "kis-main"),
        _identity(KrBroker.KIS, "kis-secondary"),
        _identity(KrBroker.TOSS, "toss-main"),
    )
    assert context.expected_account_identities == context.observed_account_identities
    assert context.roster_hash == context.derived_roster_hash
    assert context.producer_capability


def test_omitted_account_cannot_claim_complete():
    roster = (
        _configured(KrBroker.KIS, "kis-main"),
        _configured(KrBroker.KIS, "kis-secondary"),
        _configured(KrBroker.TOSS, "toss-main"),
    )
    observed = (
        _account(KrBroker.KIS, "kis-main"),
        _account(KrBroker.TOSS, "toss-main", lots=(_lot(),)),
    )
    result = _replay_result(roster=roster, accounts=observed)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "expected_observed_account_roster_mismatch",
    )
    assert result.expected_account_identities == (
        "kis:kis-main",
        "kis:kis-secondary",
        "toss:toss-main",
    )
    assert result.observed_account_identities == ("kis:kis-main", "toss:toss-main")


def test_context_uses_private_exact_replay_type_with_derived_mode():
    context = _make_context()
    assert type(context).__name__ == "_ValidatedReplaySingleShareExitContext"
    assert snapshot_service.is_validated_context(context) is True
    assert snapshot_service.is_validated_replay_context(context) is True
    assert snapshot_service.is_validated_live_context(context) is False
    assert context.mode is snapshot_service.ContextMode.REPLAY
    assert "mode" not in {field.name for field in fields(context)}
    assert not hasattr(context, "model_copy")


def test_functional_copy_does_not_promote_replay_or_redeclare_derived_roster_hash():
    replay = _make_context()

    with pytest.raises(TypeError):
        replace(replay, mode=snapshot_service.ContextMode.LIVE)
    with pytest.raises(TypeError):
        copy.replace(replay, mode=snapshot_service.ContextMode.LIVE)
    with pytest.raises(AttributeError):
        replay.model_copy(update={"mode": snapshot_service.ContextMode.LIVE})  # type: ignore[attr-defined]

    for copied in (replace(replay), copy.replace(replay)):
        assert type(copied) is type(replay)
        result = policy.evaluate_single_share_exit(copied)
        assert (result.outcome, result.reason) == (
            "INELIGIBLE",
            "replay_context_not_live",
        )

    forged_hash = replace(replay, roster_hash="caller-claims-complete")
    assert forged_hash.derived_roster_hash != forged_hash.roster_hash
    result = policy.evaluate_single_share_exit_replay(forged_hash)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "account_roster_hash_mismatch",
    )


def test_exact_type_boundary_rejects_subclass_and_direct_differing_layout_swap():
    replay = _make_context()
    live = _make_context(live=True)
    replay_type = type(replay)

    class ReplaySubclass(replay_type):
        pass

    values = {field.name: getattr(replay, field.name) for field in fields(replay)}
    subclass = ReplaySubclass(**values)
    assert snapshot_service.is_validated_context(subclass) is False
    result = policy.evaluate_single_share_exit_replay(subclass)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "unvalidated_producer_context",
    )

    with pytest.raises(TypeError, match="object layout differs"):
        object.__setattr__(replay, "__class__", type(live))
    assert replay.mode is snapshot_service.ContextMode.REPLAY


def test_live_wrapper_ignores_class_level_replay_mode_spoof(monkeypatch):
    replay = _make_context()
    monkeypatch.setattr(
        type(replay),
        "mode",
        property(lambda _context: snapshot_service.ContextMode.LIVE),
    )

    assert replay.mode is snapshot_service.ContextMode.LIVE
    result = policy.evaluate_single_share_exit(replay)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "replay_context_not_live",
    )


@pytest.mark.parametrize("live", [False, True], ids=["replay", "live"])
def test_uninitialized_exact_context_is_structured_ineligible(live):
    context_type = type(_make_context(live=live))
    uninitialized = object.__new__(context_type)

    assert snapshot_service.is_validated_context(uninitialized) is False
    evaluator = (
        policy.evaluate_single_share_exit
        if live
        else policy.evaluate_single_share_exit_replay
    )
    result = evaluator(uninitialized)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "unvalidated_producer_context",
    )


def test_slotted_context_has_no_dict_mutation_path():
    context = _make_context()

    with pytest.raises(AttributeError):
        _ = context.__dict__
    with pytest.raises(AttributeError):
        object.__setattr__(
            context,
            "mode",
            snapshot_service.ContextMode.LIVE,
        )


def test_raw_context_shaped_object_is_rejected():
    configured_accounts = _default_roster()
    identities = tuple(account.identity for account in configured_accounts)
    roster_hash = compute_account_roster_hash(
        roster_id="caller-forged-roster",
        roster_version="caller-forged-v1",
        read_model_identity="caller.forged_roster",
        accounts=configured_accounts,
    )
    public_values = {
        "snapshot_id": "caller-forged-without-producer",
        "market": "kr",
        "captured_at": _CLOCK_AT,
        "produced_at": _CLOCK_AT,
        "mode": snapshot_service.ContextMode.REPLAY,
        "target": SingleShareExitTarget(
            "257720", KrBroker.TOSS, "toss-main", "target-lot"
        ),
        "roster_id": "caller-forged-roster",
        "roster_version": "caller-forged-v1",
        "roster_hash": roster_hash,
        "derived_roster_hash": roster_hash,
        "roster_read_model_identity": "caller.forged_roster",
        "roster_read_model_capability": ROSTER_CAPABILITY,
        "expected_account_identities": identities,
        "observed_account_identities": identities,
        "configured_accounts": configured_accounts,
        "accounts": _default_accounts(),
        "quote": _firm_quote(),
        "resistance": _resistance(),
        "open_actions": ScopedOpenActionsEvidence(_EVIDENCE_AT, ()),
        "producer_identity": snapshot_service.PRODUCER_IDENTITY,
        "producer_capability": snapshot_service.PRODUCER_CAPABILITY,
        "reader_identities": (
            "caller.forged_roster",
            "caller.forged_broker",
            "caller.forged_actions",
            "caller.forged_market",
        ),
        "reader_capabilities": (
            ROSTER_CAPABILITY,
            BROKER_EVIDENCE_CAPABILITY,
            OPEN_ACTION_CAPABILITY,
            MARKET_EVIDENCE_CAPABILITY,
        ),
    }

    forged_namespace = SimpleNamespace(**public_values)
    assert snapshot_service.is_validated_context(forged_namespace) is False
    result = policy.evaluate_single_share_exit_replay(forged_namespace)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "unvalidated_producer_context",
    )


def test_snapshot_service_has_no_app_or_broker_import_reachability():
    source = Path(snapshot_service.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    relative_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level
    ]

    assert not {module for module in imported_modules if module.startswith("app")}
    assert not relative_imports


def test_mismatched_declared_roster_hash_is_rejected():
    result = _replay_result(declared_roster_hash="0" * 64)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "account_roster_hash_mismatch",
    )


def test_roster_hash_canonical_compatibility_vector_is_unchanged():
    assert (
        compute_account_roster_hash(
            roster_id="kr-live-routable-accounts",
            roster_version="2026-07-23T15:50:00+09:00",
            read_model_identity="offline.authoritative_roster",
            accounts=_default_roster(),
        )
        == "34f9275b3b4c153af15f39200da470ad7f4e9acbf26795bc1a5ae94fed26c4a9"
    )
    assert (
        compute_account_roster_hash(
            roster_id='명부"A',
            roster_version="v1\\n",
            read_model_identity="reader/한글",
            accounts=(_configured(KrBroker.KIS, '계좌"\\n'),),
        )
        == "f4d927823d5cba703695fe9e7a642eedd751d1ecff92579e0dac3c6fe69089c2"
    )


def test_replay_clock_does_not_create_live_eligibility():
    old_clock = _CLOCK_AT - timedelta(days=1)
    old_evidence = _EVIDENCE_AT - timedelta(days=1)
    accounts = (
        _account(
            KrBroker.KIS,
            "kis-main",
            holdings_at=old_evidence,
            orders_at=old_evidence,
        ),
        _account(
            KrBroker.TOSS,
            "toss-main",
            lots=(_lot(),),
            holdings_at=old_evidence,
            orders_at=old_evidence,
        ),
    )
    context = _make_context(
        accounts=accounts,
        quote=_firm_quote(observed_at=old_evidence),
        resistance=_resistance(computed_at=old_evidence),
        actions_at=old_evidence,
        clock_at=old_clock,
    )
    assert policy.evaluate_single_share_exit_replay(context).outcome == (
        "REPLAY_ELIGIBLE"
    )
    live_result = policy.evaluate_single_share_exit(context)
    assert (live_result.outcome, live_result.reason) == (
        "INELIGIBLE",
        "replay_context_not_live",
    )
    with pytest.raises(TypeError):
        policy.evaluate_single_share_exit(context, evaluated_at=old_clock)  # type: ignore[call-arg]


def test_pairwise_600_second_endpoint_skew_is_rejected():
    oldest = _CLOCK_AT - timedelta(seconds=600)
    accounts = (
        _account(
            KrBroker.KIS,
            "kis-main",
            holdings_at=oldest,
            orders_at=oldest,
        ),
        _account(
            KrBroker.TOSS,
            "toss-main",
            lots=(_lot(),),
            holdings_at=oldest,
            orders_at=oldest,
        ),
    )
    result = _replay_result(
        accounts=accounts,
        quote=_firm_quote(observed_at=_CLOCK_AT),
        resistance=_resistance(computed_at=_CLOCK_AT),
        actions_at=_CLOCK_AT - timedelta(seconds=300),
    )
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "snapshot_pairwise_skew_exceeded",
    )


@pytest.mark.parametrize(
    ("field", "expected_reason"),
    [
        ("quote", "stale_quote"),
        ("resistance", "stale_resistance"),
        ("holdings", "stale_holdings"),
        ("open_orders", "stale_open_orders"),
        ("open_actions", "stale_open_actions"),
    ],
)
def test_each_evidence_kind_has_wall_clock_age_limit(
    field,
    expected_reason,
    monkeypatch,
):
    doc = policy.load_trading_policy()
    rule = doc.decision_rules["sell.single_share_exit"]
    widened_conditions = rule.conditions.model_copy(
        update={"snapshot_max_skew_seconds": 600}
    )
    widened_rule = rule.model_copy(update={"conditions": widened_conditions})
    monkeypatch.setattr(
        policy,
        "_single_share_policy_document",
        lambda: doc.model_copy(
            update={
                "decision_rules": {
                    **doc.decision_rules,
                    "sell.single_share_exit": widened_rule,
                }
            }
        ),
    )
    stale = _CLOCK_AT - timedelta(seconds=301)
    fresh = _CLOCK_AT - timedelta(seconds=1)
    quote = _firm_quote(observed_at=stale if field == "quote" else fresh)
    resistance = _resistance(computed_at=stale if field == "resistance" else fresh)
    accounts = (
        _account(
            KrBroker.KIS,
            "kis-main",
            holdings_at=stale if field == "holdings" else fresh,
            orders_at=stale if field == "open_orders" else fresh,
        ),
        _account(
            KrBroker.TOSS,
            "toss-main",
            lots=(_lot(),),
            holdings_at=stale if field == "holdings" else fresh,
            orders_at=stale if field == "open_orders" else fresh,
        ),
    )
    result = _replay_result(
        accounts=accounts,
        quote=quote,
        resistance=resistance,
        actions_at=stale if field == "open_actions" else fresh,
    )
    assert (result.outcome, result.reason) == ("INELIGIBLE", expected_reason)


@pytest.mark.parametrize(
    ("kind", "source"),
    [
        (QuoteKind.NXT_EXPECTED_PRICE, QuoteSource.NXT_EXPECTED_MODEL),
        (QuoteKind.PREVIOUS_CLOSE_ECHO, QuoteSource.PREVIOUS_CLOSE),
        (QuoteKind.INDICATIVE_ONLY, QuoteSource.INDICATIVE_MODEL),
    ],
)
def test_non_executable_quote_kinds_fail_closed(kind, source):
    quote = TypedQuoteEvidence(
        symbol="257720",
        venue=QuoteVenue.NXT,
        quote_kind=kind,
        source=source,
        observed_at=_EVIDENCE_AT,
        executable=False,
        firm=False,
        last_price=Decimal("36450"),
        last_provenance=(
            QuoteProvenance.PREVIOUS_CLOSE
            if kind is QuoteKind.PREVIOUS_CLOSE_ECHO
            else QuoteProvenance.INDICATIVE_MODEL
        ),
    )
    result = _replay_result(quote=quote)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "quote_quality_not_executable",
    )


def test_unknown_string_quote_source_does_not_form_typed_evidence():
    with pytest.raises(ValueError, match="quote source must be typed"):
        TypedQuoteEvidence(
            symbol="257720",
            venue=QuoteVenue.NXT,
            quote_kind=QuoteKind.BROKER_LAST_TRADE,
            source="unknown",  # type: ignore[arg-type]
            observed_at=_EVIDENCE_AT,
            executable=True,
            firm=True,
            last_price=Decimal("36450"),
            last_provenance=QuoteProvenance.VENUE_LAST_TRADE,
        )


def test_two_sided_live_orderbook_mid_is_executable():
    quote = TypedQuoteEvidence(
        symbol="257720",
        venue=QuoteVenue.NXT,
        quote_kind=QuoteKind.LIVE_ORDERBOOK_MID,
        source=QuoteSource.TOSS_BROKER,
        observed_at=_EVIDENCE_AT,
        executable=True,
        firm=True,
        bid_price=Decimal("36400"),
        ask_price=Decimal("36500"),
        bid_provenance=QuoteProvenance.VENUE_BEST_BID,
        ask_provenance=QuoteProvenance.VENUE_BEST_ASK,
    )
    result = _replay_result(quote=quote)
    assert result.outcome == "REPLAY_ELIGIBLE"
    assert result.current_quote == Decimal("36450")


def test_no_resistance_reference_is_ineligible():
    result = _replay_result(resistance=None)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "no_resistance_reference",
    )


@pytest.mark.parametrize(
    "strength",
    [ResistanceStrength.WEAK, ResistanceStrength.MODERATE],
)
def test_resistance_must_be_strong(strength):
    result = _replay_result(resistance=_resistance(strength=strength))
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "resistance_strength_below_required",
    )
    assert result.resistance_strength == strength.value


def test_resistance_must_cover_expected_completed_krx_bar():
    resistance = ResistanceEvidence(
        symbol="257720",
        price=Decimal("39946.31"),
        sources=("bb_upper", "fib_50"),
        strength=ResistanceStrength.STRONG,
        computed_at=_EVIDENCE_AT,
        ohlcv_through_date=date(2026, 7, 22),
    )
    result = _replay_result(resistance=resistance)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "ohlcv_not_through_expected_completed_krx_bar",
    )
    assert result.expected_completed_krx_bar_date == _EXPECTED_KRX_BAR


def test_same_symbol_broker_open_order_defers():
    pending = BrokerOpenOrderEvidence("pending-1", "257720", "buy")
    accounts = (
        _account(KrBroker.KIS, "kis-main", orders=(pending,)),
        _account(KrBroker.TOSS, "toss-main", lots=(_lot(),)),
    )
    result = _replay_result(accounts=accounts)
    assert (result.outcome, result.reason) == (
        "DEFER",
        "same_symbol_broker_open_order",
    )


def test_scoped_open_action_defers_and_scope_is_exact():
    action = OpenActionEvidence(
        "2fd4fa42",
        "257720",
        "sell",
        "toss-main",
        "open",
    )
    result = _replay_result(actions=(action,))
    assert (result.outcome, result.reason) == (
        "DEFER",
        "unresolved_scoped_open_action",
    )


def test_target_non_routable_is_ineligible():
    roster = (
        _configured(KrBroker.KIS, "kis-main"),
        _configured(KrBroker.TOSS, "toss-main"),
        _configured(
            KrBroker.TOSS,
            "toss-isa",
            routable=False,
            kind=AccountKind.ISA,
        ),
    )
    accounts = (
        _account(KrBroker.KIS, "kis-main"),
        _account(KrBroker.TOSS, "toss-main"),
        _account(KrBroker.TOSS, "toss-isa", lots=(_lot(),)),
    )
    result = _replay_result(
        target_account_id="toss-isa",
        roster=roster,
        accounts=accounts,
    )
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "target_account_not_routable",
    )


def test_secondary_non_routable_retirement_lot_is_excluded_from_aggregate():
    roster = (
        _configured(KrBroker.KIS, "kis-main"),
        _configured(KrBroker.TOSS, "toss-main"),
        _configured(
            KrBroker.KIS,
            "kis-retirement",
            routable=False,
            kind=AccountKind.RETIREMENT,
        ),
    )
    accounts = (
        _account(KrBroker.KIS, "kis-main"),
        _account(KrBroker.TOSS, "toss-main", lots=(_lot(),)),
        _account(
            KrBroker.KIS,
            "kis-retirement",
            lots=(_lot(lot_id="retirement-lot", quantity="99"),),
        ),
    )
    result = _replay_result(roster=roster, accounts=accounts)
    assert result.outcome == "REPLAY_ELIGIBLE"
    assert result.symbol_routable_sellable_quantity == Decimal("1")


def test_actual_total_quantity_over_one_fails_with_one_account_lot():
    roster = (
        _configured(KrBroker.KIS, "kis-main"),
        _configured(KrBroker.KIS, "kis-secondary"),
        _configured(KrBroker.TOSS, "toss-main"),
    )
    omitted_secondary = (
        _account(KrBroker.KIS, "kis-main"),
        _account(KrBroker.TOSS, "toss-main", lots=(_lot(),)),
    )
    omitted_result = _replay_result(roster=roster, accounts=omitted_secondary)
    assert omitted_result.reason == "expected_observed_account_roster_mismatch"

    exhaustive = (
        _account(KrBroker.KIS, "kis-main"),
        _account(
            KrBroker.KIS,
            "kis-secondary",
            lots=(_lot(lot_id="secondary", quantity="3"),),
        ),
        _account(KrBroker.TOSS, "toss-main", lots=(_lot(),)),
    )
    exhaustive_result = _replay_result(roster=roster, accounts=exhaustive)
    assert (exhaustive_result.outcome, exhaustive_result.reason) == (
        "INELIGIBLE",
        "symbol_routable_sellable_quantity_not_one",
    )
    assert exhaustive_result.symbol_routable_sellable_quantity == Decimal("4")


@pytest.mark.parametrize(
    (
        "symbol",
        "average_cost",
        "quote_price",
        "resistance_price",
        "sources",
        "expected_profit",
        "expected_distance",
        "expected_outcome",
        "expected_reason",
    ),
    [
        (
            "257720",
            "31800",
            "36450",
            "39946.31",
            ("bb_upper", "fib_50"),
            "14.6226",
            "9.5921",
            "REPLAY_ELIGIBLE",
            "replay_candidate_no_live_eligibility",
        ),
        (
            "042660",
            "78800",
            "89400",
            "100548.9",
            ("fib_38.2", "volume_value_area_low"),
            "13.4518",
            "12.4708",
            "REPLAY_ELIGIBLE",
            "replay_candidate_no_live_eligibility",
        ),
        (
            "086790",
            "117000",
            "130500",
            "142264.52",
            ("bb_upper", "fib_0"),
            "11.5385",
            "9.0150",
            "REPLAY_ELIGIBLE",
            "replay_candidate_no_live_eligibility",
        ),
        (
            "035420",
            "197900",
            "220000",
            "242550",
            ("fib_50",),
            "11.1673",
            "10.2500",
            "INELIGIBLE",
            "insufficient_independent_resistance_families",
        ),
    ],
)
def test_synthetic_firm_quotes_exact_decimal_far_band_and_family_normalization(
    symbol,
    average_cost,
    quote_price,
    resistance_price,
    sources,
    expected_profit,
    expected_distance,
    expected_outcome,
    expected_reason,
):
    accounts = _default_accounts(symbol=symbol, average_cost=average_cost)
    result = _replay_result(
        symbol=symbol,
        accounts=accounts,
        quote=_firm_quote(symbol=symbol, price=quote_price),
        resistance=_resistance(
            symbol=symbol,
            price=resistance_price,
            sources=sources,
        ),
    )
    assert (result.outcome, result.reason) == (expected_outcome, expected_reason)
    assert result.average_cost == Decimal(average_cost)
    assert result.current_quote == Decimal(quote_price)
    assert result.symbol_routable_sellable_quantity == Decimal("1")
    assert result.profit_pct == Decimal(expected_profit)
    assert result.resistance_distance_pct == Decimal(expected_distance)


def test_operational_1550_nxt_evidence_is_quote_quality_rejected_exactly():
    fixtures = (
        (
            "257720",
            KrBroker.TOSS,
            "31800",
            "36450",
            "39946.31",
            ("bb_upper", "fib_50"),
            (
                _account(
                    KrBroker.KIS,
                    "kis-main",
                    lots=(
                        _lot(
                            lot_id="kis-lot",
                            quantity="5",
                            average_cost="35000",
                        ),
                    ),
                ),
                _account(
                    KrBroker.TOSS,
                    "toss-main",
                    lots=(_lot(quantity="1", average_cost="31800"),),
                ),
            ),
            (),
        ),
        (
            "042660",
            KrBroker.TOSS,
            "78800",
            "89400",
            "100548.9",
            ("fib_38.2", "volume_value_area_low"),
            (
                _account(KrBroker.KIS, "kis-main"),
                _account(
                    KrBroker.TOSS,
                    "toss-main",
                    lots=(
                        _lot(
                            symbol="042660",
                            quantity="1",
                            average_cost="78800",
                        ),
                    ),
                ),
            ),
            (),
        ),
        (
            "086790",
            KrBroker.KIS,
            "117000",
            "130500",
            "142264.52",
            ("bb_upper", "fib_0"),
            (
                _account(
                    KrBroker.KIS,
                    "kis-main",
                    lots=(
                        _lot(
                            symbol="086790",
                            quantity="1",
                            average_cost="117000",
                        ),
                    ),
                ),
                _account(KrBroker.TOSS, "toss-main"),
            ),
            (
                OpenActionEvidence(
                    "2fd4fa42",
                    "086790",
                    "sell",
                    "kis-main",
                    "open",
                ),
            ),
        ),
        (
            "035420",
            KrBroker.TOSS,
            "197900",
            "220000",
            "242550",
            ("fib_50",),
            (
                _account(
                    KrBroker.KIS,
                    "kis-main",
                    lots=(
                        _lot(
                            symbol="035420",
                            lot_id="kis-lot",
                            quantity="3",
                            average_cost="241500",
                        ),
                    ),
                ),
                _account(
                    KrBroker.TOSS,
                    "toss-main",
                    lots=(
                        _lot(
                            symbol="035420",
                            quantity="1",
                            average_cost="197900",
                        ),
                    ),
                ),
            ),
            (),
        ),
    )
    for (
        symbol,
        target_broker,
        average_cost,
        quote_price,
        resistance_price,
        sources,
        accounts,
        actions,
    ) in fixtures:
        quote = TypedQuoteEvidence(
            symbol=symbol,
            venue=QuoteVenue.NXT,
            quote_kind=QuoteKind.NXT_EXPECTED_PRICE,
            source=QuoteSource.NXT_EXPECTED_MODEL,
            observed_at=_EVIDENCE_AT,
            executable=False,
            firm=False,
            last_price=Decimal(quote_price),
            last_provenance=QuoteProvenance.INDICATIVE_MODEL,
        )
        context = _make_context(
            symbol=symbol,
            target_broker=target_broker,
            target_account_id=(
                "kis-main" if target_broker is KrBroker.KIS else "toss-main"
            ),
            accounts=accounts,
            quote=quote,
            resistance=_resistance(
                symbol=symbol,
                price=resistance_price,
                sources=sources,
            ),
            actions=actions,
        )
        result = policy.evaluate_single_share_exit_replay(context)
        assert (result.outcome, result.reason) == (
            "INELIGIBLE",
            "quote_quality_not_executable",
        )
        target_account = next(
            account
            for account in context.accounts
            if account.identity == context.target.account_identity
        )
        target_lot = next(
            lot for lot in target_account.lots if lot.lot_id == "target-lot"
        )
        assert target_lot.average_cost == Decimal(average_cost)
        assert target_lot.sellable_quantity == Decimal("1")
        assert result.quote_kind == "nxt_expected_price"
        assert result.quote_source == "nxt_expected_model"
        assert result.quote_observed_at == _EVIDENCE_AT
        assert result.profit_pct is None
        assert result.resistance_distance_pct is None


def test_257720_authoritative_toss_one_plus_kis_five_is_quantity_six():
    accounts = (
        _account(
            KrBroker.KIS,
            "kis-main",
            lots=(_lot(lot_id="kis-lot", quantity="5", average_cost="35000"),),
        ),
        _account(
            KrBroker.TOSS,
            "toss-main",
            lots=(_lot(quantity="1", average_cost="31800"),),
        ),
    )
    result = _replay_result(accounts=accounts)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "symbol_routable_sellable_quantity_not_one",
    )
    assert result.symbol_routable_sellable_quantity == Decimal("6")


def test_naver_authoritative_toss_one_plus_kis_three_is_quantity_four():
    accounts = (
        _account(
            KrBroker.KIS,
            "kis-main",
            lots=(
                _lot(
                    symbol="035420",
                    lot_id="kis-lot",
                    quantity="3",
                    average_cost="241500",
                ),
            ),
        ),
        _account(
            KrBroker.TOSS,
            "toss-main",
            lots=(
                _lot(
                    symbol="035420",
                    quantity="1",
                    average_cost="197900",
                ),
            ),
        ),
    )
    result = _replay_result(
        symbol="035420",
        accounts=accounts,
        quote=_firm_quote(symbol="035420", price="220000"),
        resistance=_resistance(
            symbol="035420",
            price="242550",
            sources=("fib_50",),
        ),
    )
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "symbol_routable_sellable_quantity_not_one",
    )
    assert result.symbol_routable_sellable_quantity == Decimal("4")


def test_loss_guard_is_decimal_and_separate():
    accounts = _default_accounts(average_cost="100000")
    result = _replay_result(
        accounts=accounts,
        quote=_firm_quote(price="100999.9999"),
        resistance=_resistance(price="112000"),
    )
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "loss_guard_not_met",
    )
    assert result.average_cost == Decimal("100000")
    assert result.current_quote == Decimal("100999.9999")


def test_multiple_fibonacci_sources_normalize_to_one_independent_family():
    result = _replay_result(
        resistance=_resistance(sources=("fib_0", "fib_38.2", "fib_50"))
    )
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "insufficient_independent_resistance_families",
    )
    assert result.normalized_source_families == ("FIBONACCI",)


@pytest.mark.parametrize(
    ("resistance_price", "expected"),
    [
        ("38637", "INELIGIBLE"),  # exactly +6%, exclusive
        ("41917.5", "REPLAY_ELIGIBLE"),  # exactly +15%, inclusive
        ("41917.5001", "INELIGIBLE"),
    ],
)
def test_far_band_exact_boundaries(resistance_price, expected):
    result = _replay_result(resistance=_resistance(price=resistance_price))
    assert result.outcome == expected


def test_public_policy_loader_returns_detached_policy_copy():
    detached = policy.load_trading_policy()
    detached_rule = detached.decision_rules["sell.single_share_exit"]
    detached_rule.proposal.auto_approve = True
    detached.thresholds["sell.loss_guard_min_multiple"].value = 0.01

    fresh = policy.load_trading_policy()
    cached = policy._single_share_policy_document()
    for document in (fresh, cached):
        rule = document.decision_rules["sell.single_share_exit"]
        assert rule.proposal.auto_approve is False
        assert document.thresholds["sell.loss_guard_min_multiple"].value == 1.01
    assert detached is not fresh


@pytest.mark.parametrize(
    ("component", "field", "unsafe_value"),
    [
        ("proposal", "action", "order_proposal_create"),
        ("proposal", "sizing", "partial_lot"),
        ("proposal", "approval", "none"),
        ("proposal", "auto_approve", True),
        ("proposal", "execution", "direct_broker_submit"),
        ("rule", "operator_approval_required", False),
        ("conditions", "min_sell_price_multiple_policy_key", "other.key"),
    ],
)
def test_exact_policy_safety_projection_is_rechecked(
    monkeypatch,
    component,
    field,
    unsafe_value,
):
    doc = policy.load_trading_policy()
    rule = doc.decision_rules["sell.single_share_exit"]
    if component == "proposal":
        mutated = rule.model_copy(
            update={"proposal": rule.proposal.model_copy(update={field: unsafe_value})}
        )
    elif component == "conditions":
        mutated = rule.model_copy(
            update={
                "conditions": rule.conditions.model_copy(update={field: unsafe_value})
            }
        )
    else:
        mutated = rule.model_copy(update={field: unsafe_value})
    monkeypatch.setattr(
        policy,
        "_single_share_policy_document",
        lambda: _with_single_share_rule(doc, mutated),
    )

    result = _replay_result()
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "policy_safe_projection_mismatch",
    )
    assert result.candidate_action is None
    assert result.auto_approve is False
    assert result.execution is None


def test_unsafe_nested_proposal_does_not_leak_auto_approval_metadata(
    monkeypatch,
):
    now = datetime.now(UTC)
    expected_date = now.date()
    monkeypatch.setattr(
        policy, "_expected_completed_krx_bar", lambda _now: expected_date
    )
    doc = policy.load_trading_policy()
    rule = doc.decision_rules["sell.single_share_exit"]
    rule.proposal.auto_approve = True
    rule.proposal.execution = "direct_broker_submit"
    monkeypatch.setattr(policy, "_single_share_policy_document", lambda: doc)
    accounts = (
        _account(
            KrBroker.KIS,
            "kis-main",
            holdings_at=now,
            orders_at=now,
        ),
        _account(
            KrBroker.TOSS,
            "toss-main",
            lots=(_lot(),),
            holdings_at=now,
            orders_at=now,
        ),
    )
    context = _make_context(
        accounts=accounts,
        quote=_firm_quote(observed_at=now),
        resistance=ResistanceEvidence(
            symbol="257720",
            price=Decimal("39946.31"),
            sources=("bb_upper", "fib_50"),
            strength=ResistanceStrength.STRONG,
            computed_at=now,
            ohlcv_through_date=expected_date,
        ),
        actions_at=now,
        live=True,
    )

    result = policy.evaluate_single_share_exit(context)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "policy_safe_projection_mismatch",
    )
    assert result.proposal_enabled is False
    assert result.candidate_action is None
    assert result.auto_approve is False
    assert result.execution is None


def test_schema_valid_policy_below_code_loss_floor_stays_ineligible(
    monkeypatch,
):
    raw = policy.load_trading_policy().model_dump(mode="python")
    raw["thresholds"]["sell.loss_guard_min_multiple"]["value"] = 0.01
    raw["decision_rules"]["sell.single_share_exit"]["conditions"]["profit_pct_min"] = 0
    schema_valid = TradingPolicyDocument.model_validate(raw)
    monkeypatch.setattr(
        policy,
        "_single_share_policy_document",
        lambda: schema_valid,
    )

    result = _replay_result(
        accounts=_default_accounts(average_cost="100000"),
        quote=_firm_quote(price="100000"),
        resistance=_resistance(price="112000"),
    )
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "loss_guard_not_met",
    )
    assert result.average_cost == Decimal("100000")
    assert result.current_quote == Decimal("100000")


@pytest.mark.parametrize(
    "configured_value", [float("nan"), float("inf"), float("-inf")]
)
def test_schema_valid_non_finite_loss_guard_fails_closed(
    monkeypatch,
    configured_value,
):
    raw = policy.load_trading_policy().model_dump(mode="python")
    raw["thresholds"]["sell.loss_guard_min_multiple"]["value"] = configured_value
    schema_valid = TradingPolicyDocument.model_validate(raw)
    monkeypatch.setattr(
        policy,
        "_single_share_policy_document",
        lambda: schema_valid,
    )

    result = _replay_result()
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "invalid_loss_guard_policy",
    )


@pytest.mark.parametrize(
    "gate_mutation",
    [
        {"activation_state": "live"},
        {"proposal_enabled": True},
    ],
    ids=["activation-live", "proposal-enabled"],
)
def test_activation_and_proposal_off_are_rechecked(monkeypatch, gate_mutation):
    context = _make_context()
    doc = policy.load_trading_policy()
    rule = doc.decision_rules["sell.single_share_exit"]
    bypassed = rule.model_copy(update=gate_mutation)
    monkeypatch.setattr(
        policy,
        "_single_share_policy_document",
        lambda: doc.model_copy(
            update={
                "decision_rules": {
                    **doc.decision_rules,
                    "sell.single_share_exit": bypassed,
                }
            }
        ),
    )
    result = policy.evaluate_single_share_exit_replay(context)
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "policy_not_shadow_off",
    )
    assert result.candidate_action is None


def test_live_producer_uses_internal_clock_and_remains_shadow_off(monkeypatch):
    now = datetime.now(UTC)
    expected_date = now.date()
    monkeypatch.setattr(
        policy, "_expected_completed_krx_bar", lambda _now: expected_date
    )
    accounts = (
        _account(
            KrBroker.KIS,
            "kis-main",
            holdings_at=now,
            orders_at=now,
        ),
        _account(
            KrBroker.TOSS,
            "toss-main",
            lots=(_lot(),),
            holdings_at=now,
            orders_at=now,
        ),
    )
    context = _make_context(
        accounts=accounts,
        quote=_firm_quote(observed_at=now),
        resistance=ResistanceEvidence(
            symbol="257720",
            price=Decimal("39946.31"),
            sources=("bb_upper", "fib_50"),
            strength=ResistanceStrength.STRONG,
            computed_at=now,
            ohlcv_through_date=expected_date,
        ),
        actions_at=now,
        live=True,
    )
    result = policy.evaluate_single_share_exit(context)
    assert result.outcome == "SHADOW_ELIGIBLE"
    assert result.outcome != "PROPOSE"
    assert result.proposal_enabled is False
    assert result.candidate_action == "full_exit_at_far_resistance"
    assert result.sizing == "full_account_lot_exit"
    assert result.approval == "telegram_manual"
    assert result.auto_approve is False
    assert result.execution == "proposal_only"


def test_unsealed_raw_context_construction_is_fail_closed():
    result = policy.evaluate_single_share_exit(SimpleNamespace())
    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "unvalidated_producer_context",
    )
