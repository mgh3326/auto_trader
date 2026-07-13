from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import PaperRunOrderLink
from app.models.paper_validation import PaperValidationStateTransition
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.application import PaperExecutionApplication
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
    PaperOrderRequest,
)
from app.services.paper_cohort.cohort_service import PaperCohortService
from app.services.paper_cohort.contracts import PaperCohortError, RunMode
from app.services.paper_cohort.native_links import NativeOrderIdentity
from app.services.paper_cohort.provenance import PaperCohortProvenanceVerifier
from app.services.paper_cohort.runner import CohortRunInvocation, PaperCohortRunner
from app.services.paper_validation.contracts import ActorRole
from app.services.paper_validation.service import PaperValidationService
from tests.services.paper_cohort.test_cohort_service import (
    _activation,
    _assignment,
    _authoritative_history,
    _registry_rows,
)
from tests.services.paper_cohort.test_market_snapshot import CAPTURED_AT
from tests.services.paper_cohort.test_runner_shadow import FakeCapture, FakeQuotes
from tests.services.paper_validation.conftest import (
    FakeActorRoleProvider,
    FakeFrozenInputHashProvider,
    FakePolicyHashProvider,
    stable_hash,
)

pytestmark = pytest.mark.integration


@dataclass
class FakeAdapter:
    broker: Broker
    calls: list[object] = field(default_factory=list)

    async def submit(self, intent):
        self.calls.append(intent)
        suffix = stable_hash(f"{self.broker.value}:{intent.idempotency_key}")[:16]
        return PaperOperationResult(
            operation=PaperOperation.SUBMIT,
            status=PaperOperationStatus.SUCCEEDED,
            reason_code="ok",
            venue=self.broker,
            native_order_id=f"broker-{suffix}",
            native_client_order_id=f"client-{suffix}",
        )


@dataclass
class FakeRegistry:
    adapters: dict[Broker, FakeAdapter]

    def resolve(self, broker: Broker) -> FakeAdapter:
        return self.adapters[broker]


@dataclass
class FakeNativeResolver:
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    identities: dict[tuple[str, str], NativeOrderIdentity] = field(default_factory=dict)

    async def resolve(
        self, venue: str, client_order_id: str, broker_order_id: str
    ) -> NativeOrderIdentity:
        self.calls.append((venue, client_order_id, broker_order_id))
        key = (venue, client_order_id)
        if key not in self.identities:
            self.identities[key] = NativeOrderIdentity(
                venue=venue,
                ledger_kind=(
                    "binance_demo_order_ledger"
                    if venue == "binance"
                    else "alpaca_paper_order_ledger"
                ),
                ledger_row_id=int(stable_hash(f"{venue}:{client_order_id}")[:12], 16),
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
            )
        return self.identities[key]


async def _setup(
    db_session: AsyncSession,
) -> tuple[str, object, PaperValidationService]:
    nonce = uuid4().hex
    experiment, backtest = await _registry_rows(db_session, nonce)
    assignment = _assignment(experiment, backtest, nonce=nonce)
    activation = _activation((assignment,), nonce=nonce)
    activation = activation.model_copy(update={"required_lookback": 3})
    activation = activation.model_copy(
        update={"expected_cohort_hash": activation.computed_cohort_hash()}
    )
    await _authoritative_history(db_session, activation, state="paper_active")
    await PaperCohortService(db_session).activate(activation)
    await db_session.commit()
    validation = PaperValidationService(
        db_session,
        actor_role_provider=FakeActorRoleProvider(
            {"paper-cohort-runner": ActorRole.SYSTEM}
        ),
        frozen_input_provider=FakeFrozenInputHashProvider(assignment.input_hash),
        policy_provider=FakePolicyHashProvider(assignment.policy_hash),
    )
    return nonce, activation, validation


def _app_factory(adapters: dict[Broker, FakeAdapter]):
    def build(verifier):
        return PaperExecutionApplication(
            registry=FakeRegistry(adapters), verifier=verifier
        )

    return build


@pytest.mark.asyncio
async def test_paper_active_submits_via_rob845_and_stores_only_native_links(
    db_session: AsyncSession,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT + timedelta(seconds=1),
    )
    adapters = {
        Broker.BINANCE: FakeAdapter(Broker.BINANCE),
        Broker.ALPACA: FakeAdapter(Broker.ALPACA),
    }
    native = FakeNativeResolver()
    runner = PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=_app_factory(adapters),
        native_resolver=native,
    )

    result = await runner.run(
        CohortRunInvocation(
            cohort_id=activation.cohort_id,
            run_id=f"run-{nonce}",
            round_decision_id=f"round-{nonce}",
            mode=RunMode.PAPER_ACTIVE,
        )
    )
    await db_session.commit()

    assert result.intent_count == 4
    assert len(adapters[Broker.BINANCE].calls) == 2
    assert len(adapters[Broker.ALPACA].calls) == 2
    assert len(native.calls) == 4
    links = (
        await db_session.scalars(
            select(PaperRunOrderLink).where(PaperRunOrderLink.run_id == f"run-{nonce}")
        )
    ).all()
    assert len(links) == 4
    assert all(link.snapshot_hash == result.snapshot_hash for link in links)
    assert all(link.client_order_id.startswith("client-") for link in links)
    assert all(link.broker_order_id.startswith("broker-") for link in links)

    verified = adapters[Broker.BINANCE].calls[0]
    original_request = PaperOrderRequest(
        **{name: getattr(verified, name) for name in PaperOrderRequest.model_fields}
    )
    adapter_baseline = len(adapters[Broker.BINANCE].calls)
    for field_name, bad_value in (
        ("cohort_id", "wrong-cohort"),
        ("strategy_hash", "0" * 64),
        ("config_hash", "0" * 64),
        ("policy_hash", "0" * 64),
        ("market_snapshot_hash", "0" * 64),
        ("market_snapshot_source", "wrong-source"),
    ):
        blocked = await PaperExecutionApplication(
            registry=FakeRegistry(adapters), verifier=verifier
        ).submit(original_request.model_copy(update={field_name: bad_value}))
        assert blocked.reason_code == "provenance_verification_failed"
    assert len(adapters[Broker.BINANCE].calls) == adapter_baseline


@pytest.mark.asyncio
async def test_non_exact_paper_active_state_blocks_adapter_native_and_links(
    db_session: AsyncSession,
) -> None:
    nonce, activation, validation = await _setup(db_session)
    assignment = activation.assignments[0]
    db_session.add(
        PaperValidationStateTransition(
            validation_id=assignment.validation_id,
            validation_version=assignment.validation_version,
            experiment_id=assignment.experiment_id,
            strategy_version_id=assignment.strategy_version_id,
            cohort_id=activation.cohort_id,
            sequence=5,
            idempotency_key=f"promotion-eligible-{nonce}",
            request_hash=stable_hash(f"promotion-eligible-{nonce}"),
            prior_state="paper_active",
            new_state="promotion_eligible",
            actor_id="operator-1",
            actor_role="operator",
            reason_code="test_promotion_gate",
            reason_text="state is authorizable in ROB-848 but not exact paper_active",
            experiment_hash=assignment.experiment_hash,
            cohort_hash=activation.expected_cohort_hash,
            strategy_hash=assignment.strategy_hash,
            config_hash=assignment.config_hash,
            policy_hash=assignment.policy_hash,
            input_hash=assignment.input_hash,
            input_bundle_id="bundle-1",
            policy_version="policy-v1",
            evidence_ids=["gate-1"],
        )
    )
    await db_session.commit()
    verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=lambda: CAPTURED_AT + timedelta(seconds=1),
    )
    adapters = {
        Broker.BINANCE: FakeAdapter(Broker.BINANCE),
        Broker.ALPACA: FakeAdapter(Broker.ALPACA),
    }
    native = FakeNativeResolver()
    baseline = await db_session.scalar(
        select(func.count()).select_from(PaperRunOrderLink)
    )
    runner = PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        verifier=verifier,
        application_factory=_app_factory(adapters),
        native_resolver=native,
    )

    with pytest.raises(PaperCohortError) as exc_info:
        await runner.run(
            CohortRunInvocation(
                cohort_id=activation.cohort_id,
                run_id=f"run-{nonce}",
                round_decision_id=f"round-{nonce}",
                mode=RunMode.PAPER_ACTIVE,
            )
        )
    assert exc_info.value.reason_code == "provenance_verification_failed"
    assert adapters[Broker.BINANCE].calls == []
    assert adapters[Broker.ALPACA].calls == []
    assert native.calls == []
    assert (
        await db_session.scalar(select(func.count()).select_from(PaperRunOrderLink))
        == baseline
    )
