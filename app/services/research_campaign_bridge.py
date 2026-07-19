"""ROB-946 (H6) — app-side campaign registration + hardened trial bridge.

The generic bridge between an injected 24-experiment campaign identity (built
by the pure ``research/nautilus_scalping/rob946_campaign_identity.py`` module
or any other caller) and the ROB-846 immutable registry
(``app.services.strategy_experiment_registry``). Owns exactly two write
surfaces:

  * ``register_campaign_experiments`` — registers all 24 experiments.
  * ``record_attempt`` — hardened, idempotent recording of one logical
    attempt's complete terminal evidence.

Plus one read surface:

  * ``campaign_completeness_report`` — expected-vs-actual campaign coverage.

All writes require the ROB-946 two-gate write guard
(``app.services.research_db_write_guard``) to pass FIRST, before any registry
call or spec/shape inspection.

Idempotency hardening (ROB-946 §6, R1 Critical-2 remediation): the raw
ROB-846 ``record_trial`` returns the ORIGINAL row on ANY replay of a matching
idempotency key — on BOTH of its own internal paths (the pre-insert lookup
AND the post-IntegrityError re-read after losing a concurrent insert race) —
without ever comparing payloads. This module's own PRE-check (before calling
``record_trial``) closes the sequential-replay case, but a genuine race (this
caller's pre-check misses because a concurrent writer's row is not yet
visible to it, then ``record_trial`` itself resolves the DB-level conflict
and hands back the WINNER's row) is only closed by re-checking AFTER
delegating: the returned row's stored fingerprint is compared against this
call's computed fingerprint, and a mismatch raises ``TerminalEvidenceMismatch``
even when the raw registry call itself returned successfully with someone
else's row.

Boundary (ROB-946 §7): no broker/order/fill/execution-ledger/scheduler/
ROB-905 import — see the extended
``tests/services/research/test_no_broker_import_guard.py``.
"""

from __future__ import annotations

import json
import sys

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
from app.services.research_canonical_hash import (
    canonical_sha256,
    compute_identity_hashes,
    derive_experiment_id,
)
from app.services.research_db_write_guard import (
    ResearchDbPolicy,
    ResearchDbTarget,
    assert_research_write_authorized,
    resolve_research_db_target,
)

__all__ = [
    "CampaignBridgeError",
    "CampaignDuplicateSpecError",
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


class CampaignDuplicateSpecError(CampaignBridgeError):
    """Two or more of the (expected) 24 specs derive the SAME experiment_id —
    a duplicate identity masquerading as a distinct config slot. Raised
    BEFORE any write/query (R1 Minor-5): a caller must never be able to
    register/expect 24 specs where one silently replaces a missing one."""


class RunnerNameTooLongError(CampaignBridgeError):
    """``runner`` exceeds the DB column's 16-character limit."""


class TerminalEvidenceMismatch(CampaignBridgeError):
    """A replay under the same attempt key carries DIFFERENT terminal
    evidence than the stored trial — fail closed, never silently replayed or
    duplicated. Raised on EITHER the pre-check path (sequential replay) or
    the post-delegate path (a concurrent race whose winner row diverges)."""


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


def _derive_experiment_id(spec: StrategyExperimentIdentity) -> str:
    """Re-derive the canonical experiment_id from a spec's OWN components,
    using the exact same authority ``register_experiment`` does — never
    trust a caller-supplied experiment_id string directly."""
    hashes = compute_identity_hashes(spec.components())
    return derive_experiment_id(spec.strategy_key, spec.strategy_version, hashes)


def _assert_specs_derive_unique_experiment_ids(
    specs: list[StrategyExperimentIdentity],
) -> list[str]:
    """Fail closed if two+ specs derive the SAME experiment_id (R1 Minor-5:
    a duplicate identity silently replacing a missing 24th slot). Returns the
    derived ids in input order for reuse by the caller."""
    experiment_ids = [_derive_experiment_id(spec) for spec in specs]
    if len(set(experiment_ids)) != len(experiment_ids):
        duplicates = sorted(
            {eid for eid in experiment_ids if experiment_ids.count(eid) > 1}
        )
        raise CampaignDuplicateSpecError(
            f"expected {_EXPECTED_CAMPAIGN_SIZE} UNIQUE identities but found "
            f"duplicate derived experiment_id(s): {duplicates}"
        )
    return experiment_ids


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
    guard_policy: ResearchDbPolicy,
) -> list[ResearchStrategyExperiment]:
    """Register all 24 campaign experiments via the ROB-846 registry.

    The write guard is evaluated FIRST — before the spec count or uniqueness
    is even inspected — so a disabled/unauthorized guard always wins over a
    malformed spec list. Registering anything other than exactly 24 UNIQUE
    specs is refused before any write (ROB-946 §1 + R1 Minor-5: a duplicate
    identity must never silently stand in for a missing 24th slot).
    """
    target: ResearchDbTarget = resolve_research_db_target(session)
    assert_research_write_authorized(
        opt_in_enabled=guard_opt_in_enabled, target=target, policy=guard_policy
    )
    if len(specs) != _EXPECTED_CAMPAIGN_SIZE:
        raise CampaignSpecCountError(
            f"expected exactly {_EXPECTED_CAMPAIGN_SIZE} campaign experiment "
            f"specs, got {len(specs)}"
        )
    _assert_specs_derive_unique_experiment_ids(specs)

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


def _diagnostic_evidence_payload(evidence: AttemptEvidence) -> list[dict]:
    """ROB-970 (Q2, Fable-approved): additive, persistence-only child-
    failure evidence -- carried into ``raw_payload`` but deliberately NEVER
    referenced by ``terminal_evidence_fingerprint`` (see that function,
    unchanged by this addition)."""
    return [
        {
            "transport": d.transport,
            "stage": d.stage,
            "exception_type": d.exception_type,
            "message": d.message,
            "traceback_text": d.traceback_text,
            "stderr": d.stderr,
            "strategy": d.strategy,
            "config_id": d.config_id,
            "symbol": d.symbol,
            "fold_id": d.fold_id,
            "scenario_name": d.scenario_name,
            "signature": d.signature,
            "occurrence_count": d.occurrence_count,
            "truncated": d.truncated,
        }
        for d in evidence.diagnostic_evidence
    ]


def _diagnostic_overflow_payload(evidence: AttemptEvidence) -> dict:
    """ROB-970 R1 (Q1=A, cap=32): honest overflow accounting -- carried into
    ``raw_payload`` alongside ``diagnostic_evidence``, equally excluded from
    ``terminal_evidence_fingerprint``."""
    overflow = evidence.diagnostic_overflow
    return {
        "truncated": overflow.truncated,
        "omitted_distinct_signatures": overflow.omitted_distinct_signatures,
        "omitted_occurrences": overflow.omitted_occurrences,
    }


def _diagnostic_fingerprint(evidence: AttemptEvidence) -> str:
    """ROB-970 R1 (Q2=C-modified, Fable-approved
    ``orch-fable-answer-rob970-r1-20260719.md``): canonical fingerprint of
    JUST the sanitized/bounded diagnostic evidence + overflow metadata --
    deliberately separate from ``terminal_evidence_fingerprint`` (semantic
    identity). Used ONLY to detect replay divergence, never as a semantic
    hash/identity input."""
    return canonical_sha256(
        {
            "diagnostic_evidence": _diagnostic_evidence_payload(evidence),
            "diagnostic_overflow": _diagnostic_overflow_payload(evidence),
        }
    )


def _stored_fingerprint(row: ResearchBacktestRun) -> object:
    return (row.raw_payload or {}).get("h6_evidence_fingerprint")


def _stored_diagnostic_fingerprint(row: ResearchBacktestRun) -> object:
    return (row.raw_payload or {}).get("diagnostic_fingerprint")


def _emit_diagnostic_replay_divergence(
    *,
    idempotency_key: str,
    stored_diagnostic_fingerprint: object,
    new_diagnostic_fingerprint: str,
    stored_distinct_signature_count: int,
    new_distinct_signature_count: int,
) -> None:
    """ROB-970 R1 (Q2=C-modified): semantic-identical replay is, by
    determinism, expected to produce IDENTICAL diagnostics (same code, same
    input, same failure, same traceback) -- a divergence here is evidence of
    NONDETERMINISM, not a legitimate "late-arriving diagnostics" case.
    Never merged (would hide the nondeterminism) and never silently
    discarded (the R1 Important-2 bug) -- loudly surfaced instead. The
    original row is NEVER mutated (append-only trial-row invariance holds);
    this is observation-only, impossible to miss, sanitized (stable
    digests/counts/context only, never raw diagnostic text), and never a
    fail-stop (unlike ``TerminalEvidenceMismatch``, which remains reserved
    for genuine semantic-identity mismatches)."""
    payload = {
        "event": "diagnostic_replay_divergence",
        "idempotency_key": idempotency_key,
        "stored_diagnostic_fingerprint": stored_diagnostic_fingerprint,
        "new_diagnostic_fingerprint": new_diagnostic_fingerprint,
        "stored_distinct_signature_count": stored_distinct_signature_count,
        "new_distinct_signature_count": new_distinct_signature_count,
    }
    sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stderr.flush()


def _check_diagnostic_divergence(
    row: ResearchBacktestRun, evidence: AttemptEvidence, *, idempotency_key: str
) -> None:
    """Compare the REPLAY's diagnostic content against what is already
    durably stored for this attempt key. Byte-identical (canonical-
    serialized) diagnostics are a write-free no-op -- nothing is emitted.
    Any divergence emits the loud observation above; the caller's row is
    returned completely unchanged either way."""
    new_fingerprint = _diagnostic_fingerprint(evidence)
    stored_fingerprint = _stored_diagnostic_fingerprint(row)
    if stored_fingerprint == new_fingerprint:
        return
    stored_evidence = (row.raw_payload or {}).get("diagnostic_evidence") or []
    _emit_diagnostic_replay_divergence(
        idempotency_key=idempotency_key,
        stored_diagnostic_fingerprint=stored_fingerprint,
        new_diagnostic_fingerprint=new_fingerprint,
        stored_distinct_signature_count=len(stored_evidence),
        new_distinct_signature_count=len(evidence.diagnostic_evidence),
    )


async def record_attempt(
    session: AsyncSession,
    *,
    experiment_id: str,
    evidence: AttemptEvidence,
    strategy_name: str,
    timeframe: str,
    runner: str,
    guard_opt_in_enabled: bool,
    guard_policy: ResearchDbPolicy,
) -> ResearchBacktestRun:
    """Record one hardened, idempotent logical-attempt trial.

    * Same attempt key + IDENTICAL terminal evidence -> returns the original
      row (idempotent replay), never a second trial.
    * Same attempt key + ANY terminal evidence mismatch -> raises
      ``TerminalEvidenceMismatch`` fail-closed, checked on BOTH the pre-check
      path (sequential replay, this row already visible to us) AND the
      post-delegate path (R1 Critical-2: a concurrent race where our
      pre-check missed and the raw ``record_trial`` handed back someone
      else's already-committed winner row).
    * A new attempt key (an explicit retry -> higher ``retry_index``) always
      records a new trial, consuming the next monotonic ``trial_index``.
    """
    target = resolve_research_db_target(session)
    assert_research_write_authorized(
        opt_in_enabled=guard_opt_in_enabled, target=target, policy=guard_policy
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
        if _stored_fingerprint(existing) == fingerprint:
            # ROB-970 R1 (Q2=C-modified): semantic identity matches -- this
            # IS a legitimate replay. Diagnostics are additive/persistence-
            # only, so they are checked SEPARATELY: byte-identical is a
            # true write-free no-op; any divergence is surfaced loudly
            # (never merged, never silently discarded) while the original
            # row is returned completely untouched either way.
            _check_diagnostic_divergence(
                existing, evidence, idempotency_key=idempotency_key
            )
            return existing
        raise TerminalEvidenceMismatch(
            f"attempt {idempotency_key!r} was already recorded with different "
            "terminal evidence (pre-check); refusing to overwrite, duplicate, "
            "or silently replay a stale row"
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
            "diagnostic_evidence": _diagnostic_evidence_payload(evidence),
            "diagnostic_overflow": _diagnostic_overflow_payload(evidence),
            "diagnostic_fingerprint": _diagnostic_fingerprint(evidence),
        },
    )
    returned = await registry.record_trial(
        session, experiment_id=experiment_id, request=request
    )
    # R1 Critical-2: `record_trial` returns a WINNER row (not necessarily the
    # one we just built) on both of its own internal replay paths. Re-verify
    # after delegating: if the returned row's evidence differs from what we
    # asked to record, someone else's concurrently-committed attempt won the
    # race under our very own attempt key — fail closed rather than let the
    # caller believe its own evidence was recorded.
    if _stored_fingerprint(returned) != fingerprint:
        raise TerminalEvidenceMismatch(
            f"attempt {idempotency_key!r} was recorded concurrently by another "
            "writer with different terminal evidence (post-delegate); this "
            "call's evidence was NOT recorded — the existing row is unchanged"
        )
    # ROB-970 R1 (Q2=C-modified): the post-delegate winner row may not be the
    # one THIS call tried to insert (a concurrent race) -- same diagnostic
    # divergence check applies here too, never a fail-stop. When ``returned``
    # IS the row this very call just inserted, its stored diagnostic
    # fingerprint trivially equals this evidence's own, so the check is a
    # guaranteed no-op observation-wise.
    _check_diagnostic_divergence(returned, evidence, idempotency_key=idempotency_key)
    return returned


async def campaign_completeness_report(
    session: AsyncSession,
    *,
    campaign_run_id: str,
    expected_specs: list[StrategyExperimentIdentity],
) -> CampaignCompletenessReport:
    """ROB-946 §9 — expected=24 vs actual registration + terminal-attempt
    coverage (R1 Important-3/4 remediation).

    ``expected_specs`` are the 24 caller-asserted identities; this function
    NEVER trusts a bare experiment_id string — it re-derives each spec's
    canonical experiment_id via the SAME ``compute_identity_hashes`` /
    ``derive_experiment_id`` authority ``register_experiment`` uses, then
    diffs BOTH directions against what is actually registered under the
    expected specs' ``strategy_key``s:

    * an expected identity with no matching registered row, OR a registered
      row with no ``retry_index=0`` primary terminal attempt -> ``missing``;
    * a registered row (in scope) matching no expected identity -> ``extra``;
    * a registered row sharing an expected identity's ``params_hash`` (the
      SAME logical config slot) but under a DIFFERENT overall experiment_id
      (some other component drifted) -> ``mismatch``;
    * a non-contiguous (gapped) or raw-row-duplicated retry sequence for an
      otherwise-correctly-registered experiment -> ``duplicate_or_gap``.

    Refuses (fail-closed, before any query) anything other than exactly 24
    UNIQUE expected specs — a wrong denominator, or a duplicate expected
    identity, must never be silently reported as "incomplete" with a wrong
    basis.
    """
    if len(expected_specs) != _EXPECTED_CAMPAIGN_SIZE:
        raise CampaignSpecCountError(
            f"expected exactly {_EXPECTED_CAMPAIGN_SIZE} specs for a campaign "
            f"completeness report, got {len(expected_specs)}"
        )
    expected_experiment_ids = _assert_specs_derive_unique_experiment_ids(expected_specs)
    expected = list(
        zip(
            expected_specs,
            expected_experiment_ids,
            (
                compute_identity_hashes(spec.components())["params_hash"]
                for spec in expected_specs
            ),
            strict=True,
        )
    )

    strategy_keys = {spec.strategy_key for spec in expected_specs}
    actual_rows = list(
        (
            await session.execute(
                select(ResearchStrategyExperiment).where(
                    ResearchStrategyExperiment.strategy_key.in_(strategy_keys)
                )
            )
        ).scalars()
    )
    actual_by_id = {row.experiment_id: row for row in actual_rows}
    actual_by_params_hash: dict[str, list[ResearchStrategyExperiment]] = {}
    for row in actual_rows:
        actual_by_params_hash.setdefault(row.params_hash, []).append(row)

    claimed_actual_ids: set[str] = set()
    missing: set[str] = set()
    mismatch: set[str] = set()
    matched_registered: list[tuple[str, ResearchStrategyExperiment]] = []

    for _spec, expected_experiment_id, expected_params_hash in expected:
        row = actual_by_id.get(expected_experiment_id)
        if row is not None:
            claimed_actual_ids.add(row.experiment_id)
            matched_registered.append((expected_experiment_id, row))
            continue
        drifted_candidates = [
            candidate
            for candidate in actual_by_params_hash.get(expected_params_hash, [])
            if candidate.experiment_id != expected_experiment_id
        ]
        if drifted_candidates:
            mismatch.add(expected_experiment_id)
            claimed_actual_ids.update(
                candidate.experiment_id for candidate in drifted_candidates
            )
            continue
        missing.add(expected_experiment_id)

    extra = sorted(
        row.experiment_id
        for row in actual_rows
        if row.experiment_id not in claimed_actual_ids
    )

    status_counts: dict[str, int] = dict.fromkeys(TRIAL_STATUSES, 0)
    primary_attempts = 0
    total_attempts = 0
    duplicate_or_gap: set[str] = set()

    for expected_experiment_id, _row in matched_registered:
        trials = await registry.list_trials(session, expected_experiment_id)
        prefix = f"{campaign_run_id}:{expected_experiment_id}:"
        # Scan the RAW row list for a genuine duplicate BEFORE any dict/set
        # collapse (R1 Minor-6): under ROB-846's own
        # uq_research_backtest_runs_experiment_idempotency constraint (the
        # idempotency key embeds the retry index), two rows for the SAME
        # retry index cannot coexist for one experiment today — this check is
        # kept as defense-in-depth against that invariant weakening, not
        # decorative: it inspects `campaign_trials` directly, not a
        # pre-deduplicated view of it.
        campaign_trials = [
            t
            for t in trials
            if t.trial_idempotency_key is not None
            and t.trial_idempotency_key.startswith(prefix)
        ]
        if not campaign_trials:
            missing.add(expected_experiment_id)
            continue

        raw_suffixes = [t.trial_idempotency_key[len(prefix) :] for t in campaign_trials]
        if len(set(raw_suffixes)) != len(raw_suffixes):
            duplicate_or_gap.add(expected_experiment_id)
            continue
        try:
            retry_indices = sorted(int(suffix) for suffix in raw_suffixes)
        except ValueError:
            duplicate_or_gap.add(expected_experiment_id)
            continue

        if retry_indices[0] != 0:
            # A retry_index=1+ attempt exists but there is no primary
            # (retry_index=0) attempt — R1 Important-3: this is NOT complete
            # evidence, regardless of how many later retries exist.
            missing.add(expected_experiment_id)
            continue
        if retry_indices != list(range(len(retry_indices))):
            # Non-contiguous from 0 (e.g. 0 and 2 present, 1 missing) — a
            # genuinely reachable gap, distinct from the unreachable
            # same-index duplicate case above.
            duplicate_or_gap.add(expected_experiment_id)
            continue

        primary_attempts += 1
        total_attempts += len(campaign_trials)
        for t in campaign_trials:
            status_counts[t.trial_status] += 1

    retry_attempts = total_attempts - primary_attempts
    verdict = (
        "complete"
        if not (missing or extra or mismatch or duplicate_or_gap)
        else "incomplete"
    )
    return CampaignCompletenessReport(
        campaign_run_id=campaign_run_id,
        expected_total=_EXPECTED_CAMPAIGN_SIZE,
        actual_registrations=len(actual_rows),
        primary_attempts=primary_attempts,
        total_attempts=total_attempts,
        retry_attempts=retry_attempts,
        status_counts=status_counts,
        missing_experiment_ids=sorted(missing),
        extra_experiment_ids=extra,
        mismatch_experiment_ids=sorted(mismatch),
        duplicate_or_gap_experiment_ids=sorted(duplicate_or_gap),
        verdict=verdict,
    )
