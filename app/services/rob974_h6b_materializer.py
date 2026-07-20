"""ROB-984 H6-B composition boundary.

CP1 supplies the immutable plan/preflight vocabulary and the sole issuer for
H6-A mutation contexts.  Transaction and filesystem behavior is added by the
later checkpoints in this same module; no predecessor owns either concern.

The pre-H4/H5 plan is deliberately a ``contract_fixture``.  Its private H6-A
fixture identity is usable by call-spy tests, but is never rendered as a
production full-campaign hash or run id and is never accepted by ``--run``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.services.research_db_write_guard import ResearchDbPolicy
from app.services.rob974_h6a_bridge import (
    RECORD_ATTEMPTS_OPERATION_KIND,
    REGISTER_CAMPAIGN_OPERATION_KIND,
    ApprovedMutationContext,
    H6AAttemptBatchItem,
    compute_exact_48_mapping_hash,
    derive_campaign_run_id,
    record_h6a_attempts,
    register_h6a_campaign,
)

__all__ = [
    "AUTHORITY_OR_PREFLIGHT_REFUSED",
    "CANONICAL_ROW_ORDER",
    "CLI_USAGE_OR_PLAN_ERROR",
    "COMMIT_FAILED_OR_UNKNOWN",
    "CommitRejectedError",
    "CampaignDbSnapshot",
    "CampaignStateInspector",
    "ContractFixtureCampaignInput",
    "ContractFixtureExecutionPorts",
    "ContractFixturePlan",
    "CoordinatorCounters",
    "DatabaseTarget",
    "EXIT_DISPOSITION_TABLE",
    "ExactSourcePins",
    "H6BPlanError",
    "H6BPreflightRefused",
    "H6BDiagnosticCapture",
    "DiagnosticCapturePort",
    "H6AAccountingPort",
    "H5CompositionPort",
    "IssuedOneShotAuthorization",
    "MATERIALIZED_EXIT",
    "POSTAUDIT_FAILURE",
    "POSTCOMMIT_PUBLISH_FAILURE",
    "PRECOMMIT_FAILURE",
    "MaterializationOutcome",
    "PredecessorTransactionOwnershipError",
    "ProductionExecutionPlan",
    "RunAuthority",
    "ReplayCollisionError",
    "SESSION_CLOSE_FAILURE",
    "build_h6a_mutation_contexts",
    "issue_contract_fixture_authorization",
    "issue_run_authorization",
    "materialize_contract_fixture",
    "materialize_or_replay_contract_fixture",
    "render_safe_materialization_failure",
    "validate_database_target_pair",
    "validate_derived_run_id",
    "validate_exact_48_mapping",
    "validate_h6a_context_pair",
    "validate_source_pins_pair",
]

MATERIALIZED_EXIT = 0
CLI_USAGE_OR_PLAN_ERROR = 2
AUTHORITY_OR_PREFLIGHT_REFUSED = 4
PRECOMMIT_FAILURE = 6
COMMIT_FAILED_OR_UNKNOWN = 7
POSTCOMMIT_PUBLISH_FAILURE = 8
POSTAUDIT_FAILURE = 9
SESSION_CLOSE_FAILURE = 10

EXIT_DISPOSITION_TABLE: tuple[tuple[int, tuple[str, ...], str], ...] = (
    (
        MATERIALIZED_EXIT,
        ("MATERIALIZED", "REPLAY_NOOP"),
        "physical materialization completed or exact write-free replay verified; "
        "inspect the H5 semantic verdict separately",
    ),
    (
        CLI_USAGE_OR_PLAN_ERROR,
        ("CLI_USAGE_OR_PLAN_ERROR",),
        "zero session/query/child/staging effects",
    ),
    (
        AUTHORITY_OR_PREFLIGHT_REFUSED,
        ("AUTHORITY_OR_PREFLIGHT_REFUSED",),
        "zero session/query/child/staging effects",
    ),
    (
        PRECOMMIT_FAILURE,
        ("PRECOMMIT_FAILURE",),
        "rollback outcome explicit and publish count zero",
    ),
    (
        COMMIT_FAILED_OR_UNKNOWN,
        ("COMMIT_FAILED", "COMMIT_OUTCOME_UNKNOWN"),
        "publish count zero; no retry; standalone READ ONLY audit required",
    ),
    (
        POSTCOMMIT_PUBLISH_FAILURE,
        ("DB_DURABLE_ARTIFACT_UNPUBLISHED",),
        "no rollback claim; durable DB and staging/final state explicit",
    ),
    (
        POSTAUDIT_FAILURE,
        ("POSTAUDIT_FAILURE",),
        "standalone audit failed or mismatched with mutation count zero",
    ),
    (
        SESSION_CLOSE_FAILURE,
        ("MATERIALIZED_CLOSE_FAILED", "REPLAY_NOOP_CLOSE_FAILED"),
        "close alone failed after a confirmed durable disposition; no rollback/retry",
    ),
)

CANONICAL_ROW_ORDER: tuple[str, ...] = tuple(
    [f"S3-{index:02d}" for index in range(24)]
    + [f"S4-{index:02d}" for index in range(24)]
)
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_EMPIRICAL_DATABASE = "rob974_db"


class H6BPlanError(ValueError):
    """The pure plan or an exact-type plan value is malformed."""


class H6BPreflightRefused(ValueError):
    """A run authority differs from its independently frozen plan/target."""


def _hex64(value: object, name: str) -> str:
    if type(value) is not str or _HEX64_RE.fullmatch(value) is None:
        raise H6BPlanError(f"{name} must be exact built-in lowercase hex64")
    return value


def _exact_nonempty_str(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise H6BPlanError(f"{name} must be an exact non-empty built-in str")
    return value


def validate_exact_48_mapping(
    mapping: tuple[tuple[str, str], ...],
) -> str:
    """Validate order, exact container types, uniqueness, and H6-A's hash."""
    if type(mapping) is not tuple:
        raise H6BPlanError("exact-48 mapping must be an exact tuple")
    if len(mapping) != 48:
        raise H6BPlanError("exact-48 mapping must contain exactly 48 entries")
    for item in mapping:
        if type(item) is not tuple or len(item) != 2:
            raise H6BPlanError("each exact-48 mapping entry must be a 2-tuple")
        if type(item[0]) is not str or type(item[1]) is not str:
            raise H6BPlanError("mapping row and experiment ids must be built-in str")
        _hex64(item[1], f"experiment id for {item[0]!r}")
    row_ids = tuple(row_id for row_id, _ in mapping)
    if row_ids != CANONICAL_ROW_ORDER:
        raise H6BPlanError("mapping order must be exactly S3-00..S3-23,S4-00..S4-23")
    experiment_ids = tuple(experiment_id for _, experiment_id in mapping)
    if len(set(experiment_ids)) != 48:
        raise H6BPlanError("mapping experiment ids must be unique")
    return compute_exact_48_mapping_hash(dict(mapping))


@dataclass(frozen=True, slots=True)
class ContractFixturePlan:
    """Pre-CP8 H6-A-backed plan; private identity is never rendered."""

    ordered_mapping: tuple[tuple[str, str], ...]
    contract_fixture_mapping_hash: str
    h6a_payload_schema_version: str
    h6a_source_pins: tuple[tuple[str, None], ...]
    _fixture_campaign_hash: str = field(repr=False)
    _fixture_run_id: str = field(repr=False)

    def __post_init__(self) -> None:
        actual_mapping_hash = validate_exact_48_mapping(self.ordered_mapping)
        if self.contract_fixture_mapping_hash != actual_mapping_hash:
            raise H6BPlanError("contract-fixture mapping hash mismatch")
        _exact_nonempty_str(
            self.h6a_payload_schema_version, "h6a_payload_schema_version"
        )
        if self.h6a_source_pins != (
            ("feature_source_sha256", None),
            ("engine_source_sha256", None),
            ("runner_source_sha256", None),
            ("pbo_implementation_sha256", None),
        ):
            raise H6BPlanError("fixture plan must retain the closed all-None H6-A pins")
        _hex64(self._fixture_campaign_hash, "fixture campaign hash")
        expected_run_id = derive_campaign_run_id(self._fixture_campaign_hash)
        if self._fixture_run_id != expected_run_id:
            raise H6BPlanError("fixture run id is not H6-A-derived")

    def to_payload(self) -> dict[str, object]:
        """Public plan payload: no production full hash/run-id claim."""
        return {
            "schema_version": "rob974_h6b_plan.v1",
            "status": "NOT_LAUNCHABLE_CONTRACT_FIXTURE",
            "predecessor_mode": "contract_fixture",
            "actual_h4_contract": "NOT_EVALUATED",
            "actual_h5_contract": "NOT_EVALUATED",
            "production_identity": "DEFERRED_UNTIL_H4_SOURCE_PINS",
            "launchability": "NOT_LAUNCHABLE_CONTRACT_FIXTURE",
            "h6a": {
                "identity_api": "rob974_h6a_identity",
                "payload_api": "rob974_h6a_payload",
                "payload_schema_version": self.h6a_payload_schema_version,
                "payload_mode": "fixture_plan",
                "source_pins": dict(self.h6a_source_pins),
            },
            "contract_fixture_ordered_mapping": [
                {"row_id": row_id, "experiment_id": experiment_id}
                for row_id, experiment_id in self.ordered_mapping
            ],
            "contract_fixture_exact_48_mapping_hash": (
                self.contract_fixture_mapping_hash
            ),
            "exit_disposition_table": [
                {
                    "exit": code,
                    "dispositions": list(dispositions),
                    "meaning": meaning,
                }
                for code, dispositions, meaning in EXIT_DISPOSITION_TABLE
            ],
        }


@dataclass(frozen=True, slots=True)
class DatabaseTarget:
    host: str
    port: int
    database: str
    user: str

    def __post_init__(self) -> None:
        _exact_nonempty_str(self.host, "database host")
        if type(self.port) is not int or self.port <= 0 or self.port > 65_535:
            raise H6BPlanError("database port must be an exact built-in int")
        _exact_nonempty_str(self.database, "database name")
        _exact_nonempty_str(self.user, "database user")


def validate_database_target_pair(
    *,
    approved: DatabaseTarget,
    observed: DatabaseTarget,
    inherited: DatabaseTarget | None,
) -> None:
    """Exact empirical-target comparison; no aliases, folds, or defaults."""
    if type(approved) is not DatabaseTarget or type(observed) is not DatabaseTarget:
        raise H6BPreflightRefused("database targets must be exact DatabaseTarget")
    if inherited is not None and type(inherited) is not DatabaseTarget:
        raise H6BPreflightRefused("inherited target must be exact or absent")
    if approved.database != _EMPIRICAL_DATABASE:
        raise H6BPreflightRefused("approved empirical database is not exact rob974_db")
    if observed != approved:
        raise H6BPreflightRefused("observed database target differs byte-for-byte")
    if inherited is not None and inherited != approved:
        raise H6BPreflightRefused("inherited DSN target conflicts with approval")


def validate_derived_run_id(*, full_campaign_hash: str, campaign_run_id: str) -> None:
    _hex64(full_campaign_hash, "full_campaign_hash")
    if type(campaign_run_id) is not str:
        raise H6BPreflightRefused("campaign_run_id must be exact built-in str")
    if campaign_run_id != derive_campaign_run_id(full_campaign_hash):
        raise H6BPreflightRefused("campaign_run_id is not H6-A-derived")


@dataclass(frozen=True, slots=True)
class ExactSourcePins:
    integration_head_sha: str
    integration_tree_sha: str
    feature_source_sha256: str
    engine_source_sha256: str
    runner_source_sha256: str
    pbo_implementation_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "integration_head_sha",
            "integration_tree_sha",
            "feature_source_sha256",
            "engine_source_sha256",
            "runner_source_sha256",
            "pbo_implementation_sha256",
        ):
            _hex64(getattr(self, name), name)


def validate_source_pins_pair(
    *, expected: ExactSourcePins, observed: ExactSourcePins
) -> None:
    if type(expected) is not ExactSourcePins or type(observed) is not ExactSourcePins:
        raise H6BPreflightRefused("source pins must use the exact sealed type")
    if observed != expected:
        raise H6BPreflightRefused("observed source/tree pins differ from approval")


_PRODUCTION_PLAN_SEAL = object()


@dataclass(frozen=True, slots=True)
class ProductionExecutionPlan:
    """CP8-owned plan carrier; construction without the adapter seal fails."""

    full_campaign_hash: str
    campaign_run_id: str
    ordered_mapping: tuple[tuple[str, str], ...]
    exact_48_mapping_hash: str
    source_pins: ExactSourcePins
    output_root: Path
    provenance: str
    _seal: object = field(repr=False)

    def __post_init__(self) -> None:
        if self._seal is not _PRODUCTION_PLAN_SEAL:
            raise H6BPlanError("production plan was not issued by the CP8 adapter")
        if self.provenance != "actual_merged_h4_h5":
            raise H6BPlanError("production plan provenance is not actual H4/H5")
        _hex64(self.full_campaign_hash, "full_campaign_hash")
        try:
            validate_derived_run_id(
                full_campaign_hash=self.full_campaign_hash,
                campaign_run_id=self.campaign_run_id,
            )
        except H6BPreflightRefused as exc:
            raise H6BPlanError(str(exc)) from exc
        mapping_hash = validate_exact_48_mapping(self.ordered_mapping)
        if self.exact_48_mapping_hash != mapping_hash:
            raise H6BPlanError("production mapping hash mismatch")
        if type(self.source_pins) is not ExactSourcePins:
            raise H6BPlanError("source_pins must be exact ExactSourcePins")
        if not isinstance(self.output_root, Path) or not self.output_root.is_absolute():
            raise H6BPlanError("production output root must be an absolute Path")


@dataclass(frozen=True, slots=True)
class RunAuthority:
    expected_full_campaign_hash: str
    expected_campaign_run_id: str
    expected_exact_48_mapping_hash: str
    approved_target: DatabaseTarget
    observed_target: DatabaseTarget
    inherited_target: DatabaseTarget | None
    write_opt_in: bool
    expected_output_root: Path
    requested_output_root: Path
    expected_source_pins: ExactSourcePins
    observed_source_pins: ExactSourcePins
    one_shot_approval: str

    def __post_init__(self) -> None:
        _hex64(self.expected_full_campaign_hash, "expected_full_campaign_hash")
        _exact_nonempty_str(self.expected_campaign_run_id, "expected_campaign_run_id")
        _hex64(
            self.expected_exact_48_mapping_hash,
            "expected_exact_48_mapping_hash",
        )
        if type(self.write_opt_in) is not bool:
            raise H6BPlanError("write_opt_in must be exact bool")
        for name in ("expected_output_root", "requested_output_root"):
            value = getattr(self, name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise H6BPlanError(f"{name} must be an absolute Path")
        _exact_nonempty_str(self.one_shot_approval, "one_shot_approval")


_ISSUER_SEAL = object()


class IssuedOneShotAuthorization:
    """Mutable one-shot capability; its state is authorization-only."""

    __slots__ = (
        "_campaign_hash",
        "_mapping_hash",
        "_run_id",
        "_token",
        "_used",
        "_issuer",
    )

    def __init__(
        self,
        *,
        campaign_hash: str,
        run_id: str,
        mapping_hash: str,
        token: str,
        _issuer: object,
    ) -> None:
        self._campaign_hash = campaign_hash
        self._run_id = run_id
        self._mapping_hash = mapping_hash
        self._token = token
        self._used = False
        self._issuer = _issuer

    def _consume(self) -> tuple[str, str, str, str]:
        if (
            type(self) is not IssuedOneShotAuthorization
            or self._issuer is not _ISSUER_SEAL
        ):
            raise H6BPreflightRefused("authorization was independently minted")
        if self._used:
            raise H6BPreflightRefused("one-shot authorization was already consumed")
        self._used = True
        return (
            self._campaign_hash,
            self._run_id,
            self._mapping_hash,
            self._token,
        )


def issue_run_authorization(
    plan: ProductionExecutionPlan | ContractFixturePlan,
    authority: RunAuthority,
) -> IssuedOneShotAuthorization:
    """Validate every production gate before issuing the sole capability."""
    if type(plan) is not ProductionExecutionPlan:
        raise H6BPreflightRefused("contract-fixture plan is not launchable")
    if type(authority) is not RunAuthority:
        raise H6BPreflightRefused("authority must be exact RunAuthority")
    validate_database_target_pair(
        approved=authority.approved_target,
        observed=authority.observed_target,
        inherited=authority.inherited_target,
    )
    if authority.write_opt_in is not True:
        raise H6BPreflightRefused("explicit write opt-in is required")
    if authority.expected_full_campaign_hash != plan.full_campaign_hash:
        raise H6BPreflightRefused("expected full-campaign hash differs")
    if authority.expected_campaign_run_id != plan.campaign_run_id:
        raise H6BPreflightRefused("expected campaign run id differs")
    if authority.expected_exact_48_mapping_hash != plan.exact_48_mapping_hash:
        raise H6BPreflightRefused("expected exact-48 mapping hash differs")
    if authority.requested_output_root != authority.expected_output_root:
        raise H6BPreflightRefused("output root differs from explicit approval")
    if authority.expected_output_root != plan.output_root:
        raise H6BPreflightRefused("approved output root differs from plan")
    if authority.expected_source_pins != plan.source_pins:
        raise H6BPreflightRefused("expected source/tree pins differ from plan")
    validate_source_pins_pair(
        expected=plan.source_pins, observed=authority.observed_source_pins
    )
    return IssuedOneShotAuthorization(
        campaign_hash=plan.full_campaign_hash,
        run_id=plan.campaign_run_id,
        mapping_hash=plan.exact_48_mapping_hash,
        token=authority.one_shot_approval,
        _issuer=_ISSUER_SEAL,
    )


def issue_contract_fixture_authorization(
    plan: ContractFixturePlan, *, approval_token: str
) -> IssuedOneShotAuthorization:
    """Explicit CP1-CP7 call-spy permit; never reachable from the CLI."""
    if type(plan) is not ContractFixturePlan:
        raise H6BPreflightRefused("fixture plan must be exact ContractFixturePlan")
    token = _exact_nonempty_str(approval_token, "contract fixture approval token")
    mapping_hash = validate_exact_48_mapping(plan.ordered_mapping)
    if mapping_hash != plan.contract_fixture_mapping_hash:
        raise H6BPreflightRefused("fixture mapping drift")
    if plan._fixture_run_id != derive_campaign_run_id(plan._fixture_campaign_hash):
        raise H6BPreflightRefused("fixture identity drift")
    return IssuedOneShotAuthorization(
        campaign_hash=plan._fixture_campaign_hash,
        run_id=plan._fixture_run_id,
        mapping_hash=mapping_hash,
        token=token,
        _issuer=_ISSUER_SEAL,
    )


def build_h6a_mutation_contexts(
    authorization: IssuedOneShotAuthorization,
) -> tuple[ApprovedMutationContext, ApprovedMutationContext]:
    """H6-B's only context constructor; consumes one authorization once."""
    if type(authorization) is not IssuedOneShotAuthorization:
        raise H6BPreflightRefused("authorization must be the exact issued type")
    campaign_hash, run_id, mapping_hash, token = authorization._consume()
    register = ApprovedMutationContext(
        operation_kind=REGISTER_CAMPAIGN_OPERATION_KIND,
        canonical_plan_hash=campaign_hash,
        derived_run_id=run_id,
        exact_48_mapping_hash=mapping_hash,
        approval_token=token,
    )
    record = ApprovedMutationContext(
        operation_kind=RECORD_ATTEMPTS_OPERATION_KIND,
        canonical_plan_hash=campaign_hash,
        derived_run_id=run_id,
        exact_48_mapping_hash=mapping_hash,
        approval_token=token,
    )
    validate_h6a_context_pair(register, record)
    return register, record


def validate_h6a_context_pair(
    register: ApprovedMutationContext, record: ApprovedMutationContext
) -> None:
    """Reject a caller-built/subclassed/swapped pair before any session."""
    if (
        type(register) is not ApprovedMutationContext
        or type(record) is not ApprovedMutationContext
    ):
        raise H6BPreflightRefused("H6-A contexts must use the exact approved type")
    if register.operation_kind != REGISTER_CAMPAIGN_OPERATION_KIND:
        raise H6BPreflightRefused("register context operation kind is swapped")
    if record.operation_kind != RECORD_ATTEMPTS_OPERATION_KIND:
        raise H6BPreflightRefused("record context operation kind is swapped")
    shared_register = (
        register.canonical_plan_hash,
        register.derived_run_id,
        register.exact_48_mapping_hash,
        register.approval_token,
    )
    shared_record = (
        record.canonical_plan_hash,
        record.derived_run_id,
        record.exact_48_mapping_hash,
        record.approval_token,
    )
    if shared_register != shared_record:
        raise H6BPreflightRefused("H6-A contexts do not share one authorization")


class H6AAccountingPort(Protocol):
    """Exact H6-A accounting adapter; H6-B never reproduces its algorithm."""

    provenance: str

    def reconstruct(
        self,
        *,
        plan: ContractFixturePlan,
        registered_total: int,
        attempts: Sequence[H6AAttemptBatchItem],
    ) -> object: ...


class H5CompositionPort(Protocol):
    """Pure H5 composition plus the canonical artifact methods from CP2."""

    provenance: str

    def build_scorecard(
        self,
        *,
        plan: ContractFixturePlan,
        attempts: Sequence[H6AAttemptBatchItem],
        accounting: object,
    ) -> dict[str, object]: ...

    def canonical_json_bytes(self, scorecard: Mapping[str, object]) -> bytes: ...

    def semantic_hash(self, scorecard: Mapping[str, object]) -> str: ...

    def render_markdown(self, scorecard: Mapping[str, object]) -> bytes: ...


class ArtifactPairPort(Protocol):
    """H6-B physical primitive adapter; the implementation is CP2's module."""

    provenance: str

    def stage(
        self,
        *,
        scorecard: Mapping[str, object],
        output_dir: Path,
        h5_port: H5CompositionPort,
    ) -> object: ...

    def publish(self, staged: object, *, h5_port: H5CompositionPort) -> object: ...

    def probe(self, *, output_dir: Path) -> object: ...

    def inspect(
        self,
        *,
        scorecard: Mapping[str, object],
        output_dir: Path,
        h5_port: H5CompositionPort,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class CampaignDbSnapshot:
    """Canonical raw-row projection returned by the CP4 inspection seam."""

    campaign_run_id: str | None
    registered_mapping: tuple[tuple[str, str], ...]
    attempts: tuple[H6AAttemptBatchItem, ...]
    mismatch_row_ids: tuple[str, ...] = ()
    out_of_plan_experiment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.campaign_run_id is not None and type(self.campaign_run_id) is not str:
            raise H6BPlanError("snapshot campaign_run_id must be exact str or None")
        if type(self.registered_mapping) is not tuple:
            raise H6BPlanError("snapshot registered mapping must be an exact tuple")
        for item in self.registered_mapping:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
            ):
                raise H6BPlanError("snapshot registration entries must be string pairs")
        if type(self.attempts) is not tuple or any(
            type(item) is not H6AAttemptBatchItem for item in self.attempts
        ):
            raise H6BPlanError("snapshot attempts must be exact H6-A item tuples")
        for name in ("mismatch_row_ids", "out_of_plan_experiment_ids"):
            values = getattr(self, name)
            if type(values) is not tuple or any(
                type(item) is not str for item in values
            ):
                raise H6BPlanError(f"snapshot {name} must be an exact string tuple")

    def is_absent(self) -> bool:
        return (
            self.campaign_run_id is None
            and self.registered_mapping == ()
            and self.attempts == ()
            and self.mismatch_row_ids == ()
            and self.out_of_plan_experiment_ids == ()
        )


class CampaignStateInspector(Protocol):
    """Read-only DB snapshot seam; CP5 replaces it with first-statement RO."""

    provenance: str

    async def inspect(
        self, session: object, *, plan: ContractFixturePlan
    ) -> CampaignDbSnapshot: ...


_DIAGNOSTIC_BOUNDARIES = frozenset(
    {"feature", "generator", "funding_gate", "engine", "metric", "materializer"}
)
_DIAGNOSTIC_SANITIZER_STAGES = frozenset({"generator", "funding_gate", "engine"})


@dataclass(frozen=True, slots=True)
class H6BDiagnosticCapture:
    """Safe metadata-only projection of the merged ROB-970 evidence type."""

    catch_boundary: str
    sanitizer_stage: str
    exception_type: str
    message: str
    traceback_text: str
    innermost_file: str
    innermost_function: str
    innermost_line: int
    signature: str
    occurrence_count: int
    truncated: bool
    has_cause: bool
    has_context: bool

    def __post_init__(self) -> None:
        if self.catch_boundary not in _DIAGNOSTIC_BOUNDARIES:
            raise H6BPlanError("diagnostic catch boundary is not closed")
        if self.sanitizer_stage not in _DIAGNOSTIC_SANITIZER_STAGES:
            raise H6BPlanError("diagnostic sanitizer stage is not authoritative")
        if (
            type(self.exception_type) is not str
            or not self.exception_type
            or len(self.exception_type) > 200
        ):
            raise H6BPlanError("diagnostic exception type is malformed")
        if type(self.message) is not str or len(self.message) > 500:
            raise H6BPlanError("diagnostic message exceeds the ROB-970 cap")
        if (
            type(self.traceback_text) is not str
            or not self.traceback_text
            or len(self.traceback_text) > 4000
        ):
            raise H6BPlanError("diagnostic traceback exceeds the ROB-970 cap")
        for name in ("innermost_file", "innermost_function"):
            value = getattr(self, name)
            if type(value) is not str or not value or "/" in value or "\\" in value:
                raise H6BPlanError(f"diagnostic {name} must be safe basename metadata")
        if type(self.innermost_line) is not int or self.innermost_line <= 0:
            raise H6BPlanError("diagnostic innermost line must be a positive int")
        _hex64(self.signature, "diagnostic signature")
        if type(self.occurrence_count) is not int or self.occurrence_count <= 0:
            raise H6BPlanError("diagnostic occurrence count must be positive int")
        for name in ("truncated", "has_cause", "has_context"):
            if type(getattr(self, name)) is not bool:
                raise H6BPlanError(f"diagnostic {name} must be exact bool")

    def to_safe_payload(self) -> dict[str, object]:
        return {
            "catch_boundary": self.catch_boundary,
            "sanitizer_stage": self.sanitizer_stage,
            "exception_type": self.exception_type,
            "message": self.message,
            "traceback_text": self.traceback_text,
            "innermost_frame": {
                "file": self.innermost_file,
                "function": self.innermost_function,
                "line": self.innermost_line,
            },
            "signature": self.signature,
            "occurrence_count": self.occurrence_count,
            "truncated": self.truncated,
            "has_cause": self.has_cause,
            "has_context": self.has_context,
        }


class DiagnosticCapturePort(Protocol):
    provenance: str

    def capture_live_exception(
        self,
        exc: BaseException,
        *,
        catch_boundary: str,
        strategy: str,
        config_id: str,
    ) -> H6BDiagnosticCapture: ...


@dataclass(frozen=True, slots=True)
class ContractFixtureCampaignInput:
    """Pre-CP8 registration inputs, visibly limited to contract fixtures."""

    plan: ContractFixturePlan
    s3_specs: tuple[object, ...]
    s4_specs: tuple[object, ...]
    guard_policy: ResearchDbPolicy
    strategy_name: str = "rob974-h6b-contract-fixture"
    timeframe: str = "4h"
    runner: str = "rob974-h6b-contract-fixture"
    provenance: str = "contract_fixture"

    def __post_init__(self) -> None:
        if type(self.plan) is not ContractFixturePlan:
            raise H6BPlanError("fixture campaign plan must use the exact type")
        if type(self.s3_specs) is not tuple or len(self.s3_specs) != 24:
            raise H6BPlanError(
                "fixture S3 registration specs must be an exact 24-tuple"
            )
        if type(self.s4_specs) is not tuple or len(self.s4_specs) != 24:
            raise H6BPlanError(
                "fixture S4 registration specs must be an exact 24-tuple"
            )
        if type(self.guard_policy) is not ResearchDbPolicy:
            raise H6BPlanError("guard_policy must use the exact ResearchDbPolicy type")
        for name in ("strategy_name", "timeframe", "runner"):
            _exact_nonempty_str(getattr(self, name), name)
        if self.provenance != "contract_fixture":
            raise H6BPlanError("pre-CP8 campaign input must be contract_fixture")


SessionFactory = Callable[[], object]
RegisterExperimentsFn = Callable[..., Awaitable[list[Any]]]
RunH4AttemptsFn = Callable[
    [ContractFixturePlan], Awaitable[tuple[H6AAttemptBatchItem, ...]]
]
FindExistingTrialFn = Callable[..., Awaitable[object | None]]
RecordTrialFn = Callable[..., Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ContractFixtureExecutionPorts:
    """Typed CP1-CP7 seams; no real engine or production H4/H5 is implied."""

    session_factory: SessionFactory
    register_experiments_fn: RegisterExperimentsFn
    run_h4_attempts_fn: RunH4AttemptsFn
    find_existing_trial_fn: FindExistingTrialFn
    record_trial_fn: RecordTrialFn
    h6a_accounting: H6AAccountingPort
    h5: H5CompositionPort
    artifacts: ArtifactPairPort
    state_inspector: CampaignStateInspector | None = None
    diagnostics: DiagnosticCapturePort | None = None
    provenance: str = "contract_fixture"

    def __post_init__(self) -> None:
        for name in (
            "session_factory",
            "register_experiments_fn",
            "run_h4_attempts_fn",
            "find_existing_trial_fn",
            "record_trial_fn",
        ):
            if not callable(getattr(self, name)):
                raise H6BPlanError(f"execution port {name} must be callable")
        if getattr(self.h6a_accounting, "provenance", None) != "actual_merged_h6a":
            raise H6BPlanError("accounting port must identify actual merged H6-A")
        if not callable(getattr(self.h6a_accounting, "reconstruct", None)):
            raise H6BPlanError("accounting port lacks reconstruct")
        if getattr(self.h5, "provenance", None) != "contract_fixture":
            raise H6BPlanError("pre-CP8 H5 port must be contract_fixture")
        for name in (
            "build_scorecard",
            "canonical_json_bytes",
            "semantic_hash",
            "render_markdown",
        ):
            if not callable(getattr(self.h5, name, None)):
                raise H6BPlanError(f"H5 port lacks {name}")
        if (
            getattr(self.artifacts, "provenance", None)
            != "rob974_h6b_directory_atomic_v1"
        ):
            raise H6BPlanError("artifact port is not the H6-B directory primitive")
        if not callable(getattr(self.artifacts, "stage", None)) or not callable(
            getattr(self.artifacts, "publish", None)
        ):
            raise H6BPlanError("artifact port lacks stage/publish")
        if self.state_inspector is not None:
            if getattr(self.state_inspector, "provenance", None) != "contract_fixture":
                raise H6BPlanError("pre-CP8 state inspector must be contract_fixture")
            if not callable(getattr(self.state_inspector, "inspect", None)):
                raise H6BPlanError("state inspector lacks inspect")
        if self.diagnostics is not None:
            if (
                getattr(self.diagnostics, "provenance", None)
                != "actual_merged_rob970_h6a"
            ):
                raise H6BPlanError("diagnostic port is not merged ROB-970/H6-A")
            if not callable(getattr(self.diagnostics, "capture_live_exception", None)):
                raise H6BPlanError("diagnostic port lacks live capture")
        if self.provenance != "contract_fixture":
            raise H6BPlanError("pre-CP8 execution ports must be contract_fixture")


class CommitRejectedError(RuntimeError):
    """Adapter assertion that COMMIT was rejected before confirmed success."""


class PredecessorTransactionOwnershipError(RuntimeError):
    """A predecessor attempted to own H6-B's transaction lifecycle."""


class ReplayCollisionError(RuntimeError):
    """DB/artifact state is asymmetric, partial, stale, or non-canonical."""


@dataclass(frozen=True, slots=True)
class CoordinatorCounters:
    session_factory: int
    begin: int
    register: int
    h4: int
    record: int
    accounting: int
    h5: int
    stage: int
    rollback: int
    commit: int
    publish: int
    close: int
    db_inspect: int = 0
    artifact_probe: int = 0
    replay_verify: int = 0
    delete: int = 0


@dataclass(frozen=True, slots=True)
class MaterializationOutcome:
    """Closed application outcome with primary and secondary failures separated."""

    exit_code: int
    disposition: str
    trace: tuple[str, ...]
    counters: CoordinatorCounters
    primary_error: BaseException | None
    rollback_error: BaseException | None
    close_error: BaseException | None
    rollback_outcome: str
    close_outcome: str
    commit_confirmed: bool
    retry_forbidden: bool
    staged_pair: object | None
    published_pair: object | None
    accounting: object | None
    scorecard: dict[str, object] | None
    db_state: str
    artifact_state: str
    replay_inspection: object | None
    diagnostic_capture: H6BDiagnosticCapture | None
    diagnostic_capture_error: BaseException | None


@dataclass(slots=True)
class _CoordinatorState:
    trace: list[str] = field(default_factory=list)
    session_factory: int = 0
    begin: int = 0
    register: int = 0
    h4: int = 0
    record: int = 0
    accounting_calls: int = 0
    h5: int = 0
    stage: int = 0
    rollback: int = 0
    commit: int = 0
    publish: int = 0
    close: int = 0
    db_inspect: int = 0
    artifact_probe: int = 0
    replay_verify: int = 0
    delete: int = 0
    rollback_error: BaseException | None = None
    close_error: BaseException | None = None
    rollback_outcome: str = "NOT_ATTEMPTED"
    close_outcome: str = "NOT_ATTEMPTED"
    commit_confirmed: bool = False
    staged_pair: object | None = None
    published_pair: object | None = None
    accounting_report: object | None = None
    scorecard: dict[str, object] | None = None
    db_state: str = "NOT_INSPECTED"
    artifact_state: str = "NOT_INSPECTED"
    replay_inspection: object | None = None
    diagnostic_capture: H6BDiagnosticCapture | None = None
    diagnostic_capture_error: BaseException | None = None

    def counters(self) -> CoordinatorCounters:
        return CoordinatorCounters(
            session_factory=self.session_factory,
            begin=self.begin,
            register=self.register,
            h4=self.h4,
            record=self.record,
            accounting=self.accounting_calls,
            h5=self.h5,
            stage=self.stage,
            rollback=self.rollback,
            commit=self.commit,
            publish=self.publish,
            close=self.close,
            db_inspect=self.db_inspect,
            artifact_probe=self.artifact_probe,
            replay_verify=self.replay_verify,
            delete=self.delete,
        )


class _InjectedTransactionSession:
    """Delegate view that forwards DB work but poisons lifecycle ownership."""

    __slots__ = ("__session",)

    def __init__(self, session: object) -> None:
        object.__setattr__(self, "_InjectedTransactionSession__session", session)

    def __getattr__(self, name: str) -> object:
        if name in {"begin", "commit", "rollback", "close"}:
            raise PredecessorTransactionOwnershipError(
                f"predecessor attempted forbidden session.{name} ownership"
            )
        return getattr(self.__session, name)


def _outcome(
    state: _CoordinatorState,
    *,
    exit_code: int,
    disposition: str,
    primary_error: BaseException | None,
    retry_forbidden: bool,
) -> MaterializationOutcome:
    return MaterializationOutcome(
        exit_code=exit_code,
        disposition=disposition,
        trace=tuple(state.trace),
        counters=state.counters(),
        primary_error=primary_error,
        rollback_error=state.rollback_error,
        close_error=state.close_error,
        rollback_outcome=state.rollback_outcome,
        close_outcome=state.close_outcome,
        commit_confirmed=state.commit_confirmed,
        retry_forbidden=retry_forbidden,
        staged_pair=state.staged_pair,
        published_pair=state.published_pair,
        accounting=state.accounting_report,
        scorecard=state.scorecard,
        db_state=state.db_state,
        artifact_state=state.artifact_state,
        replay_inspection=state.replay_inspection,
        diagnostic_capture=state.diagnostic_capture,
        diagnostic_capture_error=state.diagnostic_capture_error,
    )


def _attach_cancellation_outcome(
    exc: BaseException, outcome: MaterializationOutcome
) -> None:
    try:
        exc.rob984_materialization_outcome = outcome
    except Exception:
        exc.add_note(
            "ROB-984 materialization outcome attachment unavailable; "
            f"last disposition={outcome.disposition}"
        )


def _capture_materializer_exception(
    state: _CoordinatorState,
    ports: ContractFixtureExecutionPorts,
    exc: BaseException,
) -> None:
    """Capture once at the first H6-B catch; capture failure is secondary."""
    if (
        state.diagnostic_capture is not None
        or state.diagnostic_capture_error is not None
    ):
        return
    if ports.diagnostics is None:
        return
    try:
        captured = ports.diagnostics.capture_live_exception(
            exc,
            catch_boundary="materializer",
            strategy="H6B",
            config_id="ROB-984",
        )
        if type(captured) is not H6BDiagnosticCapture:
            raise H6BPlanError("diagnostic port returned a non-canonical capture")
        state.diagnostic_capture = captured
    except BaseException as capture_error:
        state.diagnostic_capture_error = capture_error


def render_safe_materialization_failure(outcome: MaterializationOutcome) -> bytes:
    """Render bounded operator evidence without stringifying raw exceptions."""
    if type(outcome) is not MaterializationOutcome:
        raise TypeError("outcome must be exact MaterializationOutcome")
    diagnostic = (
        outcome.diagnostic_capture.to_safe_payload()
        if outcome.diagnostic_capture is not None
        else {
            "capture": "unavailable",
            "capture_error_type": (
                type(outcome.diagnostic_capture_error).__name__
                if outcome.diagnostic_capture_error is not None
                else None
            ),
        }
    )
    payload = {
        "schema_version": "rob974_h6b_materializer_failure.v1",
        "exit_code": outcome.exit_code,
        "disposition": outcome.disposition,
        "commit_confirmed": outcome.commit_confirmed,
        "retry_forbidden": outcome.retry_forbidden,
        "rollback_outcome": outcome.rollback_outcome,
        "close_outcome": outcome.close_outcome,
        "primary_error_type": (
            type(outcome.primary_error).__name__
            if outcome.primary_error is not None
            else None
        ),
        "rollback_error_type": (
            type(outcome.rollback_error).__name__
            if outcome.rollback_error is not None
            else None
        ),
        "close_error_type": (
            type(outcome.close_error).__name__
            if outcome.close_error is not None
            else None
        ),
        "diagnostic": diagnostic,
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


async def _attempt_rollback(state: _CoordinatorState, session: object) -> None:
    state.trace.append("rollback")
    state.rollback += 1
    try:
        await session.rollback()
    except BaseException as exc:
        state.rollback_error = exc
        state.rollback_outcome = "FAILED"
        if not isinstance(exc, Exception):
            raise
    else:
        state.rollback_outcome = "SUCCEEDED"


async def _attempt_close(state: _CoordinatorState, session: object) -> None:
    state.trace.append("session_close")
    state.close += 1
    try:
        await session.close()
    except BaseException as exc:
        state.close_error = exc
        state.close_outcome = "FAILED"
        if not isinstance(exc, Exception):
            raise
    else:
        state.close_outcome = "SUCCEEDED"


def _validate_fixture_preflight(
    *,
    plan: ContractFixturePlan,
    campaign: ContractFixtureCampaignInput,
    ports: ContractFixtureExecutionPorts,
    output_dir: Path,
) -> None:
    if type(plan) is not ContractFixturePlan:
        raise H6BPreflightRefused("plan must be exact ContractFixturePlan")
    if type(campaign) is not ContractFixtureCampaignInput or campaign.plan is not plan:
        raise H6BPreflightRefused("campaign input is not bound to this exact plan")
    if type(ports) is not ContractFixtureExecutionPorts:
        raise H6BPreflightRefused("execution ports must use the exact fixture type")
    if not isinstance(output_dir, Path) or not output_dir.is_absolute():
        raise H6BPreflightRefused("output_dir must be an absolute Path")
    if plan.to_payload()["status"] != "NOT_LAUNCHABLE_CONTRACT_FIXTURE":
        raise H6BPreflightRefused("fixture launchability marker drifted")
    validate_exact_48_mapping(plan.ordered_mapping)


def _validate_attempt_batch(
    plan: ContractFixturePlan, attempts: tuple[H6AAttemptBatchItem, ...]
) -> None:
    if type(attempts) is not tuple or len(attempts) != 48:
        raise H6BPreflightRefused("H4 fixture must return an exact 48-attempt tuple")
    if any(type(item) is not H6AAttemptBatchItem for item in attempts):
        raise H6BPreflightRefused("H4 attempts must use exact H6AAttemptBatchItem")
    expected = tuple(row_id for row_id, _experiment_id in plan.ordered_mapping)
    if tuple(item.row_id for item in attempts) != expected:
        raise H6BPreflightRefused("H4 attempt order differs from the exact plan")
    if any(item.retry_index != 0 for item in attempts):
        raise H6BPreflightRefused(
            "primary materialization batch cannot contain retries"
        )


def _registered_pk_mapping(
    *, plan: ContractFixturePlan, registered: Sequence[object]
) -> dict[str, int]:
    if len(registered) != 48:
        raise H6BPreflightRefused("H6-A registration did not return exactly 48 rows")
    result: dict[str, int] = {}
    for (row_id, _experiment_id), row in zip(
        plan.ordered_mapping, registered, strict=True
    ):
        primary_key = getattr(row, "id", None)
        if type(primary_key) is not int or primary_key <= 0:
            raise H6BPreflightRefused(
                "registered experiment primary keys must be positive built-in ints"
            )
        result[row_id] = primary_key
    if len(set(result.values())) != 48:
        raise H6BPreflightRefused("registered experiment primary keys must be unique")
    return result


def _validate_replay_ports(ports: ContractFixtureExecutionPorts) -> None:
    if ports.state_inspector is None:
        raise H6BPreflightRefused("CP4 requires an explicit state inspector")
    for name in ("probe", "inspect"):
        if not callable(getattr(ports.artifacts, name, None)):
            raise H6BPreflightRefused(f"artifact port lacks read-only {name}")


def _validate_exact_db_snapshot(
    *,
    plan: ContractFixturePlan,
    snapshot: CampaignDbSnapshot,
    accounting: object,
) -> None:
    if snapshot.campaign_run_id != plan._fixture_run_id:
        raise ReplayCollisionError("persisted campaign belongs to a wrong run")
    if snapshot.registered_mapping != plan.ordered_mapping:
        raise ReplayCollisionError(
            "persisted registration is partial, reordered, or outside the plan"
        )
    if snapshot.mismatch_row_ids:
        raise ReplayCollisionError("persisted registration carries mismatched row IDs")
    if snapshot.out_of_plan_experiment_ids:
        raise ReplayCollisionError("persisted state carries out-of-plan experiments")
    required_report_values = {
        "campaign_run_id": plan._fixture_run_id,
        "expected_total": 48,
        "registered_total": 48,
        "primary_attempts": 48,
        "total_attempts": 48,
        "retry_attempts": 0,
        "accounting_complete": True,
    }
    for name, expected in required_report_values.items():
        if getattr(accounting, name, object()) != expected:
            raise ReplayCollisionError(
                f"H6-A reconstructed accounting field {name} is not exact"
            )
    for name in (
        "missing_row_ids",
        "extra_experiment_ids",
        "mismatch_row_ids",
        "duplicate_or_gap_row_ids",
    ):
        if getattr(accounting, name, object()) != ():
            raise ReplayCollisionError(
                f"H6-A reconstructed accounting carries non-empty {name}"
            )


async def _finish_replay_noop(
    *, state: _CoordinatorState, session: object
) -> MaterializationOutcome:
    primary_error: BaseException | None = None
    native_interrupt: BaseException | None = None
    try:
        await _attempt_rollback(state, session)
    except BaseException as exc:
        native_interrupt = exc
    if state.rollback_error is None:
        exit_code = MATERIALIZED_EXIT
        disposition = "REPLAY_NOOP"
        retry_forbidden = False
    else:
        primary_error = state.rollback_error
        exit_code = PRECOMMIT_FAILURE
        disposition = "PRECOMMIT_FAILURE"
        retry_forbidden = True
    try:
        await _attempt_close(state, session)
    except BaseException as exc:
        native_interrupt = native_interrupt or exc
    if state.close_error is not None and exit_code == MATERIALIZED_EXIT:
        exit_code = SESSION_CLOSE_FAILURE
        disposition = "REPLAY_NOOP_CLOSE_FAILED"
        retry_forbidden = True
    outcome = _outcome(
        state,
        exit_code=exit_code,
        disposition=disposition,
        primary_error=primary_error,
        retry_forbidden=retry_forbidden,
    )
    if native_interrupt is not None:
        _attach_cancellation_outcome(native_interrupt, outcome)
        raise native_interrupt
    return outcome


async def materialize_contract_fixture(
    *,
    plan: ContractFixturePlan,
    authorization: IssuedOneShotAuthorization,
    campaign: ContractFixtureCampaignInput,
    ports: ContractFixtureExecutionPorts,
    output_dir: Path,
) -> MaterializationOutcome:
    """CP3 compatibility entry point for explicitly call-spy-only fixtures."""
    return await _materialize_contract_fixture(
        plan=plan,
        authorization=authorization,
        campaign=campaign,
        ports=ports,
        output_dir=output_dir,
        require_state_inspection=False,
    )


async def materialize_or_replay_contract_fixture(
    *,
    plan: ContractFixturePlan,
    authorization: IssuedOneShotAuthorization,
    campaign: ContractFixtureCampaignInput,
    ports: ContractFixtureExecutionPorts,
    output_dir: Path,
) -> MaterializationOutcome:
    """CP4 two-sided classifier; mutation follows only dual absence."""
    return await _materialize_contract_fixture(
        plan=plan,
        authorization=authorization,
        campaign=campaign,
        ports=ports,
        output_dir=output_dir,
        require_state_inspection=True,
    )


async def _materialize_contract_fixture(
    *,
    plan: ContractFixturePlan,
    authorization: IssuedOneShotAuthorization,
    campaign: ContractFixtureCampaignInput,
    ports: ContractFixtureExecutionPorts,
    output_dir: Path,
    require_state_inspection: bool,
) -> MaterializationOutcome:
    """Compose CP1-CP7 seams with H6-B as the sole transaction owner.

    This entry point is test-only contract-fixture wiring and is unreachable
    from ``--run``.  It nevertheless exercises the real merged H6-A register
    and record functions and the actual H6-A accounting adapter.
    """
    state = _CoordinatorState(trace=["preflight"])
    try:
        _validate_fixture_preflight(
            plan=plan, campaign=campaign, ports=ports, output_dir=output_dir
        )
        register_context, record_context = build_h6a_mutation_contexts(authorization)
        validate_h6a_context_pair(register_context, record_context)
        if type(require_state_inspection) is not bool:
            raise H6BPreflightRefused("state-inspection flag must be exact bool")
        if require_state_inspection:
            _validate_replay_ports(ports)
            state.trace.append("artifact_probe")
            state.artifact_probe += 1
            presence = ports.artifacts.probe(output_dir=output_dir)
            state.artifact_state = getattr(presence, "state", "MALFORMED")
            if state.artifact_state not in {
                "ABSENT",
                "PAIR_PRESENT",
                "INVALID_FINAL",
                "STALE_STAGING",
            }:
                raise H6BPreflightRefused("artifact probe returned a malformed state")
            if state.artifact_state in {"INVALID_FINAL", "STALE_STAGING"}:
                raise ReplayCollisionError(
                    f"artifact forensic state refused: {state.artifact_state}"
                )
    except BaseException as exc:
        if type(ports) is ContractFixtureExecutionPorts:
            _capture_materializer_exception(state, ports, exc)
        if not isinstance(exc, Exception):
            outcome = _outcome(
                state,
                exit_code=AUTHORITY_OR_PREFLIGHT_REFUSED,
                disposition="AUTHORITY_OR_PREFLIGHT_REFUSED",
                primary_error=exc,
                retry_forbidden=isinstance(exc, ReplayCollisionError),
            )
            _attach_cancellation_outcome(exc, outcome)
            raise
        return _outcome(
            state,
            exit_code=AUTHORITY_OR_PREFLIGHT_REFUSED,
            disposition="AUTHORITY_OR_PREFLIGHT_REFUSED",
            primary_error=exc,
            retry_forbidden=isinstance(exc, ReplayCollisionError),
        )

    session: object | None = None
    primary_error: BaseException | None = None
    exit_code = PRECOMMIT_FAILURE
    disposition = "PRECOMMIT_FAILURE"
    retry_forbidden = False
    begun = False
    native_interrupt: BaseException | None = None

    try:
        state.trace.append("session_factory")
        state.session_factory += 1
        session = ports.session_factory()
        if session is None or isinstance(session, Awaitable):
            raise H6BPreflightRefused(
                "session factory must synchronously return one session"
            )

        state.trace.append("begin")
        state.begin += 1
        await session.begin()
        begun = True

        predecessor_session = _InjectedTransactionSession(session)
        mapping = dict(plan.ordered_mapping)

        if require_state_inspection:
            assert ports.state_inspector is not None
            state.trace.append("db_state_inspection")
            state.db_inspect += 1
            snapshot = await ports.state_inspector.inspect(
                predecessor_session, plan=plan
            )
            if type(snapshot) is not CampaignDbSnapshot:
                raise ReplayCollisionError(
                    "state inspector returned a non-canonical snapshot type"
                )
            if snapshot.is_absent():
                state.db_state = "ABSENT"
                if state.artifact_state != "ABSENT":
                    raise ReplayCollisionError(
                        "artifact pair exists while canonical DB state is absent"
                    )
            else:
                state.db_state = "PRESENT_UNVERIFIED"
                state.trace.append("h6a_accounting")
                state.accounting_calls += 1
                state.accounting_report = ports.h6a_accounting.reconstruct(
                    plan=plan,
                    registered_total=len(snapshot.registered_mapping),
                    attempts=snapshot.attempts,
                )
                _validate_exact_db_snapshot(
                    plan=plan,
                    snapshot=snapshot,
                    accounting=state.accounting_report,
                )
                state.db_state = "EXACT"
                if state.artifact_state != "PAIR_PRESENT":
                    raise ReplayCollisionError(
                        "canonical DB state exists while artifact pair is absent"
                    )
                state.trace.append("h5_scorecard")
                state.h5 += 1
                replay_scorecard = ports.h5.build_scorecard(
                    plan=plan,
                    attempts=snapshot.attempts,
                    accounting=state.accounting_report,
                )
                if type(replay_scorecard) is not dict:
                    raise ReplayCollisionError(
                        "H5 replay scorecard must be an exact built-in dict"
                    )
                state.scorecard = replay_scorecard
                state.trace.append("artifact_replay_verify")
                state.replay_verify += 1
                state.replay_inspection = ports.artifacts.inspect(
                    scorecard=replay_scorecard,
                    output_dir=output_dir,
                    h5_port=ports.h5,
                )
                return await _finish_replay_noop(state=state, session=session)

        state.trace.append("h6a_register")
        state.register += 1
        registered_s3, registered_s4 = await register_h6a_campaign(
            predecessor_session,
            approved=register_context,
            full_campaign_hash=plan._fixture_campaign_hash,
            campaign_run_id=plan._fixture_run_id,
            s3_specs=list(campaign.s3_specs),
            s4_specs=list(campaign.s4_specs),
            row_id_to_experiment_id=mapping,
            guard_opt_in_enabled=True,
            guard_policy=campaign.guard_policy,
            register_experiments_fn=ports.register_experiments_fn,
        )
        registered = (*registered_s3, *registered_s4)
        pk_mapping = _registered_pk_mapping(plan=plan, registered=registered)

        state.trace.append("h4_attempts")
        state.h4 += 1
        attempts = await ports.run_h4_attempts_fn(plan)
        _validate_attempt_batch(plan, attempts)

        state.trace.append("h6a_record")
        state.record += 1
        await record_h6a_attempts(
            predecessor_session,
            approved=record_context,
            full_campaign_hash=plan._fixture_campaign_hash,
            campaign_run_id=plan._fixture_run_id,
            row_id_to_experiment_id=mapping,
            row_id_to_experiment_pk=pk_mapping,
            attempts=attempts,
            strategy_name=campaign.strategy_name,
            timeframe=campaign.timeframe,
            runner=campaign.runner,
            guard_opt_in_enabled=True,
            guard_policy=campaign.guard_policy,
            find_existing_trial_fn=ports.find_existing_trial_fn,
            record_trial_fn=ports.record_trial_fn,
        )

        state.trace.append("h6a_accounting")
        state.accounting_calls += 1
        state.accounting_report = ports.h6a_accounting.reconstruct(
            plan=plan, registered_total=len(registered), attempts=attempts
        )

        state.trace.append("h5_scorecard")
        state.h5 += 1
        scorecard = ports.h5.build_scorecard(
            plan=plan, attempts=attempts, accounting=state.accounting_report
        )
        if type(scorecard) is not dict:
            raise H6BPreflightRefused("H5 scorecard must be an exact built-in dict")
        state.scorecard = scorecard

        state.trace.append("artifact_stage")
        state.stage += 1
        state.staged_pair = ports.artifacts.stage(
            scorecard=scorecard, output_dir=output_dir, h5_port=ports.h5
        )

        state.trace.append("db_commit")
        state.commit += 1
        try:
            await session.commit()
        except BaseException as exc:
            _capture_materializer_exception(state, ports, exc)
            primary_error = exc
            if not isinstance(exc, Exception):
                native_interrupt = exc
            retry_forbidden = True
            if isinstance(exc, CommitRejectedError):
                disposition = "COMMIT_FAILED"
            else:
                disposition = "COMMIT_OUTCOME_UNKNOWN"
            exit_code = COMMIT_FAILED_OR_UNKNOWN
            try:
                await _attempt_rollback(state, session)
            except BaseException as rollback_interrupt:
                native_interrupt = rollback_interrupt
        else:
            state.commit_confirmed = True
            state.trace.append("artifact_publish")
            state.publish += 1
            try:
                state.published_pair = ports.artifacts.publish(
                    state.staged_pair, h5_port=ports.h5
                )
            except BaseException as exc:
                _capture_materializer_exception(state, ports, exc)
                primary_error = exc
                if not isinstance(exc, Exception):
                    native_interrupt = exc
                exit_code = POSTCOMMIT_PUBLISH_FAILURE
                disposition = "DB_DURABLE_ARTIFACT_UNPUBLISHED"
                retry_forbidden = True
            else:
                exit_code = MATERIALIZED_EXIT
                disposition = "MATERIALIZED"
                retry_forbidden = False
    except BaseException as exc:
        _capture_materializer_exception(state, ports, exc)
        if primary_error is None:
            primary_error = exc
        if isinstance(exc, ReplayCollisionError) or (
            require_state_inspection
            and (state.db_state != "ABSENT" or state.artifact_state != "ABSENT")
        ):
            retry_forbidden = True
        if not isinstance(exc, Exception):
            native_interrupt = exc
        if exit_code not in (COMMIT_FAILED_OR_UNKNOWN, POSTCOMMIT_PUBLISH_FAILURE):
            exit_code = PRECOMMIT_FAILURE
            disposition = "PRECOMMIT_FAILURE"
            if begun:
                try:
                    await _attempt_rollback(state, session)
                except BaseException as rollback_interrupt:
                    native_interrupt = rollback_interrupt

    if session is None:
        outcome = _outcome(
            state,
            exit_code=exit_code,
            disposition=disposition,
            primary_error=primary_error,
            retry_forbidden=retry_forbidden,
        )
        if native_interrupt is not None:
            _attach_cancellation_outcome(native_interrupt, outcome)
            raise native_interrupt
        return outcome
    try:
        await _attempt_close(state, session)
    except BaseException as exc:
        _capture_materializer_exception(state, ports, exc)
        if not isinstance(exc, Exception):
            native_interrupt = native_interrupt or exc
    if state.close_error is not None:
        _capture_materializer_exception(state, ports, state.close_error)

    if state.close_error is not None and exit_code == MATERIALIZED_EXIT:
        exit_code = SESSION_CLOSE_FAILURE
        disposition = "MATERIALIZED_CLOSE_FAILED"
        retry_forbidden = True

    outcome = _outcome(
        state,
        exit_code=exit_code,
        disposition=disposition,
        primary_error=primary_error,
        retry_forbidden=retry_forbidden,
    )
    if native_interrupt is not None:
        _attach_cancellation_outcome(native_interrupt, outcome)
        raise native_interrupt
    return outcome
