from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_backtest import ResearchStrategyExperiment
from app.services.paper_validation.contracts import (
    ActorIdentity,
    ActorRole,
    FrozenInputStamp,
    PolicyStamp,
    ValidationIdentity,
)

HASH_FIELDS = (
    "strategy_hash",
    "code_hash",
    "params_hash",
    "dataset_manifest_hash",
    "universe_hash",
    "pit_hash",
    "frozen_config_hash",
    "policy_hash",
    "benchmark_hash",
    "cost_hash",
    "mdd_hash",
)


def stable_hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


@pytest_asyncio.fixture
async def paper_validation_experiment(
    db_session: AsyncSession,
) -> ResearchStrategyExperiment:
    nonce = uuid4().hex
    hashes = {name: stable_hash(f"{nonce}:{name}") for name in HASH_FIELDS}
    row = ResearchStrategyExperiment(
        experiment_id=stable_hash(f"{nonce}:experiment"),
        strategy_key=f"strategy-{nonce}",
        strategy_version="strategy-v1",
        manifest={},
        **hashes,
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest_asyncio.fixture
async def validation_identity(
    paper_validation_experiment: ResearchStrategyExperiment,
) -> ValidationIdentity:
    experiment = paper_validation_experiment
    return ValidationIdentity(
        validation_id=f"validation-{uuid4().hex}",
        validation_version=1,
        experiment_id=experiment.experiment_id,
        strategy_version_id=experiment.strategy_version,
        cohort_id="cohort-opaque-1",
        experiment_hash=experiment.experiment_id,
        cohort_hash=stable_hash("cohort-opaque-1"),
        strategy_hash=experiment.strategy_hash,
        config_hash=experiment.frozen_config_hash,
        policy_hash=experiment.policy_hash,
        input_hash=stable_hash("frozen-input-1"),
    )


@dataclass
class FakeActorRoleProvider:
    roles: dict[str, ActorRole]
    calls: list[str] = field(default_factory=list)
    error: Exception | None = None

    async def resolve(self, caller_id: str) -> ActorIdentity:
        self.calls.append(caller_id)
        if self.error is not None:
            raise self.error
        return ActorIdentity(actor_id=caller_id, role=self.roles[caller_id])


@dataclass
class FakeFrozenInputHashProvider:
    content_hash: str
    bundle_id: str = "bundle-1"
    calls: list[str] = field(default_factory=list)
    error: Exception | None = None

    async def get_stamp(self, identity: ValidationIdentity) -> FrozenInputStamp:
        self.calls.append(identity.validation_id)
        if self.error is not None:
            raise self.error
        return FrozenInputStamp(
            bundle_id=self.bundle_id,
            content_hash=self.content_hash,
            verified=True,
        )


@dataclass
class FakePolicyHashProvider:
    content_hash: str
    version: str = "policy-v1"
    calls: list[str] = field(default_factory=list)
    error: Exception | None = None

    async def get_stamp(self, identity: ValidationIdentity) -> PolicyStamp:
        self.calls.append(identity.validation_id)
        if self.error is not None:
            raise self.error
        return PolicyStamp(
            version=self.version,
            content_hash=self.content_hash,
            verified=True,
        )
