"""Deterministic, local-only replay counters for the context artifact boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.services.fill_watch_context.consumer import ContextArtifactConsumer
from app.services.fill_watch_context.contracts import ContextArtifact
from app.services.fill_watch_context.service import ConsumptionReceipt

__all__ = ["LocalReplayCounters", "LocalReplayHarness", "shadow_ready"]


def shadow_ready(*, elapsed: timedelta, distinct_economic_roots: int) -> bool:
    """The fixed readiness predicate; synthetic data is never eligible."""
    return elapsed >= timedelta(hours=48) and distinct_economic_roots >= 20


@dataclass(frozen=True)
class LocalReplayCounters:
    total_artifacts: int
    synthetic_artifacts: int
    observed_artifacts: int
    transport_duplicates: int
    transport_conflicts: int
    coalesced_roots: int
    distinct_economic_roots: int
    noise_outcomes: int
    unconsumed_outcomes: int
    latency_total_seconds: float
    readiness: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_artifacts": self.total_artifacts,
            "synthetic_artifacts": self.synthetic_artifacts,
            "observed_artifacts": self.observed_artifacts,
            "transport_duplicates": self.transport_duplicates,
            "transport_conflicts": self.transport_conflicts,
            "coalesced_roots": self.coalesced_roots,
            "distinct_economic_roots": self.distinct_economic_roots,
            "noise_outcomes": self.noise_outcomes,
            "unconsumed_outcomes": self.unconsumed_outcomes,
            "latency_total_seconds": self.latency_total_seconds,
            "readiness": self.readiness,
        }


class LocalReplayHarness:
    """Replay supplied artifacts without polling, scheduling, or deployment."""

    def __init__(self, consumer: ContextArtifactConsumer) -> None:
        self._consumer = consumer

    async def replay(
        self,
        raw_artifacts: list[dict[str, Any]],
        *,
        replay_finished_at: datetime,
        elapsed: timedelta,
    ) -> tuple[list[ConsumptionReceipt], LocalReplayCounters]:
        artifacts = [ContextArtifact.model_validate(raw) for raw in raw_artifacts]
        receipts = await self._consumer.consume_batch(raw_artifacts)
        roots: dict[str, set[str]] = {}
        observed_roots: set[str] = set()
        synthetic = 0
        observed = 0
        latency_total = 0.0
        for artifact, receipt in zip(artifacts, receipts, strict=True):
            context = artifact.context
            roots.setdefault(context.economic_root_ref, set()).add(
                receipt.outcome.transport_event_uuid
            )
            latency_total += max(
                0.0, (replay_finished_at - context.input_as_of).total_seconds()
            )
            if context.sample_origin == "synthetic":
                synthetic += 1
            else:
                observed += 1
                observed_roots.add(context.economic_root_ref)
        coalesced = sum(1 for event_ids in roots.values() if len(event_ids) > 1)
        distinct_roots = len(observed_roots)
        duplicates = sum(
            receipt.disposition.value == "duplicate" for receipt in receipts
        )
        conflicts = sum(receipt.disposition.value == "conflict" for receipt in receipts)
        noise = sum(
            receipt.outcome.contextual_status != "context_only_no_action"
            for receipt in receipts
        )
        unconsumed = sum(
            not receipt.as_dict()["delivery_ack"]["accepted"] for receipt in receipts
        )
        return receipts, LocalReplayCounters(
            total_artifacts=len(artifacts),
            synthetic_artifacts=synthetic,
            observed_artifacts=observed,
            transport_duplicates=duplicates,
            transport_conflicts=conflicts,
            coalesced_roots=coalesced,
            distinct_economic_roots=distinct_roots,
            noise_outcomes=noise,
            unconsumed_outcomes=unconsumed,
            latency_total_seconds=latency_total,
            readiness=shadow_ready(
                elapsed=elapsed, distinct_economic_roots=distinct_roots
            ),
        )
