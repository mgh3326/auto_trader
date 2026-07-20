"""ROB-984 H6-B composition boundary.

CP1 supplies the immutable plan/preflight vocabulary and the sole issuer for
H6-A mutation contexts.  Transaction and filesystem behavior is added by the
later checkpoints in this same module; no predecessor owns either concern.

The pre-H4/H5 plan is deliberately a ``contract_fixture``.  Its private H6-A
fixture identity is usable by call-spy tests, but is never rendered as a
production full-campaign hash or run id and is never accepted by ``--run``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.services.rob974_h6a_bridge import (
    RECORD_ATTEMPTS_OPERATION_KIND,
    REGISTER_CAMPAIGN_OPERATION_KIND,
    ApprovedMutationContext,
    compute_exact_48_mapping_hash,
    derive_campaign_run_id,
)

__all__ = [
    "AUTHORITY_OR_PREFLIGHT_REFUSED",
    "CANONICAL_ROW_ORDER",
    "CLI_USAGE_OR_PLAN_ERROR",
    "COMMIT_FAILED_OR_UNKNOWN",
    "ContractFixturePlan",
    "DatabaseTarget",
    "EXIT_DISPOSITION_TABLE",
    "ExactSourcePins",
    "H6BPlanError",
    "H6BPreflightRefused",
    "IssuedOneShotAuthorization",
    "MATERIALIZED_EXIT",
    "POSTAUDIT_FAILURE",
    "POSTCOMMIT_PUBLISH_FAILURE",
    "PRECOMMIT_FAILURE",
    "ProductionExecutionPlan",
    "RunAuthority",
    "SESSION_CLOSE_FAILURE",
    "build_h6a_mutation_contexts",
    "issue_contract_fixture_authorization",
    "issue_run_authorization",
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
        raise H6BPlanError(
            "mapping order must be exactly S3-00..S3-23,S4-00..S4-23"
        )
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
        if type(self) is not IssuedOneShotAuthorization or self._issuer is not _ISSUER_SEAL:
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
    if type(register) is not ApprovedMutationContext or type(record) is not ApprovedMutationContext:
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
