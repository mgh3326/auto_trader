"""ROB-1205 — the Binance link branch of ``_load_native`` executed for real.

Every other test of :class:`AuthoritativeEvidenceReader` either mocks the
session, substitutes a fake reader, or asserts on ``inspect.getsource``
strings.  None of them reach ``_load_native``'s per-link Binance identity
check, which is why ``instrument.symbol`` (an attribute
:class:`CryptoInstrument` has never had) survived on ``main``.

These tests build the lineage with the production
:class:`PaperCohortRunner` — snapshot, decision, intent, link and both
native ledger rows are produced by shipping code, not hand-written — and
then drive ``load()`` end to end so the identity comparison is actually
evaluated.  Inverting or deleting that comparison must turn one of them
red.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.crypto_instruments import CryptoInstrument
from app.models.paper_cohort import (
    PaperRunOrderLink,
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.paper_evaluation import EvaluationConfig as EvaluationConfigRow
from app.models.paper_evaluation import EvaluationEpoch as EvaluationEpochRow
from app.models.paper_validation import PaperValidationStateTransition
from app.models.research_backtest import (
    ResearchBacktestRun,
    ResearchStrategyExperiment,
)
from app.models.review import AlpacaPaperOrderLedger
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.application import PaperExecutionApplication
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
)
from app.services.paper_cohort.cohort_service import PaperCohortService
from app.services.paper_cohort.contracts import (
    CohortAssignmentInput,
    PaperCohortError,
    RunMode,
    SymbolTargetWeight,
)
from app.services.paper_cohort.native_links import NativeOrderIdentity
from app.services.paper_cohort.provenance import PaperCohortProvenanceVerifier
from app.services.paper_cohort.runner import CohortRunInvocation, PaperCohortRunner
from app.services.paper_evaluation.contracts import EvaluationConfigError
from app.services.paper_evaluation.evidence import AuthoritativeEvidenceReader
from app.services.paper_validation.contracts import ActorRole
from app.services.paper_validation.service import PaperValidationService
from tests.services.paper_cohort.test_cohort_service import _activation
from tests.services.paper_cohort.test_market_snapshot import CAPTURED_AT
from tests.services.paper_cohort.test_runner_shadow import FakeCapture, FakeQuotes
from tests.services.paper_evaluation.conftest import make_evaluation_config
from tests.services.paper_validation.conftest import (
    FakeActorRoleProvider,
    FakeFrozenInputHashProvider,
    FakePolicyHashProvider,
    stable_hash,
)

pytestmark = [
    pytest.mark.integration,
    # Same rationale as tests/services/paper_cohort/test_runner_paper_active.py:
    # this file writes review.alpaca_paper_order_ledger rows through the global
    # ``db_session``, so it must hold the ROB-968 cleanup advisory lock.
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]

SHADOW_START = CAPTURED_AT - timedelta(hours=2)
PAPER_START = CAPTURED_AT - timedelta(hours=1)
# ``FakeQuotes`` marks at CAPTURED_AT + 300ms; a fill must land at or before the
# resolved as-of mark or ``_load_native`` drops it from the window.
FILLED_AT = CAPTURED_AT + timedelta(milliseconds=200)
ALPACA_SYMBOL = {"BTCUSDT": "BTC/USD", "ETHUSDT": "ETH/USD"}
COHORT_SYMBOLS = ("BTCUSDT", "ETHUSDT")
# Demo hosts are product-disjoint (see CLAUDE.md ROB-298); keep the ledger row's
# host consistent with its own discriminator.
DEMO_HOST = {"spot": "demo-api.binance.com", "usdm_futures": "demo-fapi.binance.com"}


def _evaluated_at() -> datetime:
    # ``paper_run_order_links.created_at`` is a server-side ``now()`` and the
    # loader filters ``link.created_at <= end``.
    return datetime.now(UTC) + timedelta(hours=1)


@pytest.fixture(autouse=True)
def _enabled_server_flags(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", True)


@dataclass
class _RecordingAdapter:
    """Paper adapter that remembers which cohort symbol each order carries."""

    broker: Broker
    symbol_by_client: dict[str, str]

    async def submit(self, intent):
        suffix = stable_hash(f"{self.broker.value}:{intent.idempotency_key}")[:16]
        client_order_id = f"client-{suffix}"
        self.symbol_by_client[client_order_id] = intent.symbol
        return PaperOperationResult(
            operation=PaperOperation.SUBMIT,
            status=PaperOperationStatus.SUCCEEDED,
            reason_code="ok",
            venue=self.broker,
            native_order_id=f"broker-{suffix}",
            native_client_order_id=client_order_id,
        )

    async def cancel(self, intent):  # pragma: no cover - unused by these tests
        raise AssertionError("no cancel in this fixture")


@dataclass
class _Registry:
    adapters: dict[Broker, _RecordingAdapter]

    def resolve(self, broker: Broker) -> _RecordingAdapter:
        return self.adapters[broker]


async def _instrument_id(
    session: AsyncSession,
    venue_symbol: str,
    *,
    venue: str = "binance",
    product: str = "spot",
) -> int:
    """Resolve-or-create the ``(venue, product, venue_symbol)`` identity.

    Mirrors ``BinanceDemoLedgerRepository.resolve_or_create_instrument``: the
    triple is unique, so a row seeded by an earlier test must be reused rather
    than duplicated.  All three components are parameters because
    ``venue_symbol`` on its own identifies nothing — ``binance/spot/BTCUSDT``
    and ``binance/usdm_futures/BTCUSDT`` are different, equally valid rows.
    """
    existing = await session.scalar(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == venue,
            CryptoInstrument.product == product,
            CryptoInstrument.venue_symbol == venue_symbol,
        )
    )
    if existing is not None:
        return existing.id
    instrument = CryptoInstrument(
        venue=venue,
        product=product,
        venue_symbol=venue_symbol,
        base_asset=venue_symbol.removesuffix("USDT"),
        quote_asset="USDT",
        status="active",
    )
    session.add(instrument)
    await session.flush()
    return instrument.id


@dataclass
class _LedgerBackedNativeResolver:
    """Materialise a real native ledger row for each submitted order.

    ``instrument_venue`` / ``instrument_product`` / ``instrument_venue_symbol``
    select which ``crypto_instruments`` row the Binance ledger row points at,
    and ``ledger_product`` sets the ledger row's own discriminator, so a test
    can make the identity wrong in exactly one component at a time.
    """

    session: AsyncSession
    symbol_by_client: dict[str, str]
    instrument_venue_symbol: dict[str, str] = field(
        default_factory=lambda: {symbol: symbol for symbol in COHORT_SYMBOLS}
    )
    instrument_venue: str = "binance"
    instrument_product: str = "spot"
    ledger_product: str = "spot"
    filled_at: datetime = FILLED_AT

    async def resolve(
        self, venue: str, client_order_id: str, broker_order_id: str
    ) -> NativeOrderIdentity:
        symbol = self.symbol_by_client[client_order_id]
        if venue == "binance":
            row: BinanceDemoOrderLedger | AlpacaPaperOrderLedger = (
                BinanceDemoOrderLedger(
                    instrument_id=await _instrument_id(
                        self.session,
                        self.instrument_venue_symbol[symbol],
                        venue=self.instrument_venue,
                        product=self.instrument_product,
                    ),
                    product=self.ledger_product,
                    venue_host=DEMO_HOST[self.ledger_product],
                    client_order_id=client_order_id,
                    broker_order_id=broker_order_id,
                    side="BUY",
                    order_type="MARKET",
                    qty=Decimal("1"),
                    # ``filled`` is a *blocking root* state: the partial-unique
                    # index allows one per (product, instrument), and these
                    # tests deliberately share the real BTCUSDT/ETHUSDT
                    # instrument rows. A settled round trip is terminal anyway,
                    # and the reader accepts filled/closed/reconciled alike.
                    lifecycle_state="reconciled",
                    filled_at=self.filled_at,
                    closed_at=self.filled_at,
                    reconciled_at=self.filled_at,
                    extra_metadata={
                        "filled_qty": "1",
                        "filled_avg_price": "100",
                        "fee_usdt": "0.1",
                    },
                )
            )
            kind = "binance_demo_order_ledger"
        else:
            row = AlpacaPaperOrderLedger(
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
                lifecycle_correlation_id=f"corr-{uuid4().hex[:12]}",
                record_kind="execution",
                broker="alpaca",
                account_mode="alpaca_paper_lab",
                lifecycle_state="filled",
                # The Alpaca paper intent already carries the venue symbol.
                execution_symbol=ALPACA_SYMBOL.get(symbol, symbol),
                execution_venue="alpaca_paper",
                instrument_type="equity_us",
                side="buy",
                order_type="market",
                currency="USD",
                requested_qty=Decimal("1"),
                filled_qty=Decimal("1"),
                filled_avg_price=Decimal("100"),
                fee_amount=Decimal("0"),
                fee_currency="USD",
                raw_responses={"filled_at": self.filled_at.isoformat()},
            )
            kind = "alpaca_paper_order_ledger"
        self.session.add(row)
        await self.session.flush()
        return NativeOrderIdentity(
            venue=venue,
            ledger_kind=kind,
            ledger_row_id=row.id,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
        )

    async def resolve_prepared(  # pragma: no cover - recovery path unused here
        self, request, provenance
    ) -> NativeOrderIdentity:
        del request, provenance
        raise PaperCohortError("native_order_not_found")


async def _authoritative_history(session: AsyncSession, activation) -> None:
    """Seed the transition path with explicit gate timestamps.

    ``_authoritative_history`` in the cohort-service tests relies on a
    server-side ``now()``, which cannot satisfy the reader's
    ``shadow_start < paper_start`` window check.
    """
    path = ["draft", "offline_eligible", "shadow_soak", "paper_active"]
    stamps = {"shadow_soak": SHADOW_START, "paper_active": PAPER_START}
    for assignment in activation.assignments:
        for sequence, new_state in enumerate(path, start=1):
            session.add(
                PaperValidationStateTransition(
                    validation_id=assignment.validation_id,
                    validation_version=assignment.validation_version,
                    experiment_id=assignment.experiment_id,
                    strategy_version_id=assignment.strategy_version_id,
                    cohort_id=activation.cohort_id,
                    sequence=sequence,
                    idempotency_key=f"activate-{assignment.validation_id}-{sequence}",
                    request_hash=stable_hash(
                        f"request-{assignment.validation_id}-{sequence}"
                    ),
                    prior_state=None if sequence == 1 else path[sequence - 2],
                    new_state=new_state,
                    actor_id="operator-1",
                    actor_role="operator",
                    reason_code="test_evidence",
                    reason_text="ROB-1205 native evidence history",
                    experiment_hash=assignment.experiment_hash,
                    cohort_hash=activation.expected_cohort_hash,
                    strategy_hash=assignment.strategy_hash,
                    config_hash=assignment.config_hash,
                    policy_hash=assignment.policy_hash,
                    input_hash=assignment.input_hash,
                    input_bundle_id=f"bundle-{assignment.assignment_id}",
                    policy_version="policy-v1",
                    evidence_ids=["evidence-1"],
                    created_at=stamps.get(new_state, SHADOW_START - timedelta(hours=1)),
                )
            )
    await session.flush()


@dataclass(frozen=True)
class _Lineage:
    cohort_id: str
    assignment_id: str
    links: tuple[PaperRunOrderLink, ...]


async def _build_lineage(
    session: AsyncSession,
    *,
    instrument_venue_symbol: dict[str, str] | None = None,
    instrument_venue: str = "binance",
    instrument_product: str = "spot",
    ledger_product: str = "spot",
    filled_at: datetime = FILLED_AT,
) -> _Lineage:
    """Run the production paper-active pipeline and seed the evaluation epoch.

    ``filled_at`` stamps both native ledger rows.  It is a parameter because
    ``_load_native`` and ``PaperEvaluationPnL.compute_native_evidence_view``
    want the fill on opposite sides of the venue mark — the loader drops a fill
    later than the resolved as-of mark, while the P&L view needs a mark at or
    before every fill to value the *other* symbol's open inventory.  The
    default keeps this module's own lineage unchanged.
    """
    nonce = uuid4().hex
    config = make_evaluation_config(min_observations=1, min_fills=1)
    experiment = ResearchStrategyExperiment(
        experiment_id=stable_hash(f"experiment-{nonce}"),
        strategy_key=f"strategy-{nonce}",
        strategy_version="strategy-v1",
        strategy_hash=stable_hash(f"strategy-{nonce}"),
        code_hash=stable_hash(f"code-{nonce}"),
        params_hash=stable_hash(f"params-{nonce}"),
        dataset_manifest_hash=stable_hash(f"dataset-{nonce}"),
        universe_hash=stable_hash(f"universe-{nonce}"),
        pit_hash=stable_hash(f"pit-{nonce}"),
        # The evaluation epoch is keyed by the assignment's config hash, so the
        # frozen experiment config and the evaluation config must agree.
        frozen_config_hash=config.config_hash(),
        policy_hash=stable_hash(f"policy-{nonce}"),
        benchmark_hash=stable_hash(f"benchmark-{nonce}"),
        cost_hash=stable_hash(f"cost-{nonce}"),
        mdd_hash=stable_hash(f"mdd-{nonce}"),
        manifest={},
    )
    session.add(experiment)
    await session.flush()
    backtest = ResearchBacktestRun(
        run_id=f"backtest-{nonce}",
        strategy_name=experiment.strategy_key,
        strategy_version=experiment.strategy_version,
        exchange="binance",
        market="spot",
        timeframe="1m",
        runner="pytest",
        total_trades=10,
        profit_factor=Decimal("1.2"),
        max_drawdown=Decimal("0.1"),
        strategy_experiment_id=experiment.id,
        trial_index=1,
        trial_status="completed",
        trial_idempotency_key=f"trial-{nonce}",
    )
    session.add(backtest)
    await session.flush()

    assignment_input = CohortAssignmentInput(
        assignment_id=f"assignment-{nonce}-0",
        ordinal=0,
        role="champion",
        validation_id=f"validation-{nonce}-0",
        validation_version=1,
        experiment_id=experiment.experiment_id,
        source_backtest_run_id=backtest.id,
        strategy_version_id=experiment.strategy_version,
        target_weights=(
            SymbolTargetWeight(symbol="BTCUSDT", weight=Decimal("0.6")),
            SymbolTargetWeight(symbol="ETHUSDT", weight=Decimal("0.4")),
        ),
        experiment_hash=experiment.experiment_id,
        strategy_hash=experiment.strategy_hash,
        config_hash=experiment.frozen_config_hash,
        policy_hash=experiment.policy_hash,
        input_hash=stable_hash(f"input-{nonce}"),
    )
    activation = _activation((assignment_input,), nonce=nonce).model_copy(
        update={"required_lookback": 3}
    )
    activation = activation.model_copy(
        update={"expected_cohort_hash": activation.computed_cohort_hash()}
    )
    await _authoritative_history(session, activation)
    await PaperCohortService(session).activate(activation)
    await session.commit()

    validation = PaperValidationService(
        session,
        actor_role_provider=FakeActorRoleProvider(
            {"paper-cohort-runner": ActorRole.SYSTEM}
        ),
        frozen_input_provider=FakeFrozenInputHashProvider(assignment_input.input_hash),
        policy_provider=FakePolicyHashProvider(assignment_input.policy_hash),
    )
    symbol_by_client: dict[str, str] = {}
    runner = PaperCohortRunner(
        session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(session),
        verifier=PaperCohortProvenanceVerifier(
            session,
            validation_service=validation,
            caller_id="paper-cohort-runner",
            clock=lambda: CAPTURED_AT + timedelta(seconds=1),
        ),
        application_factory=lambda verifier: PaperExecutionApplication(
            registry=_Registry(
                {
                    Broker.BINANCE: _RecordingAdapter(Broker.BINANCE, symbol_by_client),
                    Broker.ALPACA: _RecordingAdapter(Broker.ALPACA, symbol_by_client),
                }
            ),
            verifier=verifier,
        ),
        native_resolver=_LedgerBackedNativeResolver(
            session,
            symbol_by_client,
            instrument_venue_symbol=(
                instrument_venue_symbol
                if instrument_venue_symbol is not None
                else {symbol: symbol for symbol in COHORT_SYMBOLS}
            ),
            instrument_venue=instrument_venue,
            instrument_product=instrument_product,
            ledger_product=ledger_product,
            filled_at=filled_at,
        ),
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
        enablement=lambda _mode: True,
    )
    result = await runner.run(
        CohortRunInvocation(
            cohort_id=activation.cohort_id,
            run_id=f"run-{nonce}",
            round_decision_id=f"round-{nonce}",
            mode=RunMode.PAPER_ACTIVE,
        )
    )
    await session.commit()
    assert result.intent_count == 4

    links = tuple(
        (
            await session.scalars(
                select(PaperRunOrderLink).where(
                    PaperRunOrderLink.run_id == f"run-{nonce}"
                )
            )
        ).all()
    )
    assert {(link.venue, link.symbol) for link in links} == {
        (venue, symbol) for venue in ("binance", "alpaca") for symbol in COHORT_SYMBOLS
    }

    cohort = await session.scalar(
        select(PaperValidationCohort).where(
            PaperValidationCohort.cohort_id == activation.cohort_id
        )
    )
    assert cohort is not None
    # ``evaluation_configs`` is content-addressed and immutable, so a config
    # hash seeded by an earlier test is the same row, not a conflict.
    if (
        await session.scalar(
            select(EvaluationConfigRow).where(
                EvaluationConfigRow.config_hash == config.config_hash()
            )
        )
        is None
    ):
        session.add(
            EvaluationConfigRow(
                config_hash=config.config_hash(),
                schema_id="paper_evaluation_config.v1",
                formula_version="v1",
                currency_conversion_policy="none",
                payload=config.model_dump(mode="json"),
            )
        )
        await session.flush()
    session.add(
        EvaluationEpochRow(
            epoch_id=f"epoch-{nonce}",
            assignment_id=assignment_input.assignment_id,
            validation_id=assignment_input.validation_id,
            cohort_id=activation.cohort_id,
            config_hash=config.config_hash(),
            initial_equity={
                view.value: str(amount)
                for view, amount in config.initial_equity.items()
            },
            started_at=PAPER_START,
            experiment_hash=assignment_input.experiment_hash,
            cohort_hash=cohort.cohort_hash,
        )
    )
    await session.commit()
    return _Lineage(
        cohort_id=activation.cohort_id,
        assignment_id=assignment_input.assignment_id,
        links=links,
    )


@pytest.mark.asyncio
async def test_load_assembles_binance_fills_from_linked_instrument_identity(
    db_session: AsyncSession,
) -> None:
    """``load()`` completes and returns the Binance fills for real linked rows.

    The Binance branch resolves ``CryptoInstrument`` and compares it against
    the link, so this asserts on the assembled output rather than on source
    text.  Comparing the wrong instrument attribute raises ``AttributeError``;
    comparing the right one against the wrong link fails ``cross_wired``.
    """
    lineage = await _build_lineage(db_session)
    reader = AuthoritativeEvidenceReader(db_session)

    evidence = await reader.load(
        evaluated_at=_evaluated_at(),
        cohort_id=lineage.cohort_id,
        assignment_id=lineage.assignment_id,
    )

    binance_links = {
        link.symbol: link for link in lineage.links if link.venue == "binance"
    }
    assert {fill.symbol for fill in evidence.binance_fills} == set(COHORT_SYMBOLS)
    for fill in evidence.binance_fills:
        link = binance_links[fill.symbol]
        assert fill.native_row_id == link.native_ledger_row_id
        assert fill.client_order_id == link.client_order_id
        assert fill.broker_order_id == link.broker_order_id
        assert fill.quantity == Decimal("1")
        assert fill.price == Decimal("100")
        assert fill.fee == Decimal("0.1")
    # The instrument identity is what the branch under test resolves; confirm
    # the row it accepted really is the one the link names.
    for symbol, link in binance_links.items():
        ledger = await db_session.get(BinanceDemoOrderLedger, link.native_ledger_row_id)
        assert ledger is not None
        instrument = await db_session.get(CryptoInstrument, ledger.instrument_id)
        assert instrument is not None
        # The whole identity triple, not just the symbol.
        assert (instrument.venue, instrument.product, instrument.venue_symbol) == (
            "binance",
            "spot",
            symbol,
        )
        assert ledger.product == "spot"
    assert {fill.symbol for fill in evidence.alpaca_fills} == {"BTC/USD", "ETH/USD"}


@pytest.mark.asyncio
async def test_load_rejects_binance_row_bound_to_another_symbols_instrument(
    db_session: AsyncSession,
) -> None:
    """A Binance ledger row pointing at the wrong instrument must fail closed.

    Both instruments are real, active ``(binance, spot)`` rows — only the
    symbol binding is crossed.  Dropping the instrument-identity comparison
    lets this evidence through, so this test is what keeps that comparison
    alive.
    """
    lineage = await _build_lineage(
        db_session,
        instrument_venue_symbol={"BTCUSDT": "ETHUSDT", "ETHUSDT": "BTCUSDT"},
    )
    reader = AuthoritativeEvidenceReader(db_session)

    with pytest.raises(EvaluationConfigError) as exc:
        await reader.load(
            evaluated_at=_evaluated_at(),
            cohort_id=lineage.cohort_id,
            assignment_id=lineage.assignment_id,
        )
    assert exc.value.reason_code == "cross_wired_evidence"
    assert str(exc.value) == "linked Binance row mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ledger_product", "instrument_venue", "instrument_product"),
    [
        # The one the ROB-1205 r1 fix still admitted: a spot ledger row whose
        # FK target is the USD-M futures instrument carrying the very same
        # ``BTCUSDT``/``ETHUSDT`` venue_symbol. Both rows are legitimate —
        # scripts/binance_futures_demo_smoke.py seeds exactly this shape — so
        # ``venue_symbol`` alone cannot tell them apart, and accepting it books
        # cross-product fill evidence with no crash to notice.
        ("spot", "binance", "usdm_futures"),
        # Same hole one component over: nothing in the schema stops a Binance
        # ledger row's FK from landing on another venue's instrument.
        ("spot", "upbit", "spot"),
        # And the mirror image: a futures ledger row whose instrument is the
        # correct spot one. Here every ``instrument`` field matches, so only the
        # ledger row's own discriminator reveals that this fill did not happen
        # in the cohort's market.
        ("usdm_futures", "binance", "spot"),
    ],
    ids=[
        "usdm_futures_instrument_same_symbol",
        "other_venue_instrument_same_symbol",
        "futures_ledger_row_in_spot_cohort",
    ],
)
async def test_load_rejects_binance_row_bound_to_wrong_instrument_identity(
    db_session: AsyncSession,
    ledger_product: str,
    instrument_venue: str,
    instrument_product: str,
) -> None:
    """A spot cohort must not accept evidence off its ``(venue, product)``.

    ``crypto_instruments`` is unique on ``(venue, product, venue_symbol)``, so
    the reader must compare all three, and the ledger row's own ``product`` must
    agree with the cohort's market as well.  Each case breaks exactly one
    component and every one of them must fail closed.
    """
    lineage = await _build_lineage(
        db_session,
        instrument_venue=instrument_venue,
        instrument_product=instrument_product,
        ledger_product=ledger_product,
    )
    reader = AuthoritativeEvidenceReader(db_session)

    with pytest.raises(EvaluationConfigError) as exc:
        await reader.load(
            evaluated_at=_evaluated_at(),
            cohort_id=lineage.cohort_id,
            assignment_id=lineage.assignment_id,
        )
    assert exc.value.reason_code == "cross_wired_evidence"
    assert str(exc.value) == "linked Binance row mismatch"


@pytest.mark.asyncio
async def test_native_load_fails_closed_when_cohort_product_is_unresolvable(
    db_session: AsyncSession,
) -> None:
    """No cohort product means no way to bind identity — refuse, don't guess.

    ``paper_run_order_links`` carries venue and symbol but no product, so
    ``cohort.market`` is the only lineage-bound source of it.  If it cannot be
    resolved the reader must fail closed rather than fall back to assuming
    ``spot``.
    """
    reader = AuthoritativeEvidenceReader(db_session)
    with pytest.raises(EvaluationConfigError) as exc:
        await reader._load_native(
            assignment=PaperValidationCohortAssignment(
                assignment_id="assignment-x",
                cohort_id="cohort-x",
            ),
            cohort=PaperValidationCohort(cohort_id="cohort-x", market=None),
            start=PAPER_START,
            end=_evaluated_at(),
        )
    assert exc.value.reason_code == "missing_evidence"
    assert str(exc.value) == "cohort market missing"


@pytest.mark.asyncio
async def test_assignment_identity_is_required_before_any_native_read(
    db_session: AsyncSession,
) -> None:
    """The reader still refuses ambiguous identity against a real session."""
    reader = AuthoritativeEvidenceReader(db_session)
    with pytest.raises(EvaluationConfigError) as exc:
        await reader.load(
            evaluated_at=_evaluated_at(),
            validation_id="validation-x",
            cohort_id="cohort-x",
        )
    assert exc.value.reason_code == "invalid_evaluation_identity"
