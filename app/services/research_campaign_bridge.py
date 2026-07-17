"""ROB-946 (H6) — app-side campaign registration + hardened trial bridge.

The generic bridge between an injected 24-experiment campaign identity (built
by the pure ``research/nautilus_scalping/rob946_campaign_identity.py`` module
or any other caller) and the ROB-846 immutable registry
(``app.services.strategy_experiment_registry``). Owns exactly two write
surfaces:

  * ``register_campaign_experiments`` — registers all 24 experiments.
  * ``record_attempt`` — hardened, idempotent recording of one logical
    attempt's complete terminal evidence.

Both require the ROB-946 two-gate write guard
(``app.services.research_db_write_guard``) to pass FIRST, before any registry
call or spec/shape inspection.

Idempotency hardening (ROB-946 §6): the raw ROB-846 ``record_trial`` returns
the ORIGINAL row on ANY replay of a matching idempotency key, even if the
incoming payload differs — it does not itself detect divergence. This module
computes a canonical fingerprint of ALL terminal evidence (status/reason/fold
hash/artifact hashes per scenario/run identity) and compares it against the
stored fingerprint before ever calling the raw registry function: identical
evidence replays the original row; any mismatch fails closed
(``TerminalEvidenceMismatch``) and the raw ``record_trial`` (whose own
idempotency would otherwise silently mask the divergence) is never invoked in
that case.

Boundary (ROB-946 §7): no broker/order/fill/execution-ledger/scheduler/
ROB-905 import — see the extended
``tests/services/research/test_no_broker_import_guard.py``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_backtest import (
    TRIAL_STATUSES,
    ResearchBacktestRun,
    ResearchStrategyExperiment,
)
from app.schemas.research_backtest import (
    BacktestTrialRequest,
    StrategyExperimentIdentity,
)
from app.schemas.research_campaign_bridge import (
    AttemptEvidence,
    CampaignCompletenessReport,
)
from app.services import strategy_experiment_registry as registry
from app.services.research_canonical_hash import canonical_sha256
from app.services.research_db_write_guard import (
    ResearchDbTarget,
    assert_research_write_authorized,
    resolve_research_db_target,
)

__all__ = [
    "CampaignBridgeError",
    "CampaignSpecCountError",
    "RunnerNameTooLongError",
    "TerminalEvidenceMismatch",
    "campaign_completeness_report",
    "record_attempt",
    "register_campaign_experiments",
    "terminal_evidence_fingerprint",
]

_EXPECTED_CAMPAIGN_SIZE = 24
# research.backtest_runs.runner is VARCHAR(16) — reject before the DB does.
_MAX_RUNNER_LENGTH = 16


class CampaignBridgeError(Exception):
    """Base error for the ROB-946 campaign bridge."""


class CampaignSpecCountError(CampaignBridgeError):
    """A registration or completeness call was made with != 24 items."""


class RunnerNameTooLongError(CampaignBridgeError):
    """``runner`` exceeds the DB column's 16-character limit."""


class TerminalEvidenceMismatch(CampaignBridgeError):
    """A replay under the same attempt key carries DIFFERENT terminal
    evidence than the stored trial — fail closed, never silently replayed or
    duplicated."""


def terminal_evidence_fingerprint(evidence: AttemptEvidence) -> str:
    """Canonical fingerprint of ALL terminal evidence for one attempt.

    Two calls with the SAME semantic evidence (status/reason/fold hash/run
    identity/per-scenario trade_count+artifact_hash) always fingerprint
    identically regardless of scenario list order; any divergence in any of
    those fields changes the fingerprint.
    """
    payload = {
        "status": evidence.status,
        "reason_code": evidence.reason_code,
        "fold_evidence_hash": evidence.fold_evidence_hash,
        "run_identity": evidence.run_identity,
        "scenario_evidence": sorted(
            (
                {
                    "scenario_name": s.scenario_name,
                    "trade_count": s.trade_count,
                    "artifact_hash": s.artifact_hash,
                }
                for s in evidence.scenario_evidence
            ),
            key=lambda d: d["scenario_name"],
        ),
    }
    return canonical_sha256(payload)


async def _get_experiment_by_id(
    session: AsyncSession, experiment_id: str
) -> ResearchStrategyExperiment | None:
    return await session.scalar(
        select(ResearchStrategyExperiment).where(
            ResearchStrategyExperiment.experiment_id == experiment_id
        )
    )


async def register_campaign_experiments(
    session: AsyncSession,
    *,
    specs: list[StrategyExperimentIdentity],
    guard_opt_in_enabled: bool,
    guard_allowlist: frozenset[str],
) -> list[ResearchStrategyExperiment]:
    """Register all 24 campaign experiments via the ROB-846 registry.

    The write guard is evaluated FIRST — before the spec count is even
    inspected — so a disabled/unauthorized guard always wins over a malformed
    spec list. Registering anything other than exactly 24 specs is refused
    (ROB-946 §1: register all 24 before the empirical runner may start).
    """
    target: ResearchDbTarget = resolve_research_db_target(session)
    assert_research_write_authorized(
        opt_in_enabled=guard_opt_in_enabled, target=target, allowlist=guard_allowlist
    )
    if len(specs) != _EXPECTED_CAMPAIGN_SIZE:
        raise CampaignSpecCountError(
            f"expected exactly {_EXPECTED_CAMPAIGN_SIZE} campaign experiment "
            f"specs, got {len(specs)}"
        )
    registered = []
    for identity in specs:
        registered.append(await registry.register_experiment(session, identity))
    return registered


async def _find_trial_by_attempt_key(
    session: AsyncSession, *, experiment_pk: int, idempotency_key: str
) -> ResearchBacktestRun | None:
    return await session.scalar(
        select(ResearchBacktestRun).where(
            ResearchBacktestRun.strategy_experiment_id == experiment_pk,
            ResearchBacktestRun.trial_idempotency_key == idempotency_key,
        )
    )


def _scenario_evidence_payload(evidence: AttemptEvidence) -> list[dict]:
    return [
        {
            "scenario_name": s.scenario_name,
            "trade_count": s.trade_count,
            "artifact_hash": s.artifact_hash,
        }
        for s in evidence.scenario_evidence
    ]


async def record_attempt(
    session: AsyncSession,
    *,
    experiment_id: str,
    evidence: AttemptEvidence,
    strategy_name: str,
    timeframe: str,
    runner: str,
    guard_opt_in_enabled: bool,
    guard_allowlist: frozenset[str],
) -> ResearchBacktestRun:
    """Record one hardened, idempotent logical-attempt trial.

    * Same attempt key + IDENTICAL terminal evidence -> returns the original
      row (idempotent replay), never a second trial.
    * Same attempt key + ANY terminal evidence mismatch -> raises
      ``TerminalEvidenceMismatch`` fail-closed; the raw ``record_trial`` is
      never called in that branch.
    * A new attempt key (an explicit retry -> higher ``retry_index``) always
      records a new trial, consuming the next monotonic ``trial_index``.
    """
    target = resolve_research_db_target(session)
    assert_research_write_authorized(
        opt_in_enabled=guard_opt_in_enabled, target=target, allowlist=guard_allowlist
    )
    if len(runner) > _MAX_RUNNER_LENGTH:
        raise RunnerNameTooLongError(
            f"runner {runner!r} ({len(runner)} chars) exceeds the "
            f"{_MAX_RUNNER_LENGTH}-char DB column limit"
        )

    experiment = await _get_experiment_by_id(session, experiment_id)
    if experiment is None:
        raise registry.ExperimentNotFound(
            f"experiment_id {experiment_id!r} is not registered"
        )

    idempotency_key = evidence.attempt_key.idempotency_key()
    fingerprint = terminal_evidence_fingerprint(evidence)

    existing = await _find_trial_by_attempt_key(
        session, experiment_pk=experiment.id, idempotency_key=idempotency_key
    )
    if existing is not None:
        stored_fingerprint = (existing.raw_payload or {}).get("h6_evidence_fingerprint")
        if stored_fingerprint == fingerprint:
            return existing
        raise TerminalEvidenceMismatch(
            f"attempt {idempotency_key!r} was already recorded with different "
            "terminal evidence; refusing to overwrite, duplicate, or silently "
            "replay a stale row"
        )

    # A SHA-256 over the full (campaign_run_id, experiment_id, retry_index)
    # triple, not a truncated concatenation: the triple is already globally
    # unique by construction, and hashing it keeps run_id both a fixed,
    # comfortably-under-128-char length (independent of campaign_run_id's
    # length) and free of any truncation-collision risk (a 12-char slice of
    # experiment_id would only be *practically*, not *provably*, unique).
    run_id = "rob946-" + canonical_sha256(
        {
            "campaign_run_id": evidence.attempt_key.campaign_run_id,
            "experiment_id": experiment_id,
            "retry_index": evidence.attempt_key.retry_index,
        }
    )
    request = BacktestTrialRequest(
        status=evidence.status,
        strategy_name=strategy_name,
        timeframe=timeframe,
        runner=runner,
        run_id=run_id,
        idempotency_key=idempotency_key,
        raw_payload={
            "h6_evidence_fingerprint": fingerprint,
            "campaign_run_id": evidence.attempt_key.campaign_run_id,
            "retry_index": evidence.attempt_key.retry_index,
            "reason_code": evidence.reason_code,
            "fold_evidence_hash": evidence.fold_evidence_hash,
            "run_identity": evidence.run_identity,
            "scenario_evidence": _scenario_evidence_payload(evidence),
        },
    )
    return await registry.record_trial(
        session, experiment_id=experiment_id, request=request
    )


async def campaign_completeness_report(
    session: AsyncSession,
    *,
    campaign_run_id: str,
    expected_experiment_ids: list[str],
) -> CampaignCompletenessReport:
    """ROB-946 §9 — expected=24 vs actual terminal-attempt coverage.

    Refuses (fail-closed, before any query) an ``expected_experiment_ids``
    list that is not exactly 24 unique ids — a wrong denominator must never
    be silently reported as "incomplete". A retry (same experiment_id, higher
    ``retry_index``) is never confused with a duplicate: both attempts count
    toward ``status_counts``, but the experiment is counted once in
    ``experiments_with_attempts``.
    """
    if len(expected_experiment_ids) != _EXPECTED_CAMPAIGN_SIZE:
        raise CampaignSpecCountError(
            f"expected exactly {_EXPECTED_CAMPAIGN_SIZE} experiment_ids for a "
            f"campaign completeness report, got {len(expected_experiment_ids)}"
        )
    if len(set(expected_experiment_ids)) != len(expected_experiment_ids):
        raise CampaignBridgeError("expected_experiment_ids contains duplicates")

    status_counts: dict[str, int] = dict.fromkeys(TRIAL_STATUSES, 0)
    missing: list[str] = []
    duplicates: list[str] = []
    experiments_with_attempts = 0

    for experiment_id in expected_experiment_ids:
        experiment = await _get_experiment_by_id(session, experiment_id)
        if experiment is None:
            missing.append(experiment_id)
            continue

        trials = await registry.list_trials(session, experiment_id)
        prefix = f"{campaign_run_id}:{experiment_id}:"
        campaign_trials = [
            t
            for t in trials
            if t.trial_idempotency_key is not None
            and t.trial_idempotency_key.startswith(prefix)
        ]
        if not campaign_trials:
            missing.append(experiment_id)
            continue

        experiments_with_attempts += 1
        retry_indices = [
            t.trial_idempotency_key[len(prefix) :] for t in campaign_trials
        ]
        if len(set(retry_indices)) != len(retry_indices):
            duplicates.append(experiment_id)
        for t in campaign_trials:
            status_counts[t.trial_status] += 1

    verdict = "complete" if not missing and not duplicates else "incomplete"
    return CampaignCompletenessReport(
        campaign_run_id=campaign_run_id,
        expected_total=_EXPECTED_CAMPAIGN_SIZE,
        experiments_with_attempts=experiments_with_attempts,
        status_counts=status_counts,
        missing_experiment_ids=missing,
        duplicate_logical_attempts=duplicates,
        verdict=verdict,
    )
