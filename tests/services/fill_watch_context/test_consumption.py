"""Operating-path evidence for Phase 0 context-only UUID outcomes."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.db import AsyncSessionLocal
from app.models.fill_watch_context_outcome import FillWatchContextOutcome
from app.services.fill_watch_context.consumer import ContextArtifactConsumer
from app.services.fill_watch_context.contracts import ContextArtifact
from app.services.fill_watch_context.domain import (
    CONTEXTUAL_STATUS_VALUES,
)
from app.services.fill_watch_context.repository import FillWatchContextOutcomeRepository
from app.services.fill_watch_context.service import FillWatchContextOutcomeService
from app.services.fill_watch_context.shadow import LocalReplayHarness, shadow_ready
from app.services.fill_watch_context.toss_token_boundary import (
    CachedTokenUnavailable,
    read_cached_token,
)
from tests.services.fill_watch_context.conftest import (
    AS_OF,
    context_artifact,
    event_uuid,
)


def _consumer() -> ContextArtifactConsumer:
    return ContextArtifactConsumer(AsyncSessionLocal)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_producer_shaped_fill_and_watch_artifacts_persist_individual_outcomes() -> (
    None
):
    """The raw fixtures have no internal outcome vocabulary to monkeypatch."""
    consumer = _consumer()
    fill = context_artifact(13701, economic_root_ref="root:partial-full")
    watch = context_artifact(
        13702,
        event_kind="watch",
        economic_root_ref="root:watch-close",
        close_condition=True,
    )

    receipts = await consumer.consume_batch([fill, watch])

    assert [receipt.disposition.value for receipt in receipts] == [
        "persisted",
        "persisted",
    ]
    assert [receipt.outcome.contextual_status for receipt in receipts] == [
        "context_only_no_action",
        "context_only_no_action",
    ]
    assert receipts[0].outcome.reason == "context_recorded"
    assert receipts[1].outcome.reason == "close_condition_recorded"
    for receipt, raw in zip(receipts, (fill, watch), strict=True):
        assert receipt.outcome.transport_event_id == raw["lane_event"]["event_id"]
        assert receipt.outcome.economic_root_ref == raw["context"]["economic_root_ref"]
        assert receipt.as_dict()["delivery_ack"]["accepted"] is True
        assert receipt.as_dict()["delivery_ack"]["persisted"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_duplicate_partial_full_and_reorder_keep_uuid_and_root_axes_separate() -> (
    None
):
    consumer = _consumer()
    partial = context_artifact(
        13703,
        economic_root_ref="root:partial-full-reordered",
        order_refs=["order-ref-7"],
    )
    full = context_artifact(
        13704,
        economic_root_ref="root:partial-full-reordered",
        order_refs=["order-ref-7"],
    )

    receipts = await consumer.consume_batch([full, partial, full])
    async with AsyncSessionLocal() as session:
        service = FillWatchContextOutcomeService(
            FillWatchContextOutcomeRepository(session)
        )
        grouped = await service.outcomes_for_economic_root(
            "root:partial-full-reordered"
        )

    assert len(receipts) == 3  # Batch N retains a result mapping for every input.
    assert [receipt.disposition.value for receipt in receipts] == [
        "persisted",
        "persisted",
        "duplicate",
    ]
    assert {outcome.transport_event_uuid for outcome in grouped} == {
        event_uuid(13703),
        event_uuid(13704),
    }
    assert len(grouped) == 2  # Root grouping is not UUID dedupe.
    assert (
        next(
            outcome
            for outcome in grouped
            if outcome.transport_event_uuid == event_uuid(13704)
        ).delivery_count
        == 2
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_conflicting_replay_converges_to_one_queryable_needs_human_outcome() -> (
    None
):
    consumer = _consumer()
    original = context_artifact(13705, economic_root_ref="root:conflict-original")
    conflict = copy.deepcopy(original)
    conflict["context"]["economic_root_ref"] = "root:conflict-injected"

    first, second = await consumer.consume_batch([original, conflict])
    async with AsyncSessionLocal() as session:
        service = FillWatchContextOutcomeService(
            FillWatchContextOutcomeRepository(session)
        )
        persisted = await service.get(event_uuid(13705))

    assert first.disposition.value == "persisted"
    assert second.disposition.value == "conflict"
    assert persisted is not None
    assert persisted.contextual_status == "needs_human"
    assert persisted.reason == "event_uuid_conflict"
    assert persisted.next_action == "operator_review"
    assert persisted.economic_root_ref == "root:conflict-original"
    assert (persisted.delivery_count, persisted.conflict_count) == (2, 1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_context_statuses_are_derived_from_supplied_staleness_route_and_artifact_health() -> (
    None
):
    consumer = _consumer()
    stale_market = context_artifact(13706, market_freshness="stale")
    stale_position = context_artifact(13707, position_freshness="stale")
    unavailable_route = context_artifact(13708, route_availability="unavailable")
    failed_artifact = context_artifact(13709, artifact_health="failed")

    receipts = await consumer.consume_batch(
        [stale_market, stale_position, unavailable_route, failed_artifact]
    )

    assert [receipt.outcome.contextual_status for receipt in receipts] == [
        "stale_input",
        "stale_input",
        "needs_human",
        "failed_processing",
    ]
    assert [receipt.outcome.reason for receipt in receipts] == [
        "stale_market_snapshot",
        "stale_position_snapshot",
        "route_unavailable",
        "artifact_declared_failed",
    ]
    assert set(CONTEXTUAL_STATUS_VALUES) == {
        "context_only_no_action",
        "stale_input",
        "failed_processing",
        "needs_human",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restart_after_post_persist_crash_does_not_double_consume() -> None:
    consumer = _consumer()
    artifact = context_artifact(13710, economic_root_ref="root:crash")

    async def crash_after_persist(_receipt) -> None:
        raise RuntimeError("simulated artifact-process crash")

    with pytest.raises(RuntimeError, match="simulated"):
        await consumer.consume_raw(artifact, after_persist=crash_after_persist)
    restarted = await consumer.consume_raw(artifact)
    async with AsyncSessionLocal() as session:
        service = FillWatchContextOutcomeService(
            FillWatchContextOutcomeRepository(session)
        )
        outcome = await service.get(event_uuid(13710))

    assert restarted.disposition.value == "duplicate"
    assert outcome is not None
    assert outcome.delivery_count == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_failover_has_one_uuid_owner_and_one_logical_outcome() -> None:
    artifact = ContextArtifact.model_validate(
        context_artifact(13711, economic_root_ref="root:failover")
    )

    async def consume_on_successor() -> str:
        async with AsyncSessionLocal() as session:
            service = FillWatchContextOutcomeService(
                FillWatchContextOutcomeRepository(session)
            )
            return (await service.consume(artifact)).disposition.value

    dispositions = await asyncio.gather(
        consume_on_successor(),
        consume_on_successor(),
    )
    async with AsyncSessionLocal() as session:
        service = FillWatchContextOutcomeService(
            FillWatchContextOutcomeRepository(session)
        )
        outcome = await service.get(event_uuid(13711))

    assert sorted(dispositions) == ["duplicate", "persisted"]
    assert outcome is not None
    assert outcome.delivery_count == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_conflicting_replays_serialize_on_the_existing_uuid_row() -> (
    None
):
    original_raw = context_artifact(13717, economic_root_ref="root:race-original")
    conflict_raw = copy.deepcopy(original_raw)
    conflict_raw["context"]["economic_root_ref"] = "root:race-conflict"
    original = ContextArtifact.model_validate(original_raw)
    conflict = ContextArtifact.model_validate(conflict_raw)

    async def consume_on_successor(artifact: ContextArtifact) -> str:
        async with AsyncSessionLocal() as session:
            service = FillWatchContextOutcomeService(
                FillWatchContextOutcomeRepository(session)
            )
            return (await service.consume(artifact)).disposition.value

    dispositions = await asyncio.gather(
        consume_on_successor(original),
        consume_on_successor(conflict),
    )
    async with AsyncSessionLocal() as session:
        service = FillWatchContextOutcomeService(
            FillWatchContextOutcomeRepository(session)
        )
        outcome = await service.get(event_uuid(13717))

    assert sorted(dispositions) == ["conflict", "persisted"]
    assert outcome is not None
    assert outcome.contextual_status == "needs_human"
    assert (outcome.delivery_count, outcome.conflict_count) == (2, 1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deterministic_local_harness_counts_synthetic_replay_without_shadow_credit() -> (
    None
):
    consumer = _consumer()
    partial = context_artifact(13712, economic_root_ref="root:shadow")
    full = context_artifact(13713, economic_root_ref="root:shadow")
    stale = context_artifact(
        13714, economic_root_ref="root:stale", market_freshness="stale"
    )
    unavailable = context_artifact(
        13715,
        economic_root_ref="root:unavailable",
        route_availability="unavailable",
    )
    receipts, counters = await LocalReplayHarness(consumer).replay(
        [partial, full, partial, stale, unavailable],
        replay_finished_at=AS_OF + dt.timedelta(minutes=5),
        elapsed=dt.timedelta(minutes=5),
    )

    assert len(receipts) == 5
    assert counters.as_dict() == {
        "total_artifacts": 5,
        "synthetic_artifacts": 5,
        "observed_artifacts": 0,
        "transport_duplicates": 1,
        "transport_conflicts": 0,
        "coalesced_roots": 1,
        "distinct_economic_roots": 0,
        "noise_outcomes": 2,
        "unconsumed_outcomes": 0,
        "latency_total_seconds": 1500.0,
        "readiness": False,
    }
    assert shadow_ready(elapsed=dt.timedelta(hours=48), distinct_economic_roots=20)
    assert not shadow_ready(
        elapsed=dt.timedelta(hours=47, minutes=59), distinct_economic_roots=20
    )
    assert not shadow_ready(elapsed=dt.timedelta(hours=48), distinct_economic_roots=19)


def test_noncanonical_existing_fill_handoff_key_is_rejected_at_uuid_artifact_boundary() -> (
    None
):
    raw = context_artifact(13716)
    raw["lane_event"]["event_id"] = "execution_ledger:13716"

    with pytest.raises(ValidationError, match="canonical UUID"):
        ContextArtifact.model_validate(raw)


@pytest.mark.asyncio
async def test_read_only_token_provider_cannot_issue_or_force_reissue() -> None:
    class IssuerShapedStub:
        calls: list[str] = []

        async def get_cached_access_token(self) -> str | None:
            self.calls.append("cache")
            return "cached-token"

        async def issue(self) -> str:  # pragma: no cover - must stay untouched
            raise AssertionError("non-owner attempted issuance")

        async def force_reissue(self) -> str:  # pragma: no cover - must stay untouched
            raise AssertionError("non-owner attempted force reissue")

    provider = IssuerShapedStub()
    assert await read_cached_token(provider) == "cached-token"
    assert provider.calls == ["cache"]

    class EmptyCache:
        async def get_cached_access_token(self) -> str | None:
            return None

    with pytest.raises(CachedTokenUnavailable, match="read-only cached"):
        await read_cached_token(EmptyCache())


def test_shadow_replay_cli_checks_default_false_gate_before_artifact_read(
    monkeypatch, capsys, tmp_path
) -> None:
    """The local harness remains inert without its independent arming flag."""
    from app.core.config import settings
    from scripts.fill_watch_context_shadow_replay import _amain

    monkeypatch.setattr(settings, "FILL_WATCH_CONTEXT_EVENT_LOOP_ENABLED", False)
    absent = tmp_path / "never-read.json"

    assert (
        asyncio.run(
            _amain(
                Path(absent),
                finished_at=AS_OF,
                elapsed_seconds=0,
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "real_shadow": "NOT_STARTED",
        "reason": "FILL_WATCH_CONTEXT_EVENT_LOOP_ENABLED is false",
        "status": "disabled",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_bootstrap_includes_only_the_new_context_outcome_table(
    db_session,
) -> None:
    """Smoke the model path that repository test bootstrap uses, not Alembic."""
    connection = await db_session.connection()

    def tables(sync_connection) -> set[str]:
        import sqlalchemy as sa

        return set(sa.inspect(sync_connection).get_table_names(schema="review"))

    assert "fill_watch_context_outcomes" in await connection.run_sync(tables)
    assert FillWatchContextOutcome.__table__.schema == "review"
