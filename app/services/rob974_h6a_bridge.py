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

import base64
import hashlib
import re
import sys
import types
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
    IDENTITY_COMPONENTS,
    canonical_json,
    canonical_sha256,
    compute_identity_hashes,
    derive_experiment_id,
)
from app.services.research_db_write_guard import ResearchDbPolicy
from research_contracts.diagnostic_evidence_policy import (
    MAX_DISTINCT_SIGNATURES as MAX_DISTINCT_SIGNATURES,
)

__all__ = [
    "CANONICAL_ROW_ORDER",
    "EXPECTED_SLICE_SIZE",
    "EXPECTED_TOTAL_ROWS",
    "MAX_DISTINCT_SIGNATURES",
    "REASON_ALLOWLIST_BY_STATUS",
    "RECORD_ATTEMPTS_OPERATION_KIND",
    "REGISTER_CAMPAIGN_OPERATION_KIND",
    "RUN_ID_PREFIX",
    "STATUSES",
    "ApprovalContextError",
    "ApprovedMutationContext",
    "BatchValidationError",
    "H6AAttemptBatchItem",
    "H6ABridgeError",
    "RunIdDerivationError",
    "TerminalEvidenceMismatch",
    "compute_exact_48_mapping_hash",
    "derive_campaign_run_id",
    "record_h6a_attempts",
    "register_h6a_campaign",
]

# A sentinel distinct from any real stored value (including ``None``) so a
# key that is GENUINELY ABSENT (a pre-ROB-981 row) can be told apart from
# one that is PRESENT with an explicit (possibly malformed) value -- mirrors
# ``research_campaign_bridge._MISSING`` / ``rob974_h6a_diagnostics.MISSING``.
# Never ``.get(key) or default`` -- that masks ``{}``/``0``/``False``/``None``
# as "missing" even though they are actually present-but-malformed.
_MISSING = object()
_DEFAULT_DIAGNOSTIC_OVERFLOW_PAYLOAD: dict[str, Any] = {
    "truncated": False,
    "omitted_distinct_signatures": 0,
    "omitted_occurrences": 0,
}

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# R3 finding #4: stand-in "canonical bytes" for a stored diagnostic that
# cannot be canonicalized at all (PRESENT-but-malformed persisted data) --
# never equal to any real _diagnostic_canonical_bytes(...) output, so a
# malformed stored value is always treated as a (loudly observed, non-fatal)
# divergence rather than silently masquerading as byte-identical.
_MALFORMED_STORED_DIAGNOSTIC_MARKER = b"__rob974_h6a_malformed_stored_diagnostic__"

EXPECTED_SLICE_SIZE = 24
EXPECTED_TOTAL_ROWS = 48
REGISTER_CAMPAIGN_OPERATION_KIND = "rob974_h6a_register_campaign"
RECORD_ATTEMPTS_OPERATION_KIND = "rob974_h6a_record_attempts"
RUN_ID_PREFIX = "rob974h6a-"

# R1 blocker #2: the canonical exact-48 row-id universe, literally
# duplicated from rob974_h6a_identity.CANONICAL_ROW_ORDER (this module
# never imports research/nautilus_scalping -- app/services stays
# DB-free-boundary-pure the same way rob944_campaign_controller's own
# literal duplication of ROB-944 constants does). A caller-supplied
# row_id_to_experiment_id/attempt batch is validated against THIS set, not
# merely checked for length-48 self-consistency with itself.
_CONFIGS_PER_STRATEGY = 24
_EXPECTED_STRATEGY_SLUGS: tuple[str, ...] = ("S3", "S4")
CANONICAL_ROW_ORDER: tuple[str, ...] = tuple(
    f"{slug}-{i:02d}"
    for slug in _EXPECTED_STRATEGY_SLUGS
    for i in range(_CONFIGS_PER_STRATEGY)
)
_CANONICAL_ROW_SET = frozenset(CANONICAL_ROW_ORDER)

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


class DiagnosticStorageError(H6ABridgeError):
    """A PRESENT (not genuinely absent) stored diagnostic value is
    malformed -- corrupted persisted data must loudly surface, never
    silently default to the empty/closed-default shape (R1 blocker #5;
    mirrors ``research_campaign_bridge.DiagnosticEvidenceBoundaryViolation``
    for the same absent-vs-malformed distinction)."""


def _freeze(obj: Any) -> Any:
    """Recursively converts dict/list into an immutable structure
    (``types.MappingProxyType`` + ``tuple``), a full deep, non-aliasing
    copy -- mirrors ``rob974_h6a_identity._freeze``/
    ``rob974_h6a_payload._freeze``/``rob974_h6a_evidence._freeze_mapping``.
    Duplicated here (never imported) since app/services never imports
    research/nautilus_scalping."""
    if isinstance(obj, Mapping):
        return types.MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, list | tuple):
        return tuple(_freeze(v) for v in obj)
    return obj


def _unfreeze(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: _unfreeze(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_unfreeze(v) for v in obj]
    return obj


class RunIdDerivationError(H6ABridgeError):
    """A caller-supplied ``campaign_run_id`` is not the value canonically
    derived from ``full_campaign_hash`` -- an arbitrary UUID/timestamp/
    operator typo is refused before any service call (R1 blocker #2)."""


def derive_campaign_run_id(full_campaign_hash: str) -> str:
    """Independent re-derivation of the SAME recipe
    ``rob974_h6a_payload.derive_primary_run_id`` uses (SHA-256 of
    ``{full_campaign_hash, kind}`` -> raw 32 bytes -> unpadded URL-safe
    base64 -> fixed prefix) -- duplicated here since app/services never
    imports research/nautilus_scalping. This is the actual persistence
    trust boundary and must not rely solely on a caller's own (separate)
    copy of this same check."""
    digest_hex = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "rob974_h6a_primary_run"}
    )
    raw = bytes.fromhex(digest_hex)
    suffix = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{RUN_ID_PREFIX}{suffix}"


def _require_derived_run_id(campaign_run_id: str, *, full_campaign_hash: str) -> None:
    expected = derive_campaign_run_id(full_campaign_hash)
    if campaign_run_id != expected:
        raise RunIdDerivationError(
            "campaign_run_id is not the value canonically derived from the frozen "
            "full_campaign_hash -- an arbitrary UUID/timestamp/operator typo is refused"
        )


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
    to.

    R1 blocker #2: this is the ONE place a caller-supplied mapping is
    checked against the canonical ``S3-00..S3-23,S4-00..S4-23`` row-id
    universe -- a same-length-48 but non-canonical row-id set (e.g.
    ``X-00..X-47``), a duplicate experiment_id, or a malformed
    (non-hex64) experiment_id is refused here, BEFORE any hash is even
    computed, rather than merely checked for internal self-consistency
    with itself.
    """
    if set(row_id_to_experiment_id) != _CANONICAL_ROW_SET:
        raise BatchValidationError(
            "row_id_to_experiment_id must cover exactly the canonical "
            f"{EXPECTED_TOTAL_ROWS} row IDs (S3-00..S3-23,S4-00..S4-23)"
        )
    experiment_ids = list(row_id_to_experiment_id.values())
    if len(set(experiment_ids)) != len(experiment_ids):
        raise BatchValidationError(
            "row_id_to_experiment_id experiment_id values must all be distinct"
        )
    for row_id, experiment_id in row_id_to_experiment_id.items():
        if type(experiment_id) is not str or not _HEX64_RE.match(experiment_id):
            raise BatchValidationError(
                f"experiment_id for row_id {row_id!r} must be a lowercase 64-hex digest"
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


# The full set of fields a registered ``ResearchStrategyExperiment`` row must
# match, independently re-derived from the caller's own trusted spec -- R3
# finding #2: an experiment_id-only re-check cannot detect a delegate that
# returns the right IDs with a tampered strategy_key/version or a tampered
# per-component hash on the returned row.
_IDENTITY_HASH_FIELDS: tuple[str, ...] = tuple(
    f"{name}_hash" for name in IDENTITY_COMPONENTS
)
_FULL_IDENTITY_FIELDS: tuple[str, ...] = (
    "strategy_key",
    "strategy_version",
) + _IDENTITY_HASH_FIELDS


def _preflight_specs(
    specs: Sequence[StrategyExperimentIdentity],
    *,
    expected_slug: str,
    row_id_to_experiment_id: Mapping[str, str],
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Independently re-derive and cross-check EVERY spec's experiment_id
    against the trusted mapping BEFORE any service call -- a malformed
    first/middle/LAST entry means zero registration calls for either slice
    (this loop is pure computation, no I/O).

    R3 finding #2: also enforces that ``specs`` are supplied in EXACT
    canonical row order (not merely as the correct SET) -- a caller-side
    reorder is refused here, before any service call, the same way a
    delegate-side reorder is refused post-delegate by
    ``_verify_registered_rows``.

    Returns ``(expected_row_order, expected_by_row_id)`` -- the canonical
    row-id sequence for this slice, and each row's FULL trusted identity
    (experiment_id + strategy_key/version + every component hash), so the
    caller can re-verify the registrar's returned rows' order AND full
    semantic identity against it post-delegate (R1 blocker #2 / R3
    finding #2)."""
    if len(specs) != EXPECTED_SLICE_SIZE:
        raise BatchValidationError(
            f"expected exactly {EXPECTED_SLICE_SIZE} {expected_slug} specs, got {len(specs)}"
        )
    expected_row_order = [
        row_id
        for row_id in CANONICAL_ROW_ORDER
        if row_id.startswith(f"{expected_slug}-")
    ]
    actual_row_order = [_spec_row_id(spec) for spec in specs]
    if actual_row_order != expected_row_order:
        raise BatchValidationError(
            f"{expected_slug} specs must be supplied in exact canonical row order "
            f"{expected_row_order!r}, got {actual_row_order!r} -- a same-set-but-reordered "
            "slice is refused before any service call"
        )
    expected_by_row_id: dict[str, dict[str, str]] = {}
    for row_id, spec in zip(expected_row_order, specs, strict=True):
        expected_experiment_id = row_id_to_experiment_id.get(row_id)
        if expected_experiment_id is None:
            raise BatchValidationError(
                f"row_id {row_id!r} is not present in the trusted row_id_to_experiment_id "
                "mapping"
            )
        hashes = compute_identity_hashes(spec.components())
        derived = derive_experiment_id(spec.strategy_key, spec.strategy_version, hashes)
        if derived != expected_experiment_id:
            raise BatchValidationError(
                f"spec for row_id {row_id!r} derives an experiment_id that does not match "
                "the trusted mapping"
            )
        expected_by_row_id[row_id] = {
            "experiment_id": expected_experiment_id,
            "strategy_key": spec.strategy_key,
            "strategy_version": spec.strategy_version,
            **hashes,
        }
    return expected_row_order, expected_by_row_id


def _verify_registered_rows(
    registered: Sequence[ResearchStrategyExperiment],
    *,
    expected_row_order: list[str],
    expected_by_row_id: dict[str, dict[str, str]],
    slug: str,
) -> None:
    """R3 finding #2: never trust the delegate's returned SEQUENCE or the
    returned rows' full identity at face value -- a delegate returning the
    correct 24 experiment_ids in a DIFFERENT order (a set-only re-check
    cannot see this), or the correct experiment_id with a tampered
    strategy_key/version/component hash on the row object itself, is
    refused here."""
    if len(registered) != len(expected_row_order):
        raise BatchValidationError(
            f"register_experiments_fn returned {len(registered)} {slug} rows, expected "
            f"exactly {len(expected_row_order)}"
        )
    for row_id, row in zip(expected_row_order, registered, strict=True):
        expected = expected_by_row_id[row_id]
        for field in _FULL_IDENTITY_FIELDS + ("experiment_id",):
            if getattr(row, field, _MISSING) != expected[field]:
                raise BatchValidationError(
                    f"register_experiments_fn returned a {slug} row at canonical position "
                    f"{row_id!r} whose {field!r} does not match the independently derived "
                    "value -- refusing to trust the delegate's return value (wrong order or "
                    "tampered identity)"
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
    _require_derived_run_id(campaign_run_id, full_campaign_hash=full_campaign_hash)
    _require_approved(
        approved,
        operation_kind=REGISTER_CAMPAIGN_OPERATION_KIND,
        full_campaign_hash=full_campaign_hash,
        campaign_run_id=campaign_run_id,
        mapping_hash=mapping_hash,
    )
    s3_row_order, expected_s3_by_row_id = _preflight_specs(
        s3_specs, expected_slug="S3", row_id_to_experiment_id=row_id_to_experiment_id
    )
    s4_row_order, expected_s4_by_row_id = _preflight_specs(
        s4_specs, expected_slug="S4", row_id_to_experiment_id=row_id_to_experiment_id
    )

    registered_s3 = await register_experiments_fn(
        session,
        specs=s3_specs,
        guard_opt_in_enabled=guard_opt_in_enabled,
        guard_policy=guard_policy,
    )
    # R1 blocker #2 / R3 finding #2: re-verify the delegate's OWN returned
    # rows by canonical ORDER and FULL identity -- never trust
    # register_experiments_fn's return value at face value, even though the
    # specs it was CALLED with were already trusted.
    _verify_registered_rows(
        registered_s3,
        expected_row_order=s3_row_order,
        expected_by_row_id=expected_s3_by_row_id,
        slug="S3",
    )

    registered_s4 = await register_experiments_fn(
        session,
        specs=s4_specs,
        guard_opt_in_enabled=guard_opt_in_enabled,
        guard_policy=guard_policy,
    )
    _verify_registered_rows(
        registered_s4,
        expected_row_order=s4_row_order,
        expected_by_row_id=expected_s4_by_row_id,
        slug="S4",
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
    # ROB-970-carrier-compatible (rob974_h6a_diagnostics.DiagnosticCarrier),
    # additive, persistence-only -- deliberately excluded from fingerprint()
    # below, exactly like ChildFailureDiagnostic is excluded from
    # terminal_evidence_fingerprint in the old S1/S2 bridge.
    diagnostic_evidence: tuple[Mapping[str, Any], ...] = ()
    diagnostic_overflow: Mapping[str, Any] | None = None

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
        if type(self.diagnostic_evidence) is not tuple:
            raise BatchValidationError("diagnostic_evidence must be an exact tuple")
        if len(self.diagnostic_evidence) > MAX_DISTINCT_SIGNATURES:
            raise BatchValidationError(
                f"diagnostic_evidence must have at most {MAX_DISTINCT_SIGNATURES} entries"
            )
        for row in self.diagnostic_evidence:
            if not isinstance(row, Mapping):
                raise BatchValidationError(
                    "each diagnostic_evidence entry must be a mapping"
                )
        if self.diagnostic_overflow is not None and not isinstance(
            self.diagnostic_overflow, Mapping
        ):
            raise BatchValidationError("diagnostic_overflow must be a mapping or None")
        # R1 blocker #4: seal a deep-frozen snapshot -- frozen=True only
        # blocks attribute REBINDING, not in-place mutation of a mutable
        # dict/tuple-of-dicts the attribute happens to hold. A caller
        # mutating evidence_payload/diagnostic_evidence/diagnostic_overflow
        # after construction now raises instead of silently desyncing the
        # sealed value from fingerprint()/the persisted raw_payload.
        object.__setattr__(self, "evidence_payload", _freeze(self.evidence_payload))
        object.__setattr__(
            self, "diagnostic_evidence", _freeze(self.diagnostic_evidence)
        )
        if self.diagnostic_overflow is not None:
            object.__setattr__(
                self, "diagnostic_overflow", _freeze(self.diagnostic_overflow)
            )

    def idempotency_key(self, campaign_run_id: str) -> str:
        return f"{campaign_run_id}:{self.experiment_id}:{self.retry_index}"

    def fingerprint(self) -> str:
        # Deliberately excludes diagnostic_evidence/diagnostic_overflow --
        # additive/persistence-only, never semantic identity.
        return canonical_sha256(
            {
                "status": self.status,
                "reason_code": self.reason_code,
                "fold_evidence_hash": self.fold_evidence_hash,
                "run_identity": self.run_identity,
                "evidence_payload": _unfreeze(self.evidence_payload),
            }
        )

    def diagnostic_evidence_payload(self) -> list[dict]:
        return [_unfreeze(row) for row in self.diagnostic_evidence]

    def diagnostic_overflow_payload(self) -> dict:
        if self.diagnostic_overflow is None:
            return dict(_DEFAULT_DIAGNOSTIC_OVERFLOW_PAYLOAD)
        return _unfreeze(self.diagnostic_overflow)


def _preflight_attempts(
    attempts: Sequence[H6AAttemptBatchItem],
    *,
    row_id_to_experiment_id: Mapping[str, str],
) -> None:
    """``record_h6a_attempts`` records ONLY the primary (retry_index=0)
    batch -- an explicit retry is a separate, later, single-item call (a
    real H4 rerun of one config), never smuggled into this 48-row batch.

    R1 blocker #2: row_id set is checked against the CANONICAL universe
    directly (never merely against whatever keys the caller's own
    ``row_id_to_experiment_id`` happens to have -- that mapping is ALSO
    independently validated by ``compute_exact_48_mapping_hash``, but this
    check does not rely on that ordering)."""
    if len(attempts) != EXPECTED_TOTAL_ROWS:
        raise BatchValidationError(
            f"expected exactly {EXPECTED_TOTAL_ROWS} attempts, got {len(attempts)}"
        )
    seen_row_ids = [a.row_id for a in attempts]
    if len(set(seen_row_ids)) != EXPECTED_TOTAL_ROWS:
        raise BatchValidationError("duplicate row_id present in attempt batch")
    if set(seen_row_ids) != _CANONICAL_ROW_SET:
        raise BatchValidationError(
            "attempt batch does not cover exactly the canonical 48 row IDs "
            "(S3-00..S3-23,S4-00..S4-23)"
        )
    if set(seen_row_ids) != set(row_id_to_experiment_id):
        raise BatchValidationError(
            "attempt batch does not match row_id_to_experiment_id's row-id set"
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
        if attempt.retry_index != 0:
            raise BatchValidationError(
                f"record_h6a_attempts only accepts the primary (retry_index=0) batch -- "
                f"row_id {attempt.row_id!r} has retry_index={attempt.retry_index}"
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


def _diagnostic_canonical_bytes(
    evidence_payload: list[dict], overflow_payload: dict
) -> bytes:
    """The SOLE comparison authority for replay-divergence detection --
    mirrors ``rob974_h6a_diagnostics.canonical_diagnostic_bytes`` /
    ``research_campaign_bridge._canonical_diagnostic_bytes`` (this module
    cannot import the research-side sibling -- app/services never imports
    research/nautilus_scalping -- so the SAME shape is reconstructed here
    via the shared ``canonical_json`` authority)."""
    return canonical_json(
        {
            "diagnostic_evidence": evidence_payload,
            "diagnostic_overflow": overflow_payload,
        }
    ).encode("utf-8")


def _stored_diagnostic_evidence_payload(row: ResearchBacktestRun) -> list[dict]:
    """R1 blocker #5: genuinely ABSENT (key missing entirely -- a
    pre-ROB-981 row) normalizes to ``[]`` (an empty list IS a legitimate
    "no diagnostics captured" fact, same semantic meaning as absent). A
    PRESENT value that is malformed (not a list, or containing a non-dict
    item) is a DIFFERENT, more severe case -- corrupted persisted data --
    and is never silently coerced to the same default; it raises
    ``DiagnosticStorageError`` instead (mirrors
    ``research_campaign_bridge._stored_diagnostic_evidence_payload``'s own
    absent-vs-malformed discipline exactly)."""
    raw_payload = row.raw_payload
    if type(raw_payload) is not dict:
        raise DiagnosticStorageError(
            "stored trial raw_payload is not a dict -- cannot resolve diagnostic evidence"
        )
    value = raw_payload.get("diagnostic_evidence", _MISSING)
    if value is _MISSING:
        return []
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise DiagnosticStorageError(
            "stored diagnostic_evidence is PRESENT but malformed (not a list of dicts) -- "
            "refusing to silently default past corrupted persisted data"
        )
    return value


def _stored_diagnostic_overflow_payload(row: ResearchBacktestRun) -> dict:
    """Same absent-vs-malformed distinction as
    ``_stored_diagnostic_evidence_payload`` -- genuinely absent normalizes
    to the closed default shape; a PRESENT value that is not a dict raises
    rather than silently defaulting."""
    raw_payload = row.raw_payload
    if type(raw_payload) is not dict:
        raise DiagnosticStorageError(
            "stored trial raw_payload is not a dict -- cannot resolve diagnostic overflow"
        )
    value = raw_payload.get("diagnostic_overflow", _MISSING)
    if value is _MISSING:
        return dict(_DEFAULT_DIAGNOSTIC_OVERFLOW_PAYLOAD)
    if type(value) is not dict:
        raise DiagnosticStorageError(
            "stored diagnostic_overflow is PRESENT but malformed (not a dict) -- refusing "
            "to silently default past corrupted persisted data"
        )
    return value


def _emit_diagnostic_replay_divergence(
    *, idempotency_key: str, stored_bytes: bytes, incoming_bytes: bytes
) -> None:
    """Digest-only, bounded, non-fail-stop observation -- mirrors
    ``research_campaign_bridge._emit_diagnostic_replay_divergence``. Wrapped
    so an emission failure can NEVER alter the primary (already-decided,
    already-returned) outcome or any semantic byte."""
    try:
        payload = {
            "event": "rob974_h6a_diagnostic_replay_divergence",
            "idempotency_key_digest": hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest(),
            "stored_diagnostic_digest": hashlib.sha256(stored_bytes).hexdigest(),
            "incoming_diagnostic_digest": hashlib.sha256(incoming_bytes).hexdigest(),
        }
        sys.stderr.write(repr(payload) + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 -- observer failure must never propagate
        pass


def _check_diagnostic_divergence(
    existing: ResearchBacktestRun,
    incoming: H6AAttemptBatchItem,
    *,
    idempotency_key: str,
) -> None:
    """Never fail-stop -- semantic identity has ALREADY matched by the time
    this is called (see the caller). Byte-identical is a write-free no-op;
    any divergence is loudly observed (never merged, never silently
    discarded) while the returned/original row is unaffected either way.

    R3 finding #4 (AC29): a PRESENT-but-malformed stored diagnostic
    (corrupted historic data, ``DiagnosticStorageError``) must be treated as
    an observed divergence too -- NOT re-raised here. Re-raising would let a
    diagnostic-only concern fail-stop an otherwise-legitimate semantic
    replay whose primary fingerprint already matched; that is exactly the
    "malformed diagnostic alters primary outcome" defect this closes. The
    malformed-stored-value case is still loudly observed (a fixed marker
    digest stands in for "could not be canonicalized"), never silently
    dropped."""
    try:
        stored_bytes = _diagnostic_canonical_bytes(
            _stored_diagnostic_evidence_payload(existing),
            _stored_diagnostic_overflow_payload(existing),
        )
    except DiagnosticStorageError:
        stored_bytes = _MALFORMED_STORED_DIAGNOSTIC_MARKER
    incoming_bytes = _diagnostic_canonical_bytes(
        incoming.diagnostic_evidence_payload(), incoming.diagnostic_overflow_payload()
    )
    if stored_bytes == incoming_bytes:
        return
    _emit_diagnostic_replay_divergence(
        idempotency_key=idempotency_key,
        stored_bytes=stored_bytes,
        incoming_bytes=incoming_bytes,
    )


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
    _require_derived_run_id(campaign_run_id, full_campaign_hash=full_campaign_hash)
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
                _check_diagnostic_divergence(
                    existing, item, idempotency_key=idempotency_key
                )
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
                # R3 finding #3: evidence_payload is deep-frozen (nested
                # MappingProxyType/tuple) -- a shallow `dict(...)` leaves
                # nested proxies in the tree, which the real DB JSON
                # serializer cannot handle. `_unfreeze` recursively converts
                # every level back to built-in dict/list.
                "evidence_payload": _unfreeze(item.evidence_payload),
                "diagnostic_evidence": item.diagnostic_evidence_payload(),
                "diagnostic_overflow": item.diagnostic_overflow_payload(),
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
        # The post-delegate winner row may not be the one THIS call tried to
        # insert (a concurrent race) -- same non-fail-stop diagnostic check
        # applies here too. When `returned` IS the row this call just
        # inserted, its stored diagnostic bytes trivially equal this item's
        # own, so the check is a guaranteed no-op observation-wise.
        _check_diagnostic_divergence(returned, item, idempotency_key=idempotency_key)
        results.append(returned)
    return results
