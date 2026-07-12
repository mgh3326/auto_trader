"""ROB-846 — immutable strategy experiment registry + complete trial accounting.

This service is the *only* write path for the immutable registry. It is
deliberately append-only: it exposes registration + trial recording + read
helpers and NO update/delete methods. Corrections are a new lineage version
(``supersedes_experiment_id``), never an in-place edit. DB triggers enforce the
same immutability at the row level.

Boundary (ROB-846 AC#6): this module must never import or write a broker/order/
fill ledger. It touches only the ``research`` schema. The AST guard in
``tests/services/research/test_no_broker_import_guard.py`` enforces this.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_backtest import (
    TRIAL_STATUSES,
    ResearchBacktestRun,
    ResearchPromotionCandidate,
    ResearchStrategyExperiment,
)
from app.schemas.research_backtest import (
    BacktestTrialRequest,
    PromotionLinkRequest,
    StrategyExperimentIdentity,
    TrialAccounting,
)
from app.services.research_canonical_hash import (
    IDENTITY_COMPONENTS,
    canonical_ast_json,
    compute_identity_hashes,
    derive_experiment_id,
    encode_canonical,
    encode_manifest,
)

# Bounded retry for the monotonic trial_index allocation race. Two concurrent
# trials with *different* idempotency keys can compute the same next index; the
# UNIQUE(experiment, trial_index) constraint rejects the loser, which retries.
_MAX_INDEX_ALLOCATION_ATTEMPTS = 8


class StrategyExperimentRegistryError(Exception):
    """Base error for the immutable experiment registry."""


class ExperimentNotFound(StrategyExperimentRegistryError):
    """A trial/promotion referenced an experiment_id that is not registered."""


class SupersedesNotFound(StrategyExperimentRegistryError):
    """A registration supersedes an experiment_id that does not exist."""


class InvalidTrialStatus(StrategyExperimentRegistryError):
    """A trial was recorded with a status outside the terminal outcome set."""


class PromotionHashMismatch(StrategyExperimentRegistryError):
    """A promotion candidate referenced missing/mismatched experiment hashes."""


class CanonicalIdentityCollision(StrategyExperimentRegistryError):
    """An incoming identity derived an existing experiment_id but its canonical
    manifest differs from the stored one — an experiment_id collision. Registry
    refuses to replay the stored immutable row under a different identity."""


async def _get_experiment(
    session: AsyncSession, experiment_id: str
) -> ResearchStrategyExperiment | None:
    return await session.scalar(
        select(ResearchStrategyExperiment).where(
            ResearchStrategyExperiment.experiment_id == experiment_id
        )
    )


def _assert_same_identity(
    stored: ResearchStrategyExperiment,
    *,
    identity: StrategyExperimentIdentity,
    component_hashes: dict[str, str],
    incoming_manifest: dict[str, object],
) -> None:
    """Fail closed unless ``stored`` is the SAME identity as the incoming one.

    Guards every replay of an existing immutable row (both the initial
    existing-row lookup and the concurrent unique-conflict winner re-read).
    Compares the FULL identity — strategy key/version, the canonical manifest,
    every persisted component hash, AND the immutable provenance metadata
    (``hypothesis`` and ``supersedes_experiment_id``). The metadata does not feed
    the experiment_id hash, but it is immutable lineage truth (AC#3) that DB
    triggers forbid amending, so a replay under a different hypothesis or lineage
    must fail closed rather than silently return the original row.
    """
    mismatches: list[str] = []
    if stored.strategy_key != identity.strategy_key:
        mismatches.append("strategy_key")
    if stored.strategy_version != identity.strategy_version:
        mismatches.append("strategy_version")
    for name in IDENTITY_COMPONENTS:
        column = f"{name}_hash"
        if getattr(stored, column) != component_hashes[column]:
            mismatches.append(column)
    if canonical_ast_json(stored.manifest) != canonical_ast_json(incoming_manifest):
        mismatches.append("manifest")
    if stored.hypothesis != identity.hypothesis:
        mismatches.append("hypothesis")
    if stored.supersedes_experiment_id != identity.supersedes_experiment_id:
        mismatches.append("supersedes_experiment_id")
    if mismatches:
        raise CanonicalIdentityCollision(
            f"experiment_id {stored.experiment_id!r} already exists with a different "
            f"identity (mismatch: {', '.join(mismatches)}); refusing to replay the "
            "immutable row under a colliding identity"
        )


async def register_experiment(
    session: AsyncSession,
    identity: StrategyExperimentIdentity,
) -> ResearchStrategyExperiment:
    """Register (or return the existing) immutable strategy experiment.

    Idempotent by canonical identity: registering the same identity twice
    returns the original row without mutating it. A different identity (any
    component changed) is a new experiment_id — corrections link back via
    ``supersedes_experiment_id``.
    """
    component_hashes = compute_identity_hashes(identity.components())
    experiment_id = derive_experiment_id(
        identity.strategy_key,
        identity.strategy_version,
        component_hashes,
    )
    incoming_manifest = encode_manifest(identity.components())

    # Validate the supersedes parent BEFORE the existing fast path: an unknown
    # lineage parent must always fail closed (SupersedesNotFound), even when the
    # canonical components already resolved to an existing experiment_id.
    if identity.supersedes_experiment_id is not None:
        parent = await _get_experiment(session, identity.supersedes_experiment_id)
        if parent is None:
            raise SupersedesNotFound(
                f"supersedes_experiment_id {identity.supersedes_experiment_id!r} "
                "is not registered"
            )

    existing = await _get_experiment(session, experiment_id)
    if existing is not None:
        # Idempotent replay — but only for the SAME complete identity + metadata.
        _assert_same_identity(
            existing,
            identity=identity,
            component_hashes=component_hashes,
            incoming_manifest=incoming_manifest,
        )
        return existing

    # Persist the SAME typed canonical AST that was hashed, so a JSONB read-back
    # re-hashes (via hash_canonical_ast, without re-encoding) to the identical
    # identity — no double-encode of the persisted form.
    row = ResearchStrategyExperiment(
        experiment_id=experiment_id,
        strategy_key=identity.strategy_key,
        strategy_version=identity.strategy_version,
        hypothesis=identity.hypothesis,
        supersedes_experiment_id=identity.supersedes_experiment_id,
        benchmark_definition=encode_canonical(identity.benchmark),
        cost_definition=encode_canonical(identity.cost),
        mdd_definition=encode_canonical(identity.mdd),
        manifest=incoming_manifest,
        **component_hashes,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        # A concurrent registration won the unique-experiment_id race and
        # committed. Re-read the winner and apply the SAME full-identity check as
        # the initial path: only replay it when the identity matches exactly,
        # otherwise fail closed (a collision must not silently replay).
        if "experiment_id" not in _constraint_name(exc):
            raise
        winner = await _get_experiment(session, experiment_id)
        if winner is None:  # pragma: no cover - conflict without a visible row
            raise
        _assert_same_identity(
            winner,
            identity=identity,
            component_hashes=component_hashes,
            incoming_manifest=incoming_manifest,
        )
        return winner
    return row


def _build_trial_row(
    *,
    experiment_pk: int,
    experiment_id: str,
    trial_index: int,
    request: BacktestTrialRequest,
) -> ResearchBacktestRun:
    # A derived run_id carries a uuid suffix so two concurrent trials that
    # momentarily compute the same trial_index never collide on the run_id
    # unique index — the (experiment, trial_index) index is the real arbiter.
    run_id = (
        request.run_id or f"exp-{experiment_id[:12]}-t{trial_index}-{uuid4().hex[:8]}"
    )
    return ResearchBacktestRun(
        run_id=run_id,
        strategy_name=request.strategy_name,
        strategy_version=None,
        exchange=request.exchange,
        market=request.market,
        timeframe=request.timeframe,
        timerange=request.timerange,
        runner=request.runner,
        started_at=request.started_at,
        ended_at=request.ended_at,
        total_trades=request.total_trades,
        profit_factor=request.profit_factor or Decimal("0"),
        max_drawdown=request.max_drawdown or Decimal("0"),
        win_rate=request.win_rate,
        expectancy=request.expectancy,
        total_return=request.total_return,
        artifact_path=request.artifact_path,
        artifact_hash=request.artifact_hash,
        raw_payload=request.raw_payload,
        strategy_experiment_id=experiment_pk,
        trial_index=trial_index,
        seed=request.seed,
        information_cutoff=request.information_cutoff,
        trial_status=request.status,
        gate_artifact_hash=request.gate_artifact_hash,
        trial_idempotency_key=request.idempotency_key,
    )


async def _find_trial_by_idempotency(
    session: AsyncSession, experiment_pk: int, idempotency_key: str
) -> ResearchBacktestRun | None:
    return await session.scalar(
        select(ResearchBacktestRun).where(
            ResearchBacktestRun.strategy_experiment_id == experiment_pk,
            ResearchBacktestRun.trial_idempotency_key == idempotency_key,
        )
    )


async def record_trial(
    session: AsyncSession,
    *,
    experiment_id: str,
    request: BacktestTrialRequest,
) -> ResearchBacktestRun:
    """Append one trial (invocation) under an experiment.

    * Every terminal outcome (completed/rejected/crashed/timeout) records a
      trial and consumes exactly one monotonic ``trial_index``.
    * A duplicate ``idempotency_key`` returns the original row — no second
      trial, even under a concurrent race (guaranteed by the DB unique index).
    * Never updates an existing trial row (append-only).
    """
    if request.status not in TRIAL_STATUSES:
        raise InvalidTrialStatus(f"unknown trial status {request.status!r}")

    experiment = await _get_experiment(session, experiment_id)
    if experiment is None:
        raise ExperimentNotFound(f"experiment_id {experiment_id!r} is not registered")
    experiment_pk = experiment.id

    if request.idempotency_key is not None:
        replay = await _find_trial_by_idempotency(
            session, experiment_pk, request.idempotency_key
        )
        if replay is not None:
            return replay

    for _ in range(_MAX_INDEX_ALLOCATION_ATTEMPTS):
        next_index = await session.scalar(
            select(
                func.coalesce(func.max(ResearchBacktestRun.trial_index), 0) + 1
            ).where(ResearchBacktestRun.strategy_experiment_id == experiment_pk)
        )
        row = _build_trial_row(
            experiment_pk=experiment_pk,
            experiment_id=experiment_id,
            trial_index=int(next_index),
            request=request,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
            return row
        except IntegrityError as exc:
            constraint = _constraint_name(exc)
            if request.idempotency_key is not None:
                # A concurrent insert with the same idempotency key may have won
                # the race and committed; if so, return that original row.
                replay = await _find_trial_by_idempotency(
                    session, experiment_pk, request.idempotency_key
                )
                if replay is not None:
                    return replay
            if "trial_index" in constraint:
                # Another trial grabbed this index first — recompute and retry.
                continue
            raise

    raise StrategyExperimentRegistryError(
        "could not allocate a monotonic trial_index after "
        f"{_MAX_INDEX_ALLOCATION_ATTEMPTS} attempts"
    )


def _constraint_name(exc: IntegrityError) -> str:
    orig = getattr(exc, "orig", None)
    name = getattr(orig, "constraint_name", None)
    if name:
        return name
    # asyncpg may nest the driver error; fall back to the string form.
    return str(orig or exc)


async def list_trials(
    session: AsyncSession, experiment_id: str
) -> list[ResearchBacktestRun]:
    """All trials for an experiment in index order — no winner-only filter."""
    experiment = await _get_experiment(session, experiment_id)
    if experiment is None:
        raise ExperimentNotFound(f"experiment_id {experiment_id!r} is not registered")
    result = await session.execute(
        select(ResearchBacktestRun)
        .where(ResearchBacktestRun.strategy_experiment_id == experiment.id)
        .order_by(ResearchBacktestRun.trial_index)
    )
    return list(result.scalars().all())


async def get_trial_accounting(
    session: AsyncSession, experiment_id: str
) -> TrialAccounting:
    """Total trials + per-outcome counts (every status zero-filled)."""
    experiment = await _get_experiment(session, experiment_id)
    if experiment is None:
        raise ExperimentNotFound(f"experiment_id {experiment_id!r} is not registered")

    result = await session.execute(
        select(ResearchBacktestRun.trial_status, func.count())
        .where(ResearchBacktestRun.strategy_experiment_id == experiment.id)
        .group_by(ResearchBacktestRun.trial_status)
    )
    outcome_counts = dict.fromkeys(TRIAL_STATUSES, 0)
    total = 0
    for status, count in result.all():
        total += int(count)
        if status is not None:
            outcome_counts[status] = int(count)
    return TrialAccounting(
        experiment_id=experiment_id,
        total_trials=total,
        outcome_counts=outcome_counts,
    )


async def link_promotion_candidate(
    session: AsyncSession,
    *,
    backtest_run_id: int,
    request: PromotionLinkRequest,
) -> ResearchPromotionCandidate:
    """Create a promotion candidate bound to an EXACT run/config/data identity.

    Fails closed (``PromotionHashMismatch``) when the run has no experiment or
    when the experiment's experiment_id / frozen-config hash / dataset-manifest
    hash do not match the promoter's expected hashes.
    """
    run = await session.get(ResearchBacktestRun, backtest_run_id)
    if run is None:
        raise PromotionHashMismatch(f"backtest_run {backtest_run_id} does not exist")
    if run.strategy_experiment_id is None:
        raise PromotionHashMismatch(
            f"backtest_run {backtest_run_id} is not linked to an experiment"
        )

    experiment = await session.get(
        ResearchStrategyExperiment, run.strategy_experiment_id
    )
    if experiment is None:  # pragma: no cover - FK guarantees this
        raise PromotionHashMismatch(
            f"experiment for backtest_run {backtest_run_id} is missing"
        )

    mismatches: list[str] = []
    if experiment.experiment_id != request.expected_experiment_id:
        mismatches.append("experiment_id")
    if experiment.frozen_config_hash != request.expected_config_hash:
        mismatches.append("config_hash")
    if experiment.dataset_manifest_hash != request.expected_data_hash:
        mismatches.append("data_hash")
    if mismatches:
        raise PromotionHashMismatch(
            "promotion candidate hash mismatch on: " + ", ".join(mismatches)
        )

    candidate = ResearchPromotionCandidate(
        backtest_run_id=run.id,
        status=request.status,
        reason_code=request.reason_code,
        thresholds=request.thresholds,
        metrics=request.metrics,
        experiment_id=experiment.experiment_id,
        run_config_hash=experiment.frozen_config_hash,
        run_data_hash=experiment.dataset_manifest_hash,
    )
    session.add(candidate)
    await session.flush()
    return candidate
