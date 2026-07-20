"""ROB-981 (ROB-974 R2 H6-A) CP5 -- app-side exact-48 registration/attempt
batches and caller-owned transaction interface.

This module NEVER resolves a DB target, derives approval from a bool/env
value, creates/closes a session, or begins/commits/rolls back a transaction
of its own. It reuses the hardened ROB-846/946 24-row registration primitive
(``register_campaign_experiments``) for exact S3 and S4 identity slices
ONLY -- it never reuses the old S1/S2 ``AttemptEvidence``/``record_attempt``
attempt-recording path (that schema is hard-fixed to exactly the 3
``base|primary_stress|upward_stress`` scenario names, incompatible with
ROB-974's ``base13|primary_stress17|upward_stress22`` scenarios and 8-fold
dual evidence) -- attempt recording instead calls the lower-level
``strategy_experiment_registry.record_trial`` directly with a
ROB-974-specific ``raw_payload``.

Both public entry points accept the actual DB-touching primitives as
INJECTED callables with production defaults pointing at the real hardened
functions -- this is what lets a test prove zero-call/all-or-zero/
transaction-ownership behavior with pure spies, never a real DB session,
while production callers (H6-B) get the real reuse "for free" without any
extra wiring.

``ApprovedMutationContext`` is authorization-only (excluded from every
semantic hash) and can only be satisfied by an exact instance of this
module's own frozen dataclass, bound to the specific operation kind,
canonical plan hash, derived run ID, and exact-48 mapping hash being
mutated -- a bool, env opt-in, DB-name substring, caller-built mapping,
subclass, or any single mismatched field is refused before the first
``await``.

On a second-slice (S4) or later attempt-record failure, this module NEVER
catches the exception to roll back -- the original exception propagates
unchanged, and any transient flushed rows remain inside the CALLER's still
-open transaction. Only the caller's own outer rollback (H6-B) proves
durable residue zero.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_backtest import ResearchBacktestRun, ResearchStrategyExperiment
from app.schemas.research_backtest import (
    BacktestTrialRequest,
    StrategyExperimentIdentity,
)
from app.services import strategy_experiment_registry as registry
from app.services.research_campaign_bridge import register_campaign_experiments
from app.services.research_canonical_hash import (
    canonical_sha256,
    compute_identity_hashes,
    derive_experiment_id,
)
from app.services.research_db_write_guard import ResearchDbPolicy

__all__ = [
    "EXPECTED_SLICE_SIZE",
    "EXPECTED_TOTAL_ROWS",
    "REASON_ALLOWLIST_BY_STATUS",
    "RECORD_ATTEMPTS_OPERATION_KIND",
    "REGISTER_CAMPAIGN_OPERATION_KIND",
    "STATUSES",
    "ApprovalContextError",
    "ApprovedMutationContext",
    "BatchValidationError",
    "H6AAttemptBatchItem",
    "H6ABridgeError",
    "TerminalEvidenceMismatch",
    "compute_exact_48_mapping_hash",
    "record_h6a_attempts",
    "register_h6a_campaign",
]

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

EXPECTED_SLICE_SIZE = 24
EXPECTED_TOTAL_ROWS = 48
REGISTER_CAMPAIGN_OPERATION_KIND = "rob974_h6a_register_campaign"
RECORD_ATTEMPTS_OPERATION_KIND = "rob974_h6a_record_attempts"

STATUSES: tuple[str, ...] = ("completed", "rejected", "crashed", "timeout")
# Literal duplication of the CP3 closed reason taxonomy (this module never
# imports the pure research-side rob974_h6a_evidence module -- app/services
# stays DB-free-boundary-pure the same way rob944_campaign_controller's own
# literal duplication of ROB-944's reason codes does).
REASON_ALLOWLIST_BY_STATUS: dict[str, frozenset[str]] = {
    "completed": frozenset(),
    "rejected": frozenset(
        {
            "rejected:data_gap_in_position",
            "rejected:data_gap_in_pair_position",
            "insufficient_train_evidence_all_folds",
        }
    ),
    "crashed": frozenset({"child_execution_crashed", "global_corpus_load_failed"}),
    "timeout": frozenset({"child_execution_timeout"}),
}


class H6ABridgeError(Exception):
    """Base error for the ROB-981 H6-A app-side bridge."""


class ApprovalContextError(H6ABridgeError):
    """The injected ``ApprovedMutationContext`` does not authorize this
    exact operation/plan/run/mapping -- refused before any service call."""


class BatchValidationError(H6ABridgeError):
    """The registration or attempt batch does not exactly satisfy the
    combined exact-48 shape -- refused before any service call."""


class TerminalEvidenceMismatch(H6ABridgeError):
    """A replay under the same attempt key carries DIFFERENT terminal
    evidence than the stored trial -- fail closed, never silently replayed
    or duplicated."""


@dataclass(frozen=True)
class ApprovedMutationContext:
    """Authorization-only token bound to one exact mutation. Excluded from
    every semantic hash -- this is a capability assertion, never campaign
    content. Only an exact instance of THIS dataclass (never a subclass,
    a bool, or a caller-built mapping) can satisfy the checks below."""

    operation_kind: str
    canonical_plan_hash: str
    derived_run_id: str
    exact_48_mapping_hash: str
    approval_token: str

    def __post_init__(self) -> None:
        if type(self.operation_kind) is not str or not self.operation_kind:
            raise ApprovalContextError("operation_kind must be a non-empty str")
        if type(self.canonical_plan_hash) is not str or not _HEX64_RE.match(
            self.canonical_plan_hash
        ):
            raise ApprovalContextError(
                "canonical_plan_hash must be a lowercase 64-hex SHA-256 digest"
            )
        if type(self.derived_run_id) is not str or not self.derived_run_id:
            raise ApprovalContextError("derived_run_id must be a non-empty str")
        if type(self.exact_48_mapping_hash) is not str or not _HEX64_RE.match(
            self.exact_48_mapping_hash
        ):
            raise ApprovalContextError(
                "exact_48_mapping_hash must be a lowercase 64-hex SHA-256 digest"
            )
        if type(self.approval_token) is not str or not self.approval_token:
            raise ApprovalContextError("approval_token must be a non-empty opaque str")


def compute_exact_48_mapping_hash(row_id_to_experiment_id: Mapping[str, str]) -> str:
    """Canonical hash of the exact 48 row_id -> experiment_id mapping being
    mutated -- one of the four facts an ``ApprovedMutationContext`` binds
    to."""
    if len(row_id_to_experiment_id) != EXPECTED_TOTAL_ROWS:
        raise BatchValidationError(
            f"row_id_to_experiment_id must map exactly {EXPECTED_TOTAL_ROWS} rows, got "
            f"{len(row_id_to_experiment_id)}"
        )
    return canonical_sha256(dict(row_id_to_experiment_id))


def _require_approved(
    approved: ApprovedMutationContext,
    *,
    operation_kind: str,
    full_campaign_hash: str,
    campaign_run_id: str,
    mapping_hash: str,
) -> None:
    if type(approved) is not ApprovedMutationContext:
        raise ApprovalContextError(
            "approved must be an exact ApprovedMutationContext instance (no subclass, no "
            "bool/env/mapping stand-in)"
        )
    if approved.operation_kind != operation_kind:
        raise ApprovalContextError("approval was not issued for this exact operation")
    if approved.canonical_plan_hash != full_campaign_hash:
        raise ApprovalContextError(
            "approval does not match the full_campaign_hash being mutated"
        )
    if approved.derived_run_id != campaign_run_id:
        raise ApprovalContextError(
            "approval does not match the campaign_run_id being mutated"
        )
    if approved.exact_48_mapping_hash != mapping_hash:
        raise ApprovalContextError(
            "approval does not match the exact-48 row->experiment_id mapping being mutated"
        )


def _spec_row_id(spec: StrategyExperimentIdentity) -> str | None:
    if isinstance(spec.params, Mapping):
        row_id = spec.params.get("row_id")
        if type(row_id) is str:
            return row_id
    return None


def _preflight_specs(
    specs: Sequence[StrategyExperimentIdentity],
    *,
    expected_slug: str,
    row_id_to_experiment_id: Mapping[str, str],
) -> None:
    """Independently re-derive and cross-check EVERY spec's experiment_id
    against the trusted mapping BEFORE any service call -- a malformed
    first/middle/LAST entry means zero registration calls for either slice
    (this loop is pure computation, no I/O)."""
    if len(specs) != EXPECTED_SLICE_SIZE:
        raise BatchValidationError(
            f"expected exactly {EXPECTED_SLICE_SIZE} {expected_slug} specs, got {len(specs)}"
        )
    seen_row_ids: set[str] = set()
    for spec in specs:
        row_id = _spec_row_id(spec)
        if row_id is None or not row_id.startswith(f"{expected_slug}-"):
            raise BatchValidationError(
                f"a {expected_slug} spec is missing/foreign params['row_id']"
            )
        if row_id in seen_row_ids:
            raise BatchValidationError(
                f"duplicate row_id {row_id!r} within {expected_slug}"
            )
        seen_row_ids.add(row_id)
        expected_experiment_id = row_id_to_experiment_id.get(row_id)
        if expected_experiment_id is None:
            raise BatchValidationError(
                f"row_id {row_id!r} is not present in the trusted row_id_to_experiment_id "
                "mapping"
            )
        derived = derive_experiment_id(
            spec.strategy_key,
            spec.strategy_version,
            compute_identity_hashes(spec.components()),
        )
        if derived != expected_experiment_id:
            raise BatchValidationError(
                f"spec for row_id {row_id!r} derives an experiment_id that does not match "
                "the trusted mapping"
            )
    expected_row_ids = {
        rid for rid in row_id_to_experiment_id if rid.startswith(f"{expected_slug}-")
    }
    if seen_row_ids != expected_row_ids:
        raise BatchValidationError(
            f"{expected_slug} specs do not cover exactly its 24 expected row IDs"
        )


RegisterExperimentsFn = Callable[..., Awaitable[list[ResearchStrategyExperiment]]]


async def register_h6a_campaign(
    session: AsyncSession,
    *,
    approved: ApprovedMutationContext,
    full_campaign_hash: str,
    campaign_run_id: str,
    s3_specs: list[StrategyExperimentIdentity],
    s4_specs: list[StrategyExperimentIdentity],
    row_id_to_experiment_id: Mapping[str, str],
    guard_opt_in_enabled: bool,
    guard_policy: ResearchDbPolicy,
    register_experiments_fn: RegisterExperimentsFn = register_campaign_experiments,
) -> tuple[list[ResearchStrategyExperiment], list[ResearchStrategyExperiment]]:
    """Register the combined 48-row campaign as two 24-row calls into the
    hardened primitive, S3 then S4, inside the CALLER's already-open
    transaction. This function itself never begins/commits/rolls back/
    closes anything -- only ``session.add``/``session.flush`` happen deep
    inside the reused ``register_campaign_experiments`` -> ``registry.
    register_experiment`` primitive, and only after the FULL 48-preflight
    (approval + both 24-slice shape/identity checks) passes.

    If S3 succeeds and S4 raises, the exception propagates completely
    unchanged -- this function never catches it to roll back; any
    transiently flushed S3 rows remain inside the caller's still-open
    transaction until the CALLER'S OWN rollback (H6-B) removes them.
    """
    mapping_hash = compute_exact_48_mapping_hash(row_id_to_experiment_id)
    _require_approved(
        approved,
        operation_kind=REGISTER_CAMPAIGN_OPERATION_KIND,
        full_campaign_hash=full_campaign_hash,
        campaign_run_id=campaign_run_id,
        mapping_hash=mapping_hash,
    )
    _preflight_specs(
        s3_specs, expected_slug="S3", row_id_to_experiment_id=row_id_to_experiment_id
    )
    _preflight_specs(
        s4_specs, expected_slug="S4", row_id_to_experiment_id=row_id_to_experiment_id
    )

    registered_s3 = await register_experiments_fn(
        session,
        specs=s3_specs,
        guard_opt_in_enabled=guard_opt_in_enabled,
        guard_policy=guard_policy,
    )
    registered_s4 = await register_experiments_fn(
        session,
        specs=s4_specs,
        guard_opt_in_enabled=guard_opt_in_enabled,
        guard_policy=guard_policy,
    )
    return registered_s3, registered_s4


@dataclass(frozen=True)
class H6AAttemptBatchItem:
    """One attempt's persistence-boundary projection -- the app-side
    conversion target for a (research-side, pure) CP3 ``AttemptRecord``.
    Deliberately decoupled from that dataclass's exact shape so this module
    stays independently unit-testable with plain data."""

    row_id: str
    experiment_id: str
    retry_index: int
    status: str
    reason_code: str | None
    fold_evidence_hash: str
    run_identity: str
    evidence_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self.row_id) is not str or not self.row_id:
            raise BatchValidationError("row_id must be a non-empty str")
        if type(self.experiment_id) is not str or not _HEX64_RE.match(
            self.experiment_id
        ):
            raise BatchValidationError(
                "experiment_id must be a lowercase 64-hex digest"
            )
        if type(self.retry_index) is not int or self.retry_index < 0:
            raise BatchValidationError(
                "retry_index must be a non-negative built-in int"
            )
        if self.status not in STATUSES:
            raise BatchValidationError(f"status must be one of {STATUSES}")
        if self.status == "completed":
            if self.reason_code is not None:
                raise BatchValidationError("completed must carry reason_code=None")
        else:
            allowed = REASON_ALLOWLIST_BY_STATUS[self.status]
            if self.reason_code not in allowed:
                raise BatchValidationError(
                    f"reason_code {self.reason_code!r} is not permitted for status "
                    f"{self.status!r} under the closed allowlist"
                )
        if type(self.fold_evidence_hash) is not str or not _HEX64_RE.match(
            self.fold_evidence_hash
        ):
            raise BatchValidationError(
                "fold_evidence_hash must be a lowercase 64-hex digest"
            )
        if type(self.run_identity) is not str or not _HEX64_RE.match(self.run_identity):
            raise BatchValidationError("run_identity must be a lowercase 64-hex digest")
        if not isinstance(self.evidence_payload, Mapping):
            raise BatchValidationError("evidence_payload must be a mapping")

    def idempotency_key(self, campaign_run_id: str) -> str:
        return f"{campaign_run_id}:{self.experiment_id}:{self.retry_index}"

    def fingerprint(self) -> str:
        return canonical_sha256(
            {
                "status": self.status,
                "reason_code": self.reason_code,
                "fold_evidence_hash": self.fold_evidence_hash,
                "run_identity": self.run_identity,
                "evidence_payload": dict(self.evidence_payload),
            }
        )


def _preflight_attempts(
    attempts: Sequence[H6AAttemptBatchItem],
    *,
    row_id_to_experiment_id: Mapping[str, str],
) -> None:
    if len(attempts) != EXPECTED_TOTAL_ROWS:
        raise BatchValidationError(
            f"expected exactly {EXPECTED_TOTAL_ROWS} attempts, got {len(attempts)}"
        )
    seen_row_ids = [a.row_id for a in attempts]
    if len(set(seen_row_ids)) != EXPECTED_TOTAL_ROWS:
        raise BatchValidationError("duplicate row_id present in attempt batch")
    if set(seen_row_ids) != set(row_id_to_experiment_id):
        raise BatchValidationError(
            "attempt batch does not cover exactly the 48 canonical row IDs"
        )
    for attempt in attempts:
        # H6AAttemptBatchItem.__post_init__ already validated its own
        # status/reason/hash-format shape at CONSTRUCTION time -- this loop
        # cross-checks it against trusted campaign identity, never trusting
        # the item's own self-reported experiment_id at face value.
        expected_experiment_id = row_id_to_experiment_id[attempt.row_id]
        if attempt.experiment_id != expected_experiment_id:
            raise BatchValidationError(
                f"attempt for row_id {attempt.row_id!r} carries an experiment_id that does "
                "not match the trusted mapping"
            )


async def _default_find_existing_trial(
    session: AsyncSession, *, experiment_pk: int, idempotency_key: str
) -> ResearchBacktestRun | None:
    return await session.scalar(
        select(ResearchBacktestRun).where(
            ResearchBacktestRun.strategy_experiment_id == experiment_pk,
            ResearchBacktestRun.trial_idempotency_key == idempotency_key,
        )
    )


FindExistingTrialFn = Callable[..., Awaitable[ResearchBacktestRun | None]]
RecordTrialFn = Callable[..., Awaitable[ResearchBacktestRun]]


def _stored_fingerprint(row: ResearchBacktestRun) -> object:
    raw_payload = row.raw_payload
    if type(raw_payload) is not dict:
        return None
    return raw_payload.get("h6a_evidence_fingerprint")


async def record_h6a_attempts(
    session: AsyncSession,
    *,
    approved: ApprovedMutationContext,
    full_campaign_hash: str,
    campaign_run_id: str,
    row_id_to_experiment_id: Mapping[str, str],
    row_id_to_experiment_pk: Mapping[str, int],
    attempts: Sequence[H6AAttemptBatchItem],
    strategy_name: str,
    timeframe: str,
    runner: str,
    guard_opt_in_enabled: bool,
    guard_policy: ResearchDbPolicy,
    find_existing_trial_fn: FindExistingTrialFn = _default_find_existing_trial,
    record_trial_fn: RecordTrialFn = registry.record_trial,
) -> list[ResearchBacktestRun]:
    """Record the exact-48 attempt batch, in canonical row-id order, inside
    the caller's already-open transaction.

    * All 48 statuses/reasons/keys/evidence are validated -- BOTH at
      ``H6AAttemptBatchItem`` construction AND again here against the
      trusted mapping -- before the first ``await``.
    * Identical semantic replay (matching stored fingerprint) returns the
      original row untouched, no write.
    * Divergent replay (same idempotency key, different fingerprint) raises
      ``TerminalEvidenceMismatch`` fail-closed -- the stored row is never
      overwritten.
    * A first/middle/last recorder failure preserves the caller's
      transaction ownership and the original exception -- never caught,
      never rolled back here.
    """
    mapping_hash = compute_exact_48_mapping_hash(row_id_to_experiment_id)
    _require_approved(
        approved,
        operation_kind=RECORD_ATTEMPTS_OPERATION_KIND,
        full_campaign_hash=full_campaign_hash,
        campaign_run_id=campaign_run_id,
        mapping_hash=mapping_hash,
    )
    _preflight_attempts(attempts, row_id_to_experiment_id=row_id_to_experiment_id)
    if set(row_id_to_experiment_pk) != set(row_id_to_experiment_id):
        raise BatchValidationError(
            "row_id_to_experiment_pk must cover exactly the same 48 row IDs"
        )

    by_row_id = {a.row_id: a for a in attempts}
    results: list[ResearchBacktestRun] = []
    for row_id in sorted(row_id_to_experiment_id):
        item = by_row_id[row_id]
        experiment_pk = row_id_to_experiment_pk[row_id]
        idempotency_key = item.idempotency_key(campaign_run_id)
        fingerprint = item.fingerprint()

        existing = await find_existing_trial_fn(
            session, experiment_pk=experiment_pk, idempotency_key=idempotency_key
        )
        if existing is not None:
            if _stored_fingerprint(existing) == fingerprint:
                results.append(existing)
                continue
            raise TerminalEvidenceMismatch(
                f"attempt for row_id {row_id!r} was already recorded with different "
                "terminal evidence; refusing to overwrite, duplicate, or silently replay"
            )

        request = BacktestTrialRequest(
            status=item.status,
            strategy_name=strategy_name,
            timeframe=timeframe,
            runner=runner,
            idempotency_key=idempotency_key,
            raw_payload={
                "h6a_evidence_fingerprint": fingerprint,
                "campaign_run_id": campaign_run_id,
                "row_id": item.row_id,
                "retry_index": item.retry_index,
                "reason_code": item.reason_code,
                "fold_evidence_hash": item.fold_evidence_hash,
                "run_identity": item.run_identity,
                "evidence_payload": dict(item.evidence_payload),
            },
        )
        returned = await record_trial_fn(
            session, experiment_id=item.experiment_id, request=request
        )
        if _stored_fingerprint(returned) != fingerprint:
            raise TerminalEvidenceMismatch(
                f"attempt for row_id {row_id!r} was recorded concurrently by another writer "
                "with different terminal evidence; this call's evidence was NOT recorded"
            )
        results.append(returned)
    return results
