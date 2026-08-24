from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.strategy_learning_event import ResearchStrategyLearningEvent
from app.schemas.research_backtest import StrategyExperimentIdentity
from app.schemas.strategy_learning_event import StrategyLearningEventRequest
from app.services import strategy_experiment_registry as registry
from app.services import strategy_learning_event_service as memory
from app.services.research_canonical_hash import (
    canonical_ast_json,
    encode_canonical,
)
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = pytest.mark.integration


def _learning_payload(*, observed: str = "the claim failed its frozen gate") -> dict:
    return {
        "tested_claim": "liquid-market reversal survives realistic costs",
        "observed": observed,
        "falsified_claims": ["net edge survives taker costs"],
        "preserved_claims": ["gross signal direction remains positive"],
        "next_question": "does a longer horizon reduce turnover?",
        "allowed_change_axis": "horizon",
        "prohibited_changes": ["threshold_relaxation", "sealed_oos_retune"],
        "stop_rule": "retire after the registered robustness gate fails",
        "schema_version": "1",
    }


def _request(
    *,
    key: str | None = None,
    experiment_id: str | None = None,
    market: str = "crypto",
    horizon: str = "5m",
    failure_class: str = "cost_turnover",
    observed: str = "the claim failed its frozen gate",
) -> StrategyLearningEventRequest:
    return StrategyLearningEventRequest(
        experiment_id=experiment_id,
        stage="offline",
        verdict="iterate",
        failure_class=failure_class,
        reason_codes=[
            "gross_positive",
            "net_negative",
            "turnover_cost_dominated",
        ],
        evidence_refs=["trial:" + "a1" * 32, "artifact:" + "b2" * 32],
        failure_fingerprint={
            "market": market,
            "horizon": horizon,
            "mechanism": "reversal",
            "threshold": -0.0,
        },
        learning_payload=_learning_payload(observed=observed),
        idempotency_key=key or f"rob1115-{uuid4().hex}",
        actor_id="research-worker",
        actor_role="system",
    )


def _identity(
    *,
    strategy_key: str,
    version: str,
    supersedes: str | None = None,
) -> StrategyExperimentIdentity:
    return StrategyExperimentIdentity(
        strategy_key=strategy_key,
        strategy_version=version,
        hypothesis=f"hypothesis-{version}",
        strategy={"name": strategy_key, "version": version},
        code={"commit": version},
        params={"lookback": 10 if version == "v1" else 20},
        dataset_manifest={"dataset": "fixture"},
        universe=["BTCUSDT"],
        pit={"cutoff": "2026-07-01"},
        frozen_config={"version": version},
        policy={"name": "honest-gate"},
        benchmark={"name": "flat"},
        cost={"bps": 8},
        mdd={"max": "0.20"},
        supersedes_experiment_id=supersedes,
    )


def _invalid_db_copy(
    row: ResearchStrategyLearningEvent,
    *,
    memory_event_id: str,
    idempotency_key: str,
) -> ResearchStrategyLearningEvent:
    return ResearchStrategyLearningEvent(
        memory_event_id=memory_event_id,
        experiment_id=row.experiment_id,
        stage=row.stage,
        verdict=row.verdict,
        failure_class=row.failure_class,
        reason_codes=list(row.reason_codes),
        evidence_refs=list(row.evidence_refs),
        failure_fingerprint=list(row.failure_fingerprint),
        learning_payload=list(row.learning_payload),
        idempotency_key=idempotency_key,
        request_hash=row.request_hash,
        actor_id=row.actor_id,
        actor_role=row.actor_role,
    )


def test_request_rejects_empty_reason_codes_and_invalid_enums() -> None:
    payload = _request().model_dump(mode="python")
    payload["reason_codes"] = []
    with pytest.raises(ValidationError, match="reason_codes"):
        StrategyLearningEventRequest.model_validate(payload)

    payload = _request().model_dump(mode="python")
    payload["stage"] = "training"
    with pytest.raises(ValidationError, match="stage"):
        StrategyLearningEventRequest.model_validate(payload)

    payload = _request().model_dump(mode="python")
    payload["failure_class"] = "fee"
    with pytest.raises(ValidationError, match="failure_class"):
        StrategyLearningEventRequest.model_validate(payload)


def test_evidence_refs_reject_inline_payloads() -> None:
    payload = _request().model_dump(mode="python")
    payload["evidence_refs"] = ['{"pnl":[1,2,3]}']
    with pytest.raises(ValidationError, match="evidence_refs"):
        StrategyLearningEventRequest.model_validate(payload)


def test_learning_payload_minimum_contract_accepts_structured_claims() -> None:
    payload = _request().model_dump(mode="python")
    payload["learning_payload"].update(
        {
            "tested_claim": {"claim_id": "C1", "summary": "edge survives costs"},
            "observed": {"summary": "net negative", "evidence_ref_count": 2},
            "next_question": ["change horizon only"],
            "stop_rule": {"gate": "robustness", "max_retries": 0},
            "schema_version": 1,
        }
    )
    request = StrategyLearningEventRequest.model_validate(payload)
    assert request.learning_payload.schema_version == 1


def test_fingerprint_and_event_hashes_are_deterministic_rob846_ast() -> None:
    request = _request(key="deterministic-key")
    reordered = request.model_dump(mode="python")
    reordered["failure_fingerprint"] = {
        "threshold": -0.0,
        "mechanism": "reversal",
        "horizon": "5m",
        "market": "crypto",
    }
    second = StrategyLearningEventRequest.model_validate(reordered)

    assert memory.compute_learning_event_request_hash(request) == (
        memory.compute_learning_event_request_hash(second)
    )
    first_id = memory.derive_memory_event_id(
        idempotency_key=request.idempotency_key,
        request_hash=memory.compute_learning_event_request_hash(request),
    )
    second_id = memory.derive_memory_event_id(
        idempotency_key=second.idempotency_key,
        request_hash=memory.compute_learning_event_request_hash(second),
    )
    assert first_id == second_id
    assert encode_canonical(-0.0) == ["float", "-0x0.0p+0"]


@pytest.mark.asyncio
async def test_unregistered_event_is_writable_and_full_arrays_are_preserved(
    db_session: AsyncSession,
) -> None:
    request = _request()
    row = await memory.record_learning_event(db_session, request)
    await db_session.flush()

    assert row.experiment_id is None
    assert row.reason_codes == request.reason_codes
    assert row.evidence_refs == request.evidence_refs
    assert row.failure_fingerprint == encode_canonical(
        request.failure_fingerprint.model_dump(mode="python")
    )

    record = memory.to_learning_event_record(row)
    assert record.reason_codes == request.reason_codes
    assert record.evidence_refs == request.evidence_refs
    assert record.failure_fingerprint.threshold == -0.0


@pytest.mark.asyncio
async def test_non_null_unknown_experiment_fails_closed(
    db_session: AsyncSession,
) -> None:
    unknown = "f0" * 32
    with pytest.raises(memory.LearningEventExperimentNotFound):
        await memory.record_learning_event(
            db_session,
            _request(experiment_id=unknown),
        )


@pytest.mark.asyncio
async def test_fk_points_to_experiment_id_and_nullable_bridge_is_real(
    db_session: AsyncSession,
) -> None:
    fk = (
        await db_session.execute(
            text(
                """
                SELECT kcu.column_name, ccu.table_schema, ccu.table_name,
                       ccu.column_name
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                   AND tc.constraint_schema = kcu.constraint_schema
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                   AND tc.constraint_schema = ccu.constraint_schema
                 WHERE tc.constraint_name = 'fk_strategy_learning_event_experiment'
                """
            )
        )
    ).one()
    assert tuple(fk) == (
        "experiment_id",
        "research",
        "strategy_experiments",
        "experiment_id",
    )
    nullable = await db_session.scalar(
        text(
            """
            SELECT is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'research'
               AND table_name = 'strategy_learning_events'
               AND column_name = 'experiment_id'
            """
        )
    )
    assert nullable == "YES"


@pytest.mark.asyncio
async def test_database_fk_rejects_nonexistent_parent(
    db_session: AsyncSession,
) -> None:
    request = _request(experiment_id=None)
    request_hash = memory.compute_learning_event_request_hash(request)
    event_id = memory.derive_memory_event_id(
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
    )
    row = ResearchStrategyLearningEvent(
        memory_event_id=event_id,
        experiment_id="e1" * 32,
        stage=request.stage,
        verdict=request.verdict,
        failure_class=request.failure_class,
        reason_codes=request.reason_codes,
        evidence_refs=request.evidence_refs,
        failure_fingerprint=encode_canonical(
            request.failure_fingerprint.model_dump(mode="python")
        ),
        learning_payload=encode_canonical(
            request.learning_payload.model_dump(mode="python")
        ),
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
        actor_id=request.actor_id,
        actor_role=request.actor_role,
    )
    with pytest.raises(IntegrityError, match="fk_strategy_learning_event_experiment"):
        async with db_session.begin_nested():
            db_session.add(row)
            await db_session.flush()


@pytest.mark.asyncio
async def test_memory_event_and_idempotency_uniques_are_database_enforced(
    db_session: AsyncSession,
) -> None:
    original = await memory.record_learning_event(db_session, _request())
    await db_session.flush()

    duplicate_event_id = _invalid_db_copy(
        original,
        memory_event_id=original.memory_event_id,
        idempotency_key=f"other-key-{uuid4().hex}",
    )
    with pytest.raises(
        IntegrityError, match="uq_strategy_learning_event_memory_event_id"
    ):
        async with db_session.begin_nested():
            db_session.add(duplicate_event_id)
            await db_session.flush()

    duplicate_idempotency = _invalid_db_copy(
        original,
        memory_event_id="f1" * 32,
        idempotency_key=original.idempotency_key,
    )
    with pytest.raises(
        IntegrityError, match="uq_strategy_learning_event_idempotency_key"
    ):
        async with db_session.begin_nested():
            db_session.add(duplicate_idempotency)
            await db_session.flush()


@pytest.mark.asyncio
async def test_idempotent_replay_and_request_hash_conflict(
    db_session: AsyncSession,
) -> None:
    request = _request()
    first = await memory.record_learning_event(db_session, request)
    replay = await memory.record_learning_event(db_session, request)
    assert replay.id == first.id
    assert replay.memory_event_id == first.memory_event_id

    changed = request.model_copy(
        update={
            "learning_payload": request.learning_payload.model_copy(
                update={"observed": "different semantic observation"}
            )
        }
    )
    with pytest.raises(memory.LearningEventIdempotencyConflict):
        await memory.record_learning_event(db_session, changed)


@pytest.mark.asyncio
async def test_concurrent_idempotency_race_creates_one_row() -> None:
    from app.core.db import engine

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    request = _request(key=f"race-{uuid4().hex}")

    async def worker() -> str:
        async with session_factory() as session:
            row = await memory.record_learning_event(session, request)
            event_id = row.memory_event_id
            await session.commit()
            return event_id

    left, right = await asyncio.gather(worker(), worker())
    assert left == right
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ResearchStrategyLearningEvent)
            .where(
                ResearchStrategyLearningEvent.idempotency_key == request.idempotency_key
            )
        )
    assert count == 1


@pytest.mark.asyncio
async def test_database_checks_reject_invalid_enum_and_empty_reasons(
    db_session: AsyncSession,
) -> None:
    valid = await memory.record_learning_event(db_session, _request())
    await db_session.flush()

    bad_stage = _invalid_db_copy(
        valid,
        memory_event_id="c1" * 32,
        idempotency_key=f"bad-stage-{uuid4().hex}",
    )
    bad_stage.stage = "training"
    with pytest.raises(IntegrityError, match="ck_strategy_learning_event_stage"):
        async with db_session.begin_nested():
            db_session.add(bad_stage)
            await db_session.flush()

    bad_reasons = _invalid_db_copy(
        valid,
        memory_event_id="c2" * 32,
        idempotency_key=f"bad-reasons-{uuid4().hex}",
    )
    bad_reasons.reason_codes = []
    with pytest.raises(IntegrityError, match="ck_strategy_learning_event_reason_codes"):
        async with db_session.begin_nested():
            db_session.add(bad_reasons)
            await db_session.flush()


@pytest.mark.asyncio
async def test_update_delete_and_truncate_are_rejected(
    db_session: AsyncSession,
) -> None:
    row = await memory.record_learning_event(db_session, _request())
    row_id = row.id
    await db_session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            update(ResearchStrategyLearningEvent)
            .where(ResearchStrategyLearningEvent.id == row_id)
            .values(actor_role="operator")
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            delete(ResearchStrategyLearningEvent).where(
                ResearchStrategyLearningEvent.id == row_id
            )
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text("TRUNCATE TABLE research.strategy_learning_events")
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_get_memory_and_lineage_are_append_sorted(
    db_session: AsyncSession,
) -> None:
    strategy_key = f"strategy-{uuid4().hex}"
    parent = await registry.register_experiment(
        db_session,
        _identity(strategy_key=strategy_key, version="v1"),
    )
    child = await registry.register_experiment(
        db_session,
        _identity(
            strategy_key=strategy_key,
            version="v2",
            supersedes=parent.experiment_id,
        ),
    )
    first = await memory.record_learning_event(
        db_session,
        _request(experiment_id=parent.experiment_id),
    )
    second = await memory.record_learning_event(
        db_session,
        _request(experiment_id=parent.experiment_id),
    )
    third = await memory.record_learning_event(
        db_session,
        _request(experiment_id=child.experiment_id),
    )
    await db_session.flush()

    parent_memory = await memory.get_memory(db_session, parent.experiment_id)
    assert [item.memory_event_id for item in parent_memory] == [
        first.memory_event_id,
        second.memory_event_id,
    ]
    lineage = await memory.get_lineage(db_session, strategy_key)
    assert [item.memory_event_id for item in lineage] == [
        first.memory_event_id,
        second.memory_event_id,
        third.memory_event_id,
    ]


@pytest.mark.asyncio
async def test_search_failures_filters_market_horizon_and_class(
    db_session: AsyncSession,
) -> None:
    matching = await memory.record_learning_event(
        db_session,
        _request(market="kr", horizon="1d", failure_class="robustness"),
    )
    await memory.record_learning_event(
        db_session,
        _request(market="us", horizon="1d", failure_class="robustness"),
    )
    await memory.record_learning_event(
        db_session,
        _request(market="kr", horizon="1h", failure_class="robustness"),
    )
    await memory.record_learning_event(
        db_session,
        _request(market="kr", horizon="1d", failure_class="risk"),
    )
    await db_session.flush()

    results = await memory.search_failures(
        db_session,
        market="kr",
        horizon="1d",
        failure_class="robustness",
    )
    assert [item.memory_event_id for item in results] == [matching.memory_event_id]


@pytest.mark.asyncio
async def test_persisted_fingerprint_ast_roundtrip_is_byte_deterministic(
    db_session: AsyncSession,
) -> None:
    request = _request()
    row = await memory.record_learning_event(db_session, request)
    await db_session.flush()
    await db_session.refresh(row)

    expected = encode_canonical(request.failure_fingerprint.model_dump(mode="python"))
    assert canonical_ast_json(row.failure_fingerprint) == canonical_ast_json(expected)
    assert memory.to_learning_event_record(row).memory_event_id == row.memory_event_id
