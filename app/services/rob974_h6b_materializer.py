"""ROB-984 H6-B composition boundary.

CP1 supplies the immutable plan/preflight vocabulary and the sole issuer for
H6-A mutation contexts.  Transaction and filesystem behavior is added by the
later checkpoints in this same module; no predecessor owns either concern.

The retained CP1-CP7 plan is deliberately a ``contract_fixture``.  Its private
H6-A fixture identity remains usable only by call-spy regressions.  CP8 adds a
separately sealed production identity and execution path over the actual
merged H4, H5, and H6-A APIs; fixture provenance is rejected there.
"""

from __future__ import annotations

import json
import math
import re
import sys
import types
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import rob974_h2_h1_bridge as h2_h1_bridge
import rob974_h2_ingress as h2_ingress
import rob974_h2_scenarios as h2_scenarios
import rob974_h3_evidence as h3_evidence
import rob974_h3_h2_adapter as h3_h2_adapter
import rob974_h3_manifest as h3_manifest
import rob974_h3_s3 as h3_s3
import rob974_h3_s4 as h3_s4
import rob974_h4_adapter as h4_adapter
import rob974_h4_contracts as h4_contracts
import rob974_h4_h6a_adapter as h4_h6a_adapter
import rob974_h4_pbo as h4_pbo
import rob974_h4_runner as h4_runner
import rob974_h4_selection as h4_selection
import rob974_h5_canonical as h5_canonical
import rob974_h5_contracts as h5_contracts
import rob974_h5_dual_evidence as h5_dual
import rob974_h5_gates as h5_gates
import rob974_h5_markdown as h5_markdown
import rob974_h5_s3 as h5_s3
import rob974_h5_s4 as h5_s4
import rob974_h6a_accounting as h6a_accounting
import rob974_h6a_evidence as h6a_evidence
import rob974_lineage as h1_lineage
from rob974_features import FOUR_HOUR_MS, MinuteBar
from sqlalchemy import or_, select, text

from app.models.research_backtest import (
    ResearchBacktestRun,
    ResearchStrategyExperiment,
)
from app.schemas.research_backtest import StrategyExperimentIdentity
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
from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "AUTHORITY_OR_PREFLIGHT_REFUSED",
    "ActualH4CampaignResult",
    "ActualH4InputData",
    "ActualH4RunnerPort",
    "ActualCampaignStateInspector",
    "ActualMergedH5Composition",
    "ActualMergedH4Runner",
    "ActualMergedH6AAccounting",
    "CANONICAL_ROW_ORDER",
    "CLI_USAGE_OR_PLAN_ERROR",
    "COMMIT_FAILED_OR_UNKNOWN",
    "CommitRejectedError",
    "CampaignDbSnapshot",
    "CampaignStateInspector",
    "ContractFixtureCampaignInput",
    "ContractFixtureClosureEvidence",
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
    "ProductionCampaignInput",
    "ProductionExecutionPlan",
    "ProductionExecutionPorts",
    "ProductionIdentityPlan",
    "ProjectTestDbAuthority",
    "RunAuthority",
    "ReplayCollisionError",
    "SESSION_CLOSE_FAILURE",
    "build_h6a_mutation_contexts",
    "build_contract_fixture_closure_evidence",
    "build_h4_member_trade_key",
    "build_production_execution_plan",
    "build_production_identity_plan",
    "parse_persisted_attempt_record",
    "issue_contract_fixture_authorization",
    "issue_project_test_db_authorization",
    "issue_run_authorization",
    "materialize_contract_fixture",
    "materialize_or_replay_contract_fixture",
    "materialize_production",
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
_GIT_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_EMPIRICAL_DATABASE = "rob974_db"
_PROJECT_TEST_DB_TARGET = ("localhost", 5432, "test_db", "postgres")
_PROJECT_TEST_DB_APPROVAL = "ROB984_CP9_ORCH_PROJECT_TEST_DB"


class H6BPlanError(ValueError):
    """The pure plan or an exact-type plan value is malformed."""


class H6BPreflightRefused(ValueError):
    """A run authority differs from its independently frozen plan/target."""


def _hex64(value: object, name: str) -> str:
    if type(value) is not str or _HEX64_RE.fullmatch(value) is None:
        raise H6BPlanError(f"{name} must be exact built-in lowercase hex64")
    return value


def _git_oid(value: object, name: str) -> str:
    if type(value) is not str or _GIT_OID_RE.fullmatch(value) is None:
        raise H6BPlanError(f"{name} must be an exact lowercase SHA-1 Git OID")
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


_PRODUCTION_IDENTITY_SEAL = object()


@dataclass(frozen=True, slots=True)
class ProductionIdentityPlan:
    """Pure projection of the actual merged H4/H6-A production identity.

    The H4 adapter independently re-hashes its closed source inventories and
    H6-A derives the exact row IDs, full-campaign hash, and primary run ID.
    H6-B only validates and renders those predecessor-owned values.
    """

    full_campaign_hash: str
    campaign_run_id: str
    ordered_mapping: tuple[tuple[str, str], ...]
    exact_48_mapping_hash: str
    source_pins: object
    h4_source_pins: object
    _h4_plan: object = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _PRODUCTION_IDENTITY_SEAL:
            raise H6BPlanError("production identity was not issued by the H4 adapter")
        if type(self._h4_plan) is not h4_h6a_adapter.ProductionH4Plan:
            raise H6BPlanError("production identity lacks exact ProductionH4Plan")
        if self.source_pins is not self._h4_plan.source_pins:
            raise H6BPlanError("production source pins are not the H4 plan pins")
        if self.h4_source_pins is not self._h4_plan.h4_source_pins:
            raise H6BPlanError("H4 source pins are not the H4 plan pins")
        self.source_pins.require_production_ready()
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
            raise H6BPlanError("production identity exact-48 mapping hash mismatch")
        expected_mapping = tuple(
            (spec.row_id, spec.experiment_id) for spec in self._h4_plan.row_specs
        )
        if self.ordered_mapping != expected_mapping:
            raise H6BPlanError("production mapping differs from the actual H4 plan")
        if self.full_campaign_hash != self._h4_plan.full_campaign_hash:
            raise H6BPlanError("production full-campaign hash differs from H4")
        if self.campaign_run_id != self._h4_plan.campaign_run_id:
            raise H6BPlanError("production campaign run ID differs from H4")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": "rob974_h6b_plan.v2",
            "status": "PRODUCTION_IDENTITY_READY",
            "predecessor_mode": "actual_merged_h4_h5",
            "actual_h4_contract": "PASS",
            "actual_h5_contract": "PASS",
            "actual_h6a_contract_scope": "typed_integration_only",
            "production_identity": "ACTUAL_H4_SOURCE_PINS_COMPLETE",
            "launchability": "EXACT_TARGET_AND_ONE_SHOT_PREFLIGHT_REQUIRED",
            "full_campaign_hash": self.full_campaign_hash,
            "campaign_run_id": self.campaign_run_id,
            "ordered_mapping": [
                {"row_id": row_id, "experiment_id": experiment_id}
                for row_id, experiment_id in self.ordered_mapping
            ],
            "exact_48_mapping_hash": self.exact_48_mapping_hash,
            "source_pins": self.source_pins.as_dict(),
            "h4_source_pins": {
                "runner_bundle_sha256": (self.h4_source_pins.runner_bundle_sha256),
                "pbo_source_sha256": self.h4_source_pins.pbo_source_sha256,
            },
            "exit_disposition_table": [
                {
                    "exit": code,
                    "dispositions": list(dispositions),
                    "meaning": meaning,
                }
                for code, dispositions, meaning in EXIT_DISPOSITION_TABLE
            ],
        }


def build_production_identity_plan() -> ProductionIdentityPlan:
    """Build twice identically from actual H4 source pins and H6-A identity."""
    h4_plan = h4_h6a_adapter.build_production_h4_plan()
    mapping = tuple((spec.row_id, spec.experiment_id) for spec in h4_plan.row_specs)
    return ProductionIdentityPlan(
        full_campaign_hash=h4_plan.full_campaign_hash,
        campaign_run_id=h4_plan.campaign_run_id,
        ordered_mapping=mapping,
        exact_48_mapping_hash=compute_exact_48_mapping_hash(dict(mapping)),
        source_pins=h4_plan.source_pins,
        h4_source_pins=h4_plan.h4_source_pins,
        _h4_plan=h4_plan,
        _seal=_PRODUCTION_IDENTITY_SEAL,
    )


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
        for name in ("integration_head_sha", "integration_tree_sha"):
            _git_oid(getattr(self, name), name)
        for name in (
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
    _identity: ProductionIdentityPlan = field(repr=False, compare=False)
    _seal: object = field(repr=False)

    def __post_init__(self) -> None:
        if self._seal is not _PRODUCTION_PLAN_SEAL:
            raise H6BPlanError("production plan was not issued by the CP8 adapter")
        if self.provenance != "actual_merged_h4_h5":
            raise H6BPlanError("production plan provenance is not actual H4/H5")
        if type(self._identity) is not ProductionIdentityPlan:
            raise H6BPlanError("production execution plan lacks exact identity plan")
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
        if (
            self.full_campaign_hash != self._identity.full_campaign_hash
            or self.campaign_run_id != self._identity.campaign_run_id
            or self.ordered_mapping != self._identity.ordered_mapping
            or self.exact_48_mapping_hash != self._identity.exact_48_mapping_hash
        ):
            raise H6BPlanError("execution plan identity differs from H4/H6-A plan")


def build_production_execution_plan(
    *,
    identity: ProductionIdentityPlan | ContractFixturePlan,
    output_root: Path,
    integration_head_sha: str,
    integration_tree_sha: str,
) -> ProductionExecutionPlan:
    """Bind a pure production identity to exact integration/output authority."""
    if type(identity) is not ProductionIdentityPlan:
        raise H6BPreflightRefused("actual execution rejects non-production identity")
    if not isinstance(output_root, Path) or not output_root.is_absolute():
        raise H6BPlanError("production output root must be an absolute Path")
    source_pins = ExactSourcePins(
        integration_head_sha=integration_head_sha,
        integration_tree_sha=integration_tree_sha,
        feature_source_sha256=identity.source_pins.feature_source_sha256,
        engine_source_sha256=identity.source_pins.engine_source_sha256,
        runner_source_sha256=identity.source_pins.runner_source_sha256,
        pbo_implementation_sha256=(identity.source_pins.pbo_implementation_sha256),
    )
    return ProductionExecutionPlan(
        full_campaign_hash=identity.full_campaign_hash,
        campaign_run_id=identity.campaign_run_id,
        ordered_mapping=identity.ordered_mapping,
        exact_48_mapping_hash=identity.exact_48_mapping_hash,
        source_pins=source_pins,
        output_root=output_root,
        provenance="actual_merged_h4_h5",
        _identity=identity,
        _seal=_PRODUCTION_PLAN_SEAL,
    )


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


@dataclass(frozen=True, slots=True)
class ProjectTestDbAuthority:
    """Exact CP9-only authority for the reviewed disposable project DB.

    This type is intentionally disjoint from :class:`RunAuthority`: the
    empirical production gate continues to accept only ``rob974_db`` while
    this harness-only issuer accepts exactly the repository/CI ``test_db``
    tuple and cannot be reached from the operator CLI.
    """

    expected_full_campaign_hash: str
    expected_campaign_run_id: str
    expected_exact_48_mapping_hash: str
    expected_target: DatabaseTarget
    observed_target: DatabaseTarget
    inherited_target: DatabaseTarget | None
    write_opt_in: bool
    expected_output_root: Path
    requested_output_root: Path
    expected_source_pins: ExactSourcePins
    observed_source_pins: ExactSourcePins
    one_shot_approval: str
    approval_source: str = _PROJECT_TEST_DB_APPROVAL

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
        if self.approval_source != _PROJECT_TEST_DB_APPROVAL:
            raise H6BPlanError("project test-DB approval source is not exact")


_ISSUER_SEAL = object()


class IssuedOneShotAuthorization:
    """Mutable one-shot capability; its state is authorization-only."""

    __slots__ = (
        "_approved_target",
        "_campaign_hash",
        "_mapping_hash",
        "_mode",
        "_plan",
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
        _plan: ProductionExecutionPlan | ContractFixturePlan | None = None,
        _approved_target: DatabaseTarget | None = None,
        _mode: str = "contract_fixture",
        _issuer: object,
    ) -> None:
        self._campaign_hash = campaign_hash
        self._run_id = run_id
        self._mapping_hash = mapping_hash
        self._token = token
        self._plan = _plan
        self._approved_target = _approved_target
        self._mode = _mode
        self._used = False
        self._issuer = _issuer

    def _require_plan(
        self, plan: ProductionExecutionPlan | ContractFixturePlan
    ) -> None:
        if (
            type(self) is not IssuedOneShotAuthorization
            or self._issuer is not _ISSUER_SEAL
            or self._plan is not plan
        ):
            raise H6BPreflightRefused(
                "one-shot authorization is not bound to this exact plan"
            )

    def _require_session_target(self, session: object) -> None:
        if self._mode != "project_test_db":
            return
        if self._approved_target is None:
            raise H6BPreflightRefused("project test-DB authorization lost its target")
        try:
            url = session.get_bind().url
            resolved = DatabaseTarget(
                host=url.host,
                port=url.port,
                database=url.database,
                user=url.username,
            )
        except (AttributeError, H6BPlanError, TypeError) as exc:
            raise H6BPreflightRefused(
                "project test-DB session bind target is unavailable or malformed"
            ) from exc
        if resolved != self._approved_target:
            raise H6BPreflightRefused(
                "project test-DB session bind differs byte-for-byte from approval"
            )

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
        _plan=plan,
        _approved_target=authority.approved_target,
        _mode="production_empirical",
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
        _plan=plan,
        _issuer=_ISSUER_SEAL,
    )


def issue_project_test_db_authorization(
    plan: ProductionExecutionPlan,
    authority: ProjectTestDbAuthority,
) -> IssuedOneShotAuthorization:
    """Issue CP9's one-shot capability without weakening production target gates."""
    if type(plan) is not ProductionExecutionPlan:
        raise H6BPreflightRefused("project test-DB requires exact production plan")
    if type(authority) is not ProjectTestDbAuthority:
        raise H6BPreflightRefused("test-DB authority must use the exact CP9 type")
    expected = DatabaseTarget(
        host=_PROJECT_TEST_DB_TARGET[0],
        port=_PROJECT_TEST_DB_TARGET[1],
        database=_PROJECT_TEST_DB_TARGET[2],
        user=_PROJECT_TEST_DB_TARGET[3],
    )
    if authority.expected_target != expected or authority.observed_target != expected:
        raise H6BPreflightRefused("project test-DB target differs from reviewed tuple")
    if authority.inherited_target not in (None, expected):
        raise H6BPreflightRefused("inherited project test-DB target conflicts")
    if authority.write_opt_in is not True:
        raise H6BPreflightRefused("project test-DB write opt-in is required")
    if (
        authority.expected_full_campaign_hash != plan.full_campaign_hash
        or authority.expected_campaign_run_id != plan.campaign_run_id
        or authority.expected_exact_48_mapping_hash != plan.exact_48_mapping_hash
    ):
        raise H6BPreflightRefused("project test-DB identity authority differs")
    if (
        authority.requested_output_root != authority.expected_output_root
        or authority.expected_output_root != plan.output_root
    ):
        raise H6BPreflightRefused("project test-DB output root differs")
    if authority.expected_source_pins != plan.source_pins:
        raise H6BPreflightRefused("project test-DB expected source pins differ")
    validate_source_pins_pair(
        expected=plan.source_pins,
        observed=authority.observed_source_pins,
    )
    return IssuedOneShotAuthorization(
        campaign_hash=plan.full_campaign_hash,
        run_id=plan.campaign_run_id,
        mapping_hash=plan.exact_48_mapping_hash,
        token=authority.one_shot_approval,
        _plan=plan,
        _approved_target=expected,
        _mode="project_test_db",
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


def _plain(value: object) -> object:
    if isinstance(value, types.MappingProxyType | dict):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(child) for child in value]
    return value


def build_h4_member_trade_key(row: object) -> str:
    """Seal one actual H4.5 raw attribution member without economic repair."""
    if type(row) not in (
        h4_runner.S3SelectedOOSAttribution,
        h4_runner.S4SelectedOOSAttribution,
    ):
        raise H6BPlanError("member trade key requires an exact H4 attribution row")
    return canonical_sha256(
        {
            "schema_version": "rob974_h6b_h4_member_trade_key.v1",
            "h4_attribution_row": h4_runner._attribution_row_payload(row),
        }
    )


def _attempt_evidence_payload(attempt: h6a_evidence.AttemptRecord) -> dict[str, object]:
    return {
        "schema_version": "rob974_h6b_attempt_evidence.v1",
        "row_id": attempt.row_id,
        "strategy_key": attempt.strategy_key,
        "fold_traces": [trace.canonical_payload() for trace in attempt.fold_traces],
        "unique_evidence": [
            {
                "fold_id": evidence.fold_id,
                "candidate_identity_hash": evidence.candidate_identity_hash,
                "evaluated_decision_units": evidence.evaluated_decision_units,
                "no_signal": evidence.no_signal,
                "candidate": evidence.candidate,
                "generator_rejected": evidence.generator_rejected,
                "generator_accepted": evidence.generator_accepted,
                "generator_rejection_subtotal_by_reason": dict(
                    evidence.generator_rejection_subtotal_by_reason
                ),
                "content_hash": evidence.content_hash,
            }
            for evidence in attempt.unique_evidence
        ],
        "path_scenario_evidence": [
            {
                "path_scenario": evidence.path_scenario,
                "status": evidence.status,
                "reason_code": evidence.reason_code,
                "trade_count": evidence.trade_count,
                "member_trade_keys": list(evidence.member_trade_keys),
                "no_trade_reason_counts": dict(evidence.no_trade_reason_counts),
                "artifact_hash": evidence.artifact_hash,
            }
            for evidence in attempt.path_scenario_evidence
        ],
        "historical_executor_state": (
            None
            if attempt.historical_executor_state is None
            else {
                "order_id": attempt.historical_executor_state.order_id,
                "executor_validated": (
                    attempt.historical_executor_state.executor_validated
                ),
                "pair_exec_fail": attempt.historical_executor_state.pair_exec_fail,
                "demo_eligible": attempt.historical_executor_state.demo_eligible,
                "promotion_blocked_reason": (
                    attempt.historical_executor_state.promotion_blocked_reason
                ),
            }
        ),
    }


@dataclass(frozen=True, slots=True)
class ActualH4CampaignResult:
    """Exact actual-H4 output surface consumed by H6-A and canonical H5."""

    identity: ProductionIdentityPlan
    attempts: tuple[h6a_evidence.AttemptRecord, ...]
    attribution: h4_runner.SelectedOOSAttributionEnvelope
    pbo: tuple[h4_pbo.H4PboEvidence, h4_pbo.H4PboEvidence]
    provenance: str = "actual_merged_h4"

    def __post_init__(self) -> None:
        if type(self.identity) is not ProductionIdentityPlan:
            raise H6BPlanError("actual H4 result requires exact production identity")
        if self.provenance != "actual_merged_h4":
            raise H6BPlanError("actual H4 result provenance drift")
        if type(self.attempts) is not tuple or len(self.attempts) != 48:
            raise H6BPlanError("actual H4 result requires exact 48 attempts")
        if any(type(item) is not h6a_evidence.AttemptRecord for item in self.attempts):
            raise H6BPlanError("actual H4 attempts must be exact H6-A records")
        mapping = dict(self.identity.ordered_mapping)
        specs = {spec.row_id: spec for spec in self.identity._h4_plan.row_specs}
        if tuple(item.row_id for item in self.attempts) != CANONICAL_ROW_ORDER:
            raise H6BPlanError("actual H4 attempt order differs from exact 48 plan")
        for item in self.attempts:
            spec = specs[item.row_id]
            if (
                item.experiment_id != mapping[item.row_id]
                or item.campaign_run_id != self.identity.campaign_run_id
                or item.full_campaign_hash != self.identity.full_campaign_hash
                or item.strategy_key != spec.strategy_key
                or item.retry_index != 0
            ):
                raise H6BPlanError("actual H4 attempt identity differs from plan")

        envelope = h4_runner.validate_attribution_envelope(self.attribution)
        if (
            envelope.contract_provenance != "actual"
            or envelope.full_campaign_hash != self.identity.full_campaign_hash
            or envelope.campaign_run_id != self.identity.campaign_run_id
            or envelope.source_pins != self.identity.source_pins
            or envelope.h4_source_pins != self.identity.h4_source_pins
        ):
            raise H6BPlanError("actual H4 attribution is not bound to identity")
        if type(self.pbo) is not tuple or len(self.pbo) != 2:
            raise H6BPlanError("actual H4 result requires S3 and S4 PBO evidence")
        if any(type(item) is not h4_pbo.H4PboEvidence for item in self.pbo):
            raise H6BPlanError("actual PBO evidence must use exact H4 types")
        if tuple(item.strategy for item in self.pbo) != ("S3", "S4"):
            raise H6BPlanError("actual PBO evidence must be ordered S3,S4")

        attempt_by_row = {item.row_id: item for item in self.attempts}
        selected_folds_by_row: dict[str, set[str]] = {
            row_id: set() for row_id in CANONICAL_ROW_ORDER
        }
        raw_keys: dict[tuple[str, str], list[str]] = {}
        for path in envelope.paths:
            selected_folds_by_row[path.lineage.row_id].add(path.lineage.fold_id)
            raw_keys.setdefault((path.lineage.row_id, path.path_scenario), []).extend(
                build_h4_member_trade_key(row) for row in path.rows
            )
            attempt = attempt_by_row[path.lineage.row_id]
            unique_by_fold = {
                evidence.fold_id: evidence for evidence in attempt.unique_evidence
            }
            if (
                path.engine_input_count
                > unique_by_fold[path.lineage.fold_id].generator_accepted
            ):
                raise H6BPlanError(
                    "H4 path engine input exceeds H6-A unique accepted evidence"
                )
        for attempt in self.attempts:
            selected = {
                trace.fold_id for trace in attempt.fold_traces if trace.selected
            }
            if selected != selected_folds_by_row[attempt.row_id]:
                raise H6BPlanError(
                    "H4 attribution selected folds differ from H6-A fold traces"
                )
            for path in attempt.path_scenario_evidence:
                expected_keys = tuple(
                    sorted(raw_keys.get((attempt.row_id, path.path_scenario), ()))
                )
                if tuple(sorted(path.member_trade_keys)) != expected_keys:
                    raise H6BPlanError(
                        "H4 raw member keys differ from H6-A named path evidence"
                    )

    def batch_items(self) -> tuple[H6AAttemptBatchItem, ...]:
        return tuple(
            H6AAttemptBatchItem(
                row_id=attempt.row_id,
                experiment_id=attempt.experiment_id,
                retry_index=attempt.retry_index,
                status=attempt.status,
                reason_code=attempt.reason_code,
                fold_evidence_hash=attempt.fold_evidence_hash,
                run_identity=attempt.run_identity,
                evidence_payload=_attempt_evidence_payload(attempt),
            )
            for attempt in self.attempts
        )


class ActualH4RunnerPort(Protocol):
    provenance: str

    async def run(self, plan: ProductionIdentityPlan) -> ActualH4CampaignResult: ...


@dataclass(frozen=True, slots=True)
class ActualH4InputData:
    """Persisted H1 rows supplied to the callback-free merged-H4 runner."""

    h1_minutes: tuple[tuple[str, tuple[MinuteBar, ...]], ...]
    corpus_end_ts: int
    persisted_corpus_hash: str
    persisted_feature_hash: str

    def __post_init__(self) -> None:
        if (
            type(self.h1_minutes) is not tuple
            or tuple(symbol for symbol, _ in self.h1_minutes) != h3_manifest.SYMBOLS
        ):
            raise H6BPlanError("actual H4 input must use exact H1 symbol order")
        for symbol, rows in self.h1_minutes:
            if type(symbol) is not str or type(rows) is not tuple or not rows:
                raise H6BPlanError("actual H4 input rows must be non-empty tuples")
            if any(type(row) is not MinuteBar for row in rows):
                raise H6BPlanError("actual H4 input must contain exact H1 MinuteBar")
            if any(
                right.ts <= left.ts for left, right in zip(rows, rows[1:], strict=False)
            ):
                raise H6BPlanError("actual H4 input minute rows must be ordered")
        if type(self.corpus_end_ts) is not int:
            raise H6BPlanError("actual H4 corpus_end_ts must be exact int")
        if self.corpus_end_ts <= max(rows[-1].ts for _, rows in self.h1_minutes):
            raise H6BPlanError("actual H4 corpus_end_ts must follow every minute")
        _hex64(self.persisted_corpus_hash, "persisted_corpus_hash")
        _hex64(self.persisted_feature_hash, "persisted_feature_hash")

    @classmethod
    def from_mapping(
        cls,
        rows: Mapping[str, Sequence[MinuteBar]],
        *,
        corpus_end_ts: int,
        persisted_corpus_hash: str,
        persisted_feature_hash: str,
    ) -> ActualH4InputData:
        if not isinstance(rows, Mapping) or set(rows) != set(h3_manifest.SYMBOLS):
            raise H6BPlanError("actual H4 rows must cover exact selected universe")
        return cls(
            tuple((symbol, tuple(rows[symbol])) for symbol in h3_manifest.SYMBOLS),
            corpus_end_ts,
            persisted_corpus_hash,
            persisted_feature_hash,
        )

    def as_dict(self) -> dict[str, tuple[MinuteBar, ...]]:
        return dict(self.h1_minutes)


@dataclass(frozen=True, slots=True)
class _ActualExecutionSurface:
    phase_context: h4_runner.ActualH1PhaseContext
    raw_minutes: dict[str, tuple[MinuteBar, ...]]
    minute_index: object
    close_feature_index: dict[tuple[str, int], object]
    pair_close_index: dict[tuple[str, int], object]


def _actual_execution_surface(
    data: ActualH4InputData, *, phase: h4_runner.H4Phase
) -> _ActualExecutionSurface:
    raw = {
        symbol: tuple(row for row in rows if row.ts < phase.end_ms)
        for symbol, rows in data.h1_minutes
    }
    context = h4_runner.build_actual_h1_phase_context(
        raw_minutes=raw,
        phase=phase,
    )
    normalized_minutes = tuple(
        row
        for symbol in h3_manifest.SYMBOLS
        for row in h2_h1_bridge.from_h1_minute_bars(symbol, raw[symbol])
    )
    minute_index = h2_ingress.build_minute_index(normalized_minutes)
    close_features = tuple(
        row
        for symbol in h3_manifest.SYMBOLS
        for row in h2_h1_bridge.from_h1_close_features(
            symbol,
            context.feature_context.bars_for(symbol),
            context.feature_context.snapshots,
        )
    )
    pair_closes = tuple(
        row
        for symbol in h3_manifest.SYMBOLS
        for row in h2_h1_bridge.from_h1_pair_leg_closes(
            symbol, context.feature_context.bars_for(symbol)
        )
    )
    return _ActualExecutionSurface(
        phase_context=context,
        raw_minutes=raw,
        minute_index=minute_index,
        close_feature_index={(row.symbol, row.close_ts): row for row in close_features},
        pair_close_index={(row.symbol, row.close_ts): row for row in pair_closes},
    )


def _finite_h4_pf(values: list[float]) -> float:
    """Transport H5's PF authority through H4's finite-only trace DTO.

    H5 defines zero-loss profit as positive infinity and an empty/zero book
    as undefined.  H4's selection DTO predates that representation and only
    accepts finite floats, so the order-preserving finite maximum represents
    positive infinity and exact zero represents an unrankable empty book.
    No scorecard consumer sees this transport value; H5 recomputes PF from
    raw selected-OOS attribution.
    """
    pf = h5_s3._profit_factor(values)
    if pf is None or math.isnan(pf):
        return 0.0
    if math.isinf(pf):
        return sys.float_info.max
    return pf


def _actual_h4_train_trace(
    *,
    strategy: str,
    config_id: str,
    terminal: h4_adapter.SealedS3Terminal | h4_adapter.SealedS4Terminal,
) -> h4_selection.TrainCandidateTrace:
    if strategy == "S3" and type(terminal) is h4_adapter.SealedS3Terminal:
        rows = h2_scenarios.build_s3_scenario_ledger(
            terminal.result.trades,
            h2_scenarios.PATH_SCENARIO_PRIMARY_STRESS17,
        )
        unit_order = h3_manifest.SYMBOLS
        values_by_unit = tuple((row.trade.symbol, row.e17_bps) for row in rows)
        scenario_hash = h2_scenarios.s3_ledger_hash(rows)
    elif strategy == "S4" and type(terminal) is h4_adapter.SealedS4Terminal:
        rows = h2_scenarios.build_s4_scenario_ledger(
            terminal.result.trades,
            h2_scenarios.PATH_SCENARIO_PRIMARY_STRESS17,
        )
        unit_order = h3_manifest.PAIRS
        values_by_unit = tuple(
            (
                row.trade.pair[0].removesuffix("USDT")
                + "-"
                + row.trade.pair[1].removesuffix("USDT"),
                row.e17_bps,
            )
            for row in rows
        )
        scenario_hash = h2_scenarios.s4_ledger_hash(rows)
    else:
        raise H6BPlanError("actual H4 train terminal strategy/type differs")
    values = [row.e17_bps for row in rows]
    units: list[h4_selection.TrainUnitMetric] = []
    for unit in unit_order:
        selected = [value for row_unit, value in values_by_unit if row_unit == unit]
        units.append(
            h4_selection.TrainUnitMetric(
                unit=unit,
                completed_basket_trades=len(selected),
                e17_bps=(math.fsum(selected) / len(selected) if selected else 0.0),
            )
        )
    return h4_selection.TrainCandidateTrace(
        config_id=config_id,
        units=tuple(units),
        pf=_finite_h4_pf(values),
        pooled_e17_bps=(math.fsum(values) / len(values) if values else 0.0),
        train_input_hash=terminal.input_seal_sha256,
        train_scenario_hash=scenario_hash,
    )


def _h6a_unique_evidence(
    evidence: h3_evidence.UniqueGeneratorEvidence,
    *,
    fold_id: str,
) -> h6a_evidence.UniqueGeneratorEvidence:
    if type(evidence) is not h3_evidence.UniqueGeneratorEvidence:
        raise H6BPlanError("actual unique evidence must use exact H3 type")
    if evidence.fold_or_full_window != fold_id:
        raise H6BPlanError("actual unique evidence fold differs")
    # H3 carries a closed taxonomy with literal zero buckets.  H6-A accepts
    # that broader non-negative mapping, while H5's canonical DTO represents
    # only observed reasons and requires every retained count to be positive.
    # Filtering zero buckets preserves the exact generator-rejected subtotal;
    # it does not merge no-signal reasons or recompute an outcome.
    rejection_counts = {
        reason: count
        for reason, count in evidence.generator_rejection_reason_histogram
        if count > 0
    }
    payload = {
        "fold_id": fold_id,
        "candidate_identity_hash": evidence.content_hash,
        "evaluated_decision_units": evidence.evaluated_decision_units,
        "no_signal": evidence.no_signal,
        "candidate": evidence.candidate,
        "generator_rejected": evidence.generator_rejected,
        "generator_accepted": evidence.generator_accepted,
        "generator_rejection_subtotal_by_reason": rejection_counts,
    }
    return h6a_evidence.UniqueGeneratorEvidence(
        **payload,
        content_hash=canonical_sha256(payload),
    )


def _terminal_reason_counts(
    terminal: h4_adapter.SealedS3Terminal | h4_adapter.SealedS4Terminal,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in (*terminal.result.no_trades, *terminal.result.incompletes):
        reason = row.reason
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _path_evidence(
    *,
    path_scenario: str,
    selected: bool,
    member_keys: tuple[str, ...],
    no_trade_reason_counts: Mapping[str, int],
) -> h6a_evidence.PathScenarioEvidence:
    payload = {
        "path_scenario": path_scenario,
        "status": "completed" if selected else "never_selected",
        "reason_code": None,
        "trade_count": len(member_keys),
        "member_trade_keys": tuple(sorted(member_keys)),
        "no_trade_reason_counts": dict(no_trade_reason_counts),
    }
    return h6a_evidence.PathScenarioEvidence(
        **payload,
        artifact_hash=canonical_sha256(
            {
                **payload,
                "member_trade_keys": sorted(member_keys),
            }
        ),
    )


class ActualMergedH4Runner:
    """Callback-free owner of the merged H1/H2/H3/H4 campaign APIs."""

    provenance = "actual_merged_h4"

    def __init__(self, data: ActualH4InputData) -> None:
        if type(data) is not ActualH4InputData:
            raise H6BPlanError("actual H4 runner requires exact persisted input")
        self._data = data
        self.last_trace: tuple[str, ...] = ()
        self.last_selected: tuple[tuple[str, str, str], ...] = ()
        self.last_result: ActualH4CampaignResult | None = None

    def _run_train_strategy(
        self,
        *,
        strategy: str,
        fold_id: str,
        surface: _ActualExecutionSurface,
        horizon_end_ts: int,
    ) -> tuple[
        tuple[h3_s3.S3GeneratorOutput | h3_s4.S4GeneratorOutput, ...],
        tuple[h4_selection.TrainCandidateTrace, ...],
        h4_selection.TrainSelection,
    ]:
        configs = (
            h3_manifest.FROZEN_S3_CONFIGS
            if strategy == "S3"
            else h3_manifest.FROZEN_S4_CONFIGS
        )
        outputs: list[h3_s3.S3GeneratorOutput | h3_s4.S4GeneratorOutput] = []
        factory_index = 0

        def generator(config: object) -> tuple[object, ...]:
            if strategy == "S3":
                output = h3_s3.generate_s3_global(
                    surface.phase_context.feature_context,
                    surface.phase_context.emit_window,
                    config,
                )
                intents = tuple(
                    h3_h2_adapter.adapt_s3_candidate(item, fold_id=fold_id)
                    for item in output.accepted
                )
            else:
                output = h3_s4.generate_s4_global(
                    surface.phase_context.feature_context,
                    surface.phase_context.emit_window,
                    config,
                )
                intents = tuple(
                    h3_h2_adapter.adapt_s4_candidate(item, fold_id=fold_id)
                    for item in output.accepted
                )
            outputs.append(output)
            return intents

        def fresh_engine() -> Callable[[tuple[object, ...]], object]:
            nonlocal factory_index
            config = configs[factory_index]
            factory_index += 1

            def run(intents: tuple[object, ...]) -> object:
                if strategy == "S3":
                    return h4_adapter.invoke_actual_s3_engine(
                        candidates=intents,
                        minute_index=surface.minute_index,
                        close_feature_index=surface.close_feature_index,
                        corpus_end_ts=self._data.corpus_end_ts,
                        horizon_end_ts=horizon_end_ts,
                        strategy=strategy,
                        config_id=config.config_id,
                        fold_id=fold_id,
                    )
                return h4_adapter.invoke_actual_s4_engine(
                    candidates=intents,
                    minute_index=surface.minute_index,
                    pair_close_index=surface.pair_close_index,
                    corpus_end_ts=self._data.corpus_end_ts,
                    horizon_end_ts=horizon_end_ts,
                    strategy=strategy,
                    config_id=config.config_id,
                    fold_id=fold_id,
                )

            return run

        terminals = h4_selection.run_train_global_configs(
            strategy=strategy,
            configs=configs,
            generator=generator,
            fresh_primary_engine=fresh_engine,
        )
        traces = tuple(
            _actual_h4_train_trace(
                strategy=strategy,
                config_id=config.config_id,
                terminal=terminal,
            )
            for config, terminal in zip(configs, terminals, strict=True)
        )
        return (
            tuple(outputs),
            traces,
            h4_selection.select_train_config(strategy, traces),
        )

    def _run_selected_strategy(
        self,
        *,
        identity: ProductionIdentityPlan,
        strategy: str,
        fold: object,
        config_id: str,
        surface: _ActualExecutionSurface,
        tercile: h4_runner.TercileAuthority | None,
    ) -> tuple[
        h3_s3.S3GeneratorOutput | h3_s4.S4GeneratorOutput,
        tuple[h4_runner.SelectedOOSPathAttribution, ...],
    ]:
        config = h3_manifest.get_config(config_id)
        generated: list[h3_s3.S3GeneratorOutput | h3_s4.S4GeneratorOutput] = []

        def generator(_winner: object) -> tuple[object, ...]:
            if strategy == "S3":
                output = h3_s3.generate_s3_global(
                    surface.phase_context.feature_context,
                    surface.phase_context.emit_window,
                    config,
                )
                intents = tuple(
                    h3_h2_adapter.adapt_s3_candidate(item, fold_id=fold.fold_id)
                    for item in output.accepted
                )
            else:
                output = h3_s4.generate_s4_global(
                    surface.phase_context.feature_context,
                    surface.phase_context.emit_window,
                    config,
                )
                intents = tuple(
                    h3_h2_adapter.adapt_s4_candidate(item, fold_id=fold.fold_id)
                    for item in output.accepted
                )
            generated.append(output)
            return intents

        def fresh_engine(
            _scenario: str,
        ) -> Callable[[tuple[object, ...]], object]:
            def run(intents: tuple[object, ...]) -> object:
                if strategy == "S3":
                    return h4_adapter.invoke_actual_s3_engine(
                        candidates=intents,
                        minute_index=surface.minute_index,
                        close_feature_index=surface.close_feature_index,
                        corpus_end_ts=self._data.corpus_end_ts,
                        horizon_end_ts=fold.oos_end_ms,
                        strategy=strategy,
                        config_id=config_id,
                        fold_id=fold.fold_id,
                    )
                return h4_adapter.invoke_actual_s4_engine(
                    candidates=intents,
                    minute_index=surface.minute_index,
                    pair_close_index=surface.pair_close_index,
                    corpus_end_ts=self._data.corpus_end_ts,
                    horizon_end_ts=fold.oos_end_ms,
                    strategy=strategy,
                    config_id=config_id,
                    fold_id=fold.fold_id,
                )

            return run

        terminals = h4_runner.run_selected_oos_paths(
            winner=config,
            generator=generator,
            fresh_engine=fresh_engine,
        )
        if len(generated) != 1:
            raise H6BPlanError("actual H4 selected generator call count differs")
        output = generated[0]
        spec = next(
            row for row in identity._h4_plan.row_specs if row.row_id == config_id
        )
        paths: list[h4_runner.SelectedOOSPathAttribution] = []
        for scenario, terminal in zip(
            h6a_evidence.PATH_SCENARIOS, terminals, strict=True
        ):
            if strategy == "S3":
                if tercile is None:
                    raise H6BPlanError("actual S3 selected path lacks tercile")
                path = h4_runner.bind_s3_attribution_path(
                    row_spec=spec,
                    fold_id=fold.fold_id,
                    path_scenario=scenario,
                    candidates=output.accepted,
                    terminal=terminal,
                    corpus_end_ts=self._data.corpus_end_ts,
                    horizon_end_ts=fold.oos_end_ms,
                    decision_snapshots=surface.phase_context.feature_context.snapshots,
                    tercile_authority=tercile,
                )
            else:
                path = h4_runner.bind_s4_attribution_path(
                    row_spec=spec,
                    fold_id=fold.fold_id,
                    path_scenario=scenario,
                    candidates=output.accepted,
                    terminal=terminal,
                    corpus_end_ts=self._data.corpus_end_ts,
                    horizon_end_ts=fold.oos_end_ms,
                    decision_snapshots=surface.phase_context.feature_context.snapshots,
                )
            paths.append(path)
        return output, tuple(paths)

    def _run_inactive_strategy(
        self,
        *,
        strategy: str,
        fold: object,
        feature_context: h3_s3.FeatureContext,
    ) -> tuple[
        tuple[h3_s3.S3GeneratorOutput | h3_s4.S4GeneratorOutput, ...],
        tuple[h4_selection.TrainCandidateTrace, ...],
        h4_selection.TrainSelection,
    ]:
        """Run one real no-data decision close for a bounded inactive fold.

        CP10 authorizes a bounded active event workload while retaining all
        eight fold identities.  Fold-00 exercises the complete TRAIN/OOS
        workload; later folds execute the real global generators and fresh
        H2 terminals over their first OOS decision close, which is beyond the
        persisted synthetic corpus and therefore must remain NO_SIGNAL.
        """
        configs = (
            h3_manifest.FROZEN_S3_CONFIGS
            if strategy == "S3"
            else h3_manifest.FROZEN_S4_CONFIGS
        )
        window = h3_s3.EmitWindow(
            fold.oos_start_ms,
            fold.oos_start_ms + FOUR_HOUR_MS,
        )
        outputs: list[h3_s3.S3GeneratorOutput | h3_s4.S4GeneratorOutput] = []
        factory_index = 0

        def generator(config: object) -> tuple[object, ...]:
            if strategy == "S3":
                output = h3_s3.generate_s3_global(feature_context, window, config)
                intents = tuple(
                    h3_h2_adapter.adapt_s3_candidate(item, fold_id=fold.fold_id)
                    for item in output.accepted
                )
            else:
                output = h3_s4.generate_s4_global(feature_context, window, config)
                intents = tuple(
                    h3_h2_adapter.adapt_s4_candidate(item, fold_id=fold.fold_id)
                    for item in output.accepted
                )
            outputs.append(output)
            return intents

        def fresh_engine() -> Callable[[tuple[object, ...]], object]:
            nonlocal factory_index
            config = configs[factory_index]
            factory_index += 1

            def run(intents: tuple[object, ...]) -> object:
                if strategy == "S3":
                    return h4_adapter.invoke_actual_s3_engine(
                        candidates=intents,
                        minute_index={},
                        close_feature_index={},
                        corpus_end_ts=self._data.corpus_end_ts,
                        horizon_end_ts=fold.oos_end_ms,
                        strategy=strategy,
                        config_id=config.config_id,
                        fold_id=fold.fold_id,
                    )
                return h4_adapter.invoke_actual_s4_engine(
                    candidates=intents,
                    minute_index={},
                    pair_close_index={},
                    corpus_end_ts=self._data.corpus_end_ts,
                    horizon_end_ts=fold.oos_end_ms,
                    strategy=strategy,
                    config_id=config.config_id,
                    fold_id=fold.fold_id,
                )

            return run

        terminals = h4_selection.run_train_global_configs(
            strategy=strategy,
            configs=configs,
            generator=generator,
            fresh_primary_engine=fresh_engine,
        )
        if any(output.accepted for output in outputs):
            raise H6BPlanError("inactive fold unexpectedly generated a candidate")
        traces = tuple(
            _actual_h4_train_trace(
                strategy=strategy,
                config_id=config.config_id,
                terminal=terminal,
            )
            for config, terminal in zip(configs, terminals, strict=True)
        )
        selection = h4_selection.select_train_config(strategy, traces)
        if selection.selected_config_id is not None:
            raise H6BPlanError("inactive fold unexpectedly selected a config")
        return tuple(outputs), traces, selection

    def _run_pbo(
        self,
    ) -> tuple[h4_pbo.H4PboEvidence, h4_pbo.H4PboEvidence]:
        surface = _actual_execution_surface(
            self._data,
            phase=h4_runner.H4Phase(
                "pbo_full_window",
                h4_contracts.WINDOW_START_MS,
                h4_contracts.WINDOW_END_MS,
            ),
        )

        def s3_generator(config: object) -> tuple[object, ...]:
            output = h3_s3.generate_s3_global(
                surface.phase_context.feature_context,
                surface.phase_context.emit_window,
                config,
            )
            return tuple(
                replace(
                    h3_h2_adapter.adapt_s3_candidate(item, fold_id="pbo-full-window"),
                    fold_id=None,
                )
                for item in output.accepted
            )

        def s4_generator(config: object) -> tuple[object, ...]:
            output = h3_s4.generate_s4_global(
                surface.phase_context.feature_context,
                surface.phase_context.emit_window,
                config,
            )
            return tuple(
                replace(
                    h3_h2_adapter.adapt_s4_candidate(item, fold_id="pbo-full-window"),
                    fold_id=None,
                )
                for item in output.accepted
            )

        s3_grid = h4_pbo.run_full_window_s3_configs(
            configs=h3_manifest.FROZEN_S3_CONFIGS,
            generator=s3_generator,
            minute_index=surface.minute_index,
            close_feature_index=surface.close_feature_index,
        )
        s4_grid = h4_pbo.run_full_window_s4_configs(
            configs=h3_manifest.FROZEN_S4_CONFIGS,
            generator=s4_generator,
            minute_index=surface.minute_index,
            pair_close_index=surface.pair_close_index,
        )
        return (
            h4_pbo.compute_h4_full_window_pbo(
                strategy="S3", daily_gross_bps_by_config=s3_grid
            ),
            h4_pbo.compute_h4_full_window_pbo(
                strategy="S4", daily_gross_bps_by_config=s4_grid
            ),
        )

    async def run(self, plan: ProductionIdentityPlan) -> ActualH4CampaignResult:
        if type(plan) is not ProductionIdentityPlan:
            raise H6BPreflightRefused("actual H4 runner requires production identity")
        trace: list[str] = ["persisted_h1_input"]
        folds = h4_contracts.exact_h4_folds()
        unique_by_row: dict[str, list[h6a_evidence.UniqueGeneratorEvidence]] = {
            row_id: [] for row_id in CANONICAL_ROW_ORDER
        }
        fold_trace_inputs: dict[
            tuple[str, str],
            tuple[h4_selection.TrainCandidateTrace, bool],
        ] = {}
        attribution_paths: list[h4_runner.SelectedOOSPathAttribution] = []
        tercile_by_fold: dict[str, h4_runner.TercileAuthority] = {}
        selected_rows: list[tuple[str, str, str]] = []

        for fold in folds:
            active_fold = fold.fold_index == 0
            trace.append(
                f"{fold.fold_id}:"
                + ("train_h1" if active_fold else "bounded_inactive_h1")
            )
            if active_fold:
                train_surface = _actual_execution_surface(
                    self._data,
                    phase=h4_runner.phase_for_fold(fold, "train"),
                )
                inactive_context = None
            else:
                phase = h4_runner.phase_for_fold(fold, "selected_oos")
                raw = {
                    symbol: tuple(row for row in rows if row.ts < phase.end_ms)
                    for symbol, rows in self._data.h1_minutes
                }
                inactive_context = h4_runner.build_actual_h1_phase_context(
                    raw_minutes=raw,
                    phase=phase,
                ).feature_context
                train_surface = None
            train_results: dict[
                str,
                tuple[
                    tuple[h3_s3.S3GeneratorOutput | h3_s4.S4GeneratorOutput, ...],
                    tuple[h4_selection.TrainCandidateTrace, ...],
                    h4_selection.TrainSelection,
                ],
            ] = {}
            for strategy in ("S3", "S4"):
                if active_fold:
                    if train_surface is None:
                        raise H6BPlanError("active fold lost its TRAIN surface")
                    trace.append(f"{fold.fold_id}:{strategy}:train_24")
                    train_results[strategy] = self._run_train_strategy(
                        strategy=strategy,
                        fold_id=fold.fold_id,
                        surface=train_surface,
                        horizon_end_ts=fold.train_end_ms,
                    )
                else:
                    if inactive_context is None:
                        raise H6BPlanError("inactive fold lost its H1 context")
                    trace.append(f"{fold.fold_id}:{strategy}:bounded_no_signal_24")
                    train_results[strategy] = self._run_inactive_strategy(
                        strategy=strategy,
                        fold=fold,
                        feature_context=inactive_context,
                    )

            winners = {
                strategy: values[2].selected_config_id
                for strategy, values in train_results.items()
            }
            oos_surface: _ActualExecutionSurface | None = None
            if any(winners.values()):
                trace.append(f"{fold.fold_id}:selected_oos_h1")
                oos_surface = _actual_execution_surface(
                    self._data,
                    phase=h4_runner.phase_for_fold(fold, "selected_oos"),
                )
            for strategy in ("S3", "S4"):
                outputs, traces, selection = train_results[strategy]
                selected_id = selection.selected_config_id
                output_by_id = {output.config_id: output for output in outputs}
                trace_by_id = {row.config_id: row for row in traces}
                selected_output = None
                selected_paths: tuple[h4_runner.SelectedOOSPathAttribution, ...] = ()
                tercile: h4_runner.TercileAuthority | None = None
                if selected_id is not None:
                    if oos_surface is None:
                        raise H6BPlanError("selected H4 config lacks OOS surface")
                    if strategy == "S3":
                        if train_surface is None:
                            raise H6BPlanError("selected S3 fold lacks TRAIN surface")
                        train_snapshots = tuple(
                            snapshot
                            for snapshot in train_surface.phase_context.feature_context.snapshots
                            if fold.train_start_ms
                            <= snapshot.decision_ts
                            < fold.train_end_ms
                        )
                        tercile = h4_runner.build_tercile_authority(
                            fold_id=fold.fold_id,
                            train_start_ms=fold.train_start_ms,
                            train_end_ms=fold.train_end_ms,
                            snapshots=train_snapshots,
                        )
                        tercile_by_fold[fold.fold_id] = tercile
                    trace.append(f"{fold.fold_id}:{strategy}:selected_oos_three_paths")
                    selected_output, selected_paths = self._run_selected_strategy(
                        identity=plan,
                        strategy=strategy,
                        fold=fold,
                        config_id=selected_id,
                        surface=oos_surface,
                        tercile=tercile,
                    )
                    attribution_paths.extend(selected_paths)
                    selected_rows.append((strategy, fold.fold_id, selected_id))

                for config in (
                    h3_manifest.FROZEN_S3_CONFIGS
                    if strategy == "S3"
                    else h3_manifest.FROZEN_S4_CONFIGS
                ):
                    row_id = config.config_id
                    source_output = (
                        selected_output
                        if row_id == selected_id and selected_output is not None
                        else output_by_id[row_id]
                    )
                    h3_unique = h3_evidence.build_unique_generator_evidence(
                        source_output,
                        fold_or_full_window=fold.fold_id,
                        phase=(
                            "selected_oos"
                            if row_id == selected_id or not active_fold
                            else "train"
                        ),
                    )
                    unique_by_row[row_id].append(
                        _h6a_unique_evidence(h3_unique, fold_id=fold.fold_id)
                    )
                    fold_trace_inputs[(row_id, fold.fold_id)] = (
                        trace_by_id[row_id],
                        row_id == selected_id,
                    )

        trace.append("pbo_24x365_s3_s4")
        pbo = self._run_pbo()
        envelope = h4_runner.build_actual_attribution_envelope(
            plan=plan._h4_plan,
            paths=tuple(attribution_paths),
            tercile_authorities=tuple(
                tercile_by_fold[fold.fold_id]
                for fold in folds
                if fold.fold_id in tercile_by_fold
            ),
        )

        raw_keys: dict[tuple[str, str], list[str]] = {}
        path_reasons: dict[tuple[str, str], dict[str, int]] = {}
        selected_folds: dict[str, set[str]] = {
            row_id: set() for row_id in CANONICAL_ROW_ORDER
        }
        for path in envelope.paths:
            selected_folds[path.lineage.row_id].add(path.lineage.fold_id)
            raw_keys.setdefault((path.lineage.row_id, path.path_scenario), []).extend(
                build_h4_member_trade_key(row) for row in path.rows
            )
            counts = path_reasons.setdefault(
                (path.lineage.row_id, path.path_scenario), {}
            )
            for reason, count in _terminal_reason_counts(path.terminal).items():
                counts[reason] = counts.get(reason, 0) + count

        attempts: list[h6a_evidence.AttemptRecord] = []
        for spec in plan._h4_plan.row_specs:
            row_id = spec.row_id
            unique = tuple(unique_by_row[row_id])
            traces: list[h6a_evidence.FoldSelectionTrace] = []
            for fold, evidence in zip(folds, unique, strict=True):
                train_trace, selected = fold_trace_inputs[(row_id, fold.fold_id)]
                eligible = tuple(unit.unit for unit in train_trace.eligible_units)
                excluded = tuple(
                    (unit.unit, "fewer_than_five_completed_trades")
                    for unit in train_trace.units
                    if unit.completed_basket_trades < 5
                )
                traces.append(
                    h6a_evidence.FoldSelectionTrace(
                        fold_id=fold.fold_id,
                        fold_index=fold.fold_index,
                        selected=selected,
                        eligible_symbols_or_pairs=eligible,
                        excluded_symbols_or_pairs=excluded,
                        accepted_input_hash=evidence.content_hash,
                        rejection_reason=None if selected else "not_selected",
                        no_trade_reason_counts={},
                    )
                )
            selected = bool(selected_folds[row_id])
            path_rows = tuple(
                _path_evidence(
                    path_scenario=scenario,
                    selected=selected,
                    member_keys=tuple(raw_keys.get((row_id, scenario), ())),
                    no_trade_reason_counts=path_reasons.get((row_id, scenario), {}),
                )
                for scenario in h6a_evidence.PATH_SCENARIOS
            )
            attempts.append(
                h6a_evidence.build_attempt_record(
                    row_id=row_id,
                    experiment_id=spec.experiment_id,
                    campaign_run_id=plan.campaign_run_id,
                    full_campaign_hash=plan.full_campaign_hash,
                    strategy_key=spec.strategy_key,
                    retry_index=0,
                    status="completed",
                    reason_code=None,
                    fold_traces=tuple(traces),
                    unique_evidence=unique,
                    path_scenario_evidence=path_rows,
                    historical_executor_state=(
                        h6a_evidence.HistoricalExecutorState()
                        if row_id.startswith("S4-")
                        else None
                    ),
                )
            )
        self.last_trace = tuple(trace)
        self.last_selected = tuple(selected_rows)
        result = ActualH4CampaignResult(
            identity=plan,
            attempts=tuple(attempts),
            attribution=envelope,
            pbo=pbo,
        )
        self.last_result = result
        return result


class ActualMergedH6AAccounting:
    provenance = "actual_merged_h6a"

    def reconstruct(
        self,
        *,
        plan: ProductionExecutionPlan,
        registered_total: int,
        attempts: Sequence[H6AAttemptBatchItem],
    ) -> h6a_accounting.CombinedAccountingReport:
        if type(plan) is not ProductionExecutionPlan:
            raise H6BPreflightRefused("actual accounting rejects non-production plan")
        if type(registered_total) is not int:
            raise H6BPreflightRefused("registered_total must be exact int")
        rows = tuple(
            h6a_accounting.AttemptAccountingRow(
                row_id=item.row_id,
                experiment_id=item.experiment_id,
                retry_index=item.retry_index,
                status=item.status,
                reason_code=item.reason_code,
                fold_evidence_hash=item.fold_evidence_hash,
                run_identity=item.run_identity,
            )
            for item in attempts
        )
        return h6a_accounting.build_combined_accounting(
            campaign_run_id=plan.campaign_run_id,
            canonical_row_ids=CANONICAL_ROW_ORDER,
            row_id_to_experiment_id=dict(plan.ordered_mapping),
            registered_total=registered_total,
            attempts=rows,
        )


def _h5_dual_evidence(
    result: ActualH4CampaignResult,
) -> tuple[
    dict[str, dict[tuple[str, str], h5_dual.UniqueGeneratorEvidence]],
    dict[str, dict[tuple[str, str, str], h5_dual.PathInvocationEvidence]],
]:
    unique_by_strategy: dict[
        str, dict[tuple[str, str], h5_dual.UniqueGeneratorEvidence]
    ] = {"S3": {}, "S4": {}}
    path_by_strategy: dict[
        str, dict[tuple[str, str, str], h5_dual.PathInvocationEvidence]
    ] = {"S3": {}, "S4": {}}
    attempt_by_row = {attempt.row_id: attempt for attempt in result.attempts}
    for attempt in result.attempts:
        strategy = attempt.row_id.split("-", 1)[0]
        for evidence in attempt.unique_evidence:
            unique_by_strategy[strategy][(attempt.row_id, evidence.fold_id)] = (
                h5_dual.UniqueGeneratorEvidence(
                    strategy=strategy,
                    config_id=attempt.row_id,
                    fold_id=evidence.fold_id,
                    accepted=evidence.generator_accepted,
                    rejected=evidence.generator_rejected,
                    accepted_input_hash=evidence.content_hash,
                    rejection_reason_histogram=dict(
                        evidence.generator_rejection_subtotal_by_reason
                    ),
                )
            )
    for path in result.attribution.paths:
        attempt = attempt_by_row[path.lineage.row_id]
        unique = next(
            item
            for item in attempt.unique_evidence
            if item.fold_id == path.lineage.fold_id
        )
        aggregate = next(
            item
            for item in attempt.path_scenario_evidence
            if item.path_scenario == path.path_scenario
        )
        evidence = h5_dual.PathInvocationEvidence(
            strategy=path.strategy,
            config_id=path.lineage.row_id,
            fold_id=path.lineage.fold_id,
            path_scenario=path.path_scenario,
            unique_evidence_hash=unique.content_hash,
            unique_evidence_accepted_count=unique.generator_accepted,
            engine_input_hash=path.terminal.input_seal_sha256,
            engine_input_count=path.engine_input_count,
            no_trade_reason_counts=dict(aggregate.no_trade_reason_counts),
            ledger_status=aggregate.status,
            trade_count=len(path.rows),
            artifact_hash=aggregate.artifact_hash,
        )
        path_by_strategy[path.strategy][
            (path.lineage.row_id, path.lineage.fold_id, path.path_scenario)
        ] = evidence
    selected_keys = {
        (path.strategy, path.lineage.row_id, path.lineage.fold_id)
        for path in result.attribution.paths
    }
    for strategy, row_id, fold_id in selected_keys:
        unique = unique_by_strategy[strategy][(row_id, fold_id)]
        paths = {
            scenario: path_by_strategy[strategy][(row_id, fold_id, scenario)]
            for scenario in h5_contracts.PATH_SCENARIOS
        }
        h5_dual.cross_check_dual_evidence(unique, paths)
    return unique_by_strategy, path_by_strategy


def _h5_pbo(
    *, identity: ProductionIdentityPlan, evidence: h4_pbo.H4PboEvidence
) -> h5_dual.PboEvidence:
    # H5 owns this semantic boundary: an exact 24 x 365 x 4 H4 evaluator
    # result with reason codes is honest incomplete evidence, not malformed
    # evidence and not a materializer failure.  Preserve the actual H4 value
    # and reasons so ``validate_pbo_evidence`` can make that distinction.
    return h5_dual.PboEvidence(
        strategy=evidence.strategy,
        config_count=evidence.config_count,
        day_count=evidence.day_count,
        slices=evidence.slices,
        scenario_name="primary_stress17",
        value=evidence.value,
        reason_codes=evidence.reason_codes,
        source_hash=identity.h4_source_pins.pbo_source_sha256,
        input_hash=evidence.grid_seal_sha256,
        artifact_hash=evidence.grid_seal_sha256,
    )


class ActualMergedH5Composition:
    """Pure composition adapter; every economic calculation remains in H5."""

    provenance = "actual_merged_h5"

    def build_scorecard(
        self,
        *,
        plan: ProductionExecutionPlan,
        h4_result: ActualH4CampaignResult,
        accounting: h6a_accounting.CombinedAccountingReport,
    ) -> dict[str, object]:
        if type(plan) is not ProductionExecutionPlan:
            raise H6BPreflightRefused("actual H5 rejects non-production plan")
        if type(h4_result) is not ActualH4CampaignResult:
            raise H6BPreflightRefused("actual H5 requires exact H4 campaign result")
        if h4_result.identity is not plan._identity:
            raise H6BPreflightRefused("actual H4 result is not bound to execution plan")
        if type(accounting) is not h6a_accounting.CombinedAccountingReport:
            raise H6BPreflightRefused("actual H5 requires H6-A accounting report")

        h4_contract = h5_contracts.consume_h4_attribution(h4_result.attribution)
        if (
            h4_contract.actual_h4_contract != "PASS"
            or h4_contract.contract_provenance != "actual"
        ):
            raise H6BPreflightRefused(
                "actual H4 contract did not pass typed consumption"
            )
        unique_by_strategy, paths_by_strategy = _h5_dual_evidence(h4_result)
        pbo_by_strategy = {
            evidence.strategy: _h5_pbo(identity=h4_result.identity, evidence=evidence)
            for evidence in h4_result.pbo
        }

        strategy_inputs: dict[str, h5_canonical.StrategyCanonicalInputs] = {}
        for strategy in h5_contracts.STRATEGIES:
            primary = tuple(
                trade
                for trade in h4_contract.trades
                if trade.strategy == strategy
                and trade.path_scenario == "primary_stress17"
            )
            upward = tuple(
                trade
                for trade in h4_contract.trades
                if trade.strategy == strategy
                and trade.path_scenario == "upward_stress22"
            )
            common = h5_gates.evaluate_common_gates(
                primary_trades=primary, upward_trades=upward
            )
            if strategy == "S3":
                falsification = h5_s3.evaluate_s3_falsification(
                    primary_trades=primary, upward_trades=upward
                )
                exit_order = h5_contracts.S3_EXIT_REASONS
                dimension_order = h5_contracts.S3_SYMBOLS
                executor_state = None
            else:
                falsification = h5_s4.evaluate_s4_falsification(
                    primary_trades=primary, upward_trades=upward
                )
                exit_order = h5_contracts.S4_EXIT_REASONS
                dimension_order = h5_contracts.S4_PAIRS
                executor_state = h5_s4.S4_HISTORICAL_PAIR_EXECUTOR_STATE
            direct = h5_s4.compute_direct_verdict(
                incomplete_reasons=falsification.incomplete_reasons,
                hard_gate_reasons=common.reasons + falsification.reasons,
            )
            strategy_inputs[strategy] = h5_canonical.StrategyCanonicalInputs(
                strategy=strategy,
                common_gates=common,
                falsification=falsification,
                direct_verdict=direct,
                exit_reason_order=exit_order,
                dimension_order=dimension_order,
                unique_by_key=unique_by_strategy[strategy],
                paths_by_key=paths_by_strategy[strategy],
                pbo=pbo_by_strategy[strategy],
                pair_executor_state=executor_state,
            )

        s3_inputs = strategy_inputs["S3"]
        s4_inputs = strategy_inputs["S4"]
        campaign_kwargs: dict[str, object] = {}
        if (
            s3_inputs.direct_verdict == "historical_pass"
            and s4_inputs.direct_verdict == "historical_pass"
        ):
            campaign_kwargs = {
                "s3_rank_metrics": h5_canonical._rank_metrics_from_strategy_inputs(
                    s3_inputs
                ),
                "s4_rank_metrics": h5_canonical._rank_metrics_from_strategy_inputs(
                    s4_inputs
                ),
            }
        campaign = h5_s4.compute_campaign_decision(
            s3_direct_verdict=s3_inputs.direct_verdict,
            s4_direct_verdict=s4_inputs.direct_verdict,
            **campaign_kwargs,
        )

        parent = h4_result.identity._h4_plan.envelope.parent_corpus
        envelope = h5_contracts.CampaignEnvelope(
            full_campaign_hash=plan.full_campaign_hash,
            campaign_run_id=plan.campaign_run_id,
            parent_corpus_hash=parent["content_sha256"],
            parent_projection_hash=parent["physical_manifest_sha256"],
            feature_contract_hash=plan.source_pins.feature_source_sha256,
            strategy_contract_hashes={
                "S3": h3_manifest.S3_STRATEGY_CONTRACT.contract_hash,
                "S4": h3_manifest.S4_STRATEGY_CONTRACT.contract_hash,
            },
            h4_runner_source_hash=plan.source_pins.runner_source_sha256,
            h4_pbo_source_hash=plan.source_pins.pbo_implementation_sha256,
            h2_engine_source_hash=plan.source_pins.engine_source_sha256,
            h3_generator_source_hash=plan.source_pins.runner_source_sha256,
            run_schema_version=(h4_result.identity._h4_plan.envelope.schema_version),
            generator_version=h1_lineage.GENERATOR_VERSION,
            expected_experiment_ids=CANONICAL_ROW_ORDER,
            h6a_trial_accounting_hash=accounting.trial_accounting_hash,
        )
        accounting_contract = h5_contracts.resolve_h6a_accounting_contract(accounting)
        if accounting_contract.seal is None:
            raise H6BPreflightRefused("actual H6-A accounting was not evaluated")
        validation = h5_contracts.validate_envelope_and_accounting(
            envelope, accounting_contract.seal
        )
        return h5_canonical.build_canonical_scorecard(
            envelope=envelope,
            h6a_seal=accounting,
            envelope_ok=validation.ok,
            envelope_incomplete_reasons=validation.incomplete_reasons,
            h4_attribution=h4_contract,
            s3_inputs=s3_inputs,
            s4_inputs=s4_inputs,
            campaign_decision=campaign,
        )

    def canonical_json_bytes(self, scorecard: Mapping[str, object]) -> bytes:
        return h5_canonical.canonical_json_bytes(scorecard)

    def semantic_hash(self, scorecard: Mapping[str, object]) -> str:
        return h5_canonical.hash_canonical_bytes(
            h5_canonical.canonical_json_bytes(scorecard)
        )

    def render_markdown(self, scorecard: Mapping[str, object]) -> bytes:
        return h5_markdown.render_markdown(scorecard)


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


def _persisted_h6a_item(
    *,
    row: ResearchBacktestRun,
    experiment_id: str,
    plan: ProductionExecutionPlan,
) -> H6AAttemptBatchItem:
    raw = row.raw_payload
    if type(raw) is not dict:
        raise ReplayCollisionError("persisted H6-A trial raw_payload is not a dict")
    if raw.get("campaign_run_id") != plan.campaign_run_id:
        raise ReplayCollisionError("persisted H6-A trial campaign differs")
    required = {
        "row_id",
        "retry_index",
        "reason_code",
        "fold_evidence_hash",
        "run_identity",
        "evidence_payload",
        "h6a_evidence_fingerprint",
    }
    if not required <= raw.keys():
        raise ReplayCollisionError("persisted H6-A trial payload is incomplete")
    try:
        item = H6AAttemptBatchItem(
            row_id=raw["row_id"],
            experiment_id=experiment_id,
            retry_index=raw["retry_index"],
            status=row.trial_status,
            reason_code=raw["reason_code"],
            fold_evidence_hash=raw["fold_evidence_hash"],
            run_identity=raw["run_identity"],
            evidence_payload=raw["evidence_payload"],
            diagnostic_evidence=tuple(raw.get("diagnostic_evidence", ())),
            diagnostic_overflow=raw.get("diagnostic_overflow"),
        )
    except Exception as exc:
        raise ReplayCollisionError("persisted H6-A trial failed typed parsing") from exc
    if raw["h6a_evidence_fingerprint"] != item.fingerprint():
        raise ReplayCollisionError("persisted H6-A trial fingerprint differs")
    if row.trial_idempotency_key != item.idempotency_key(plan.campaign_run_id):
        raise ReplayCollisionError("persisted H6-A trial idempotency key differs")
    return item


def parse_persisted_attempt_record(
    item: H6AAttemptBatchItem,
    *,
    plan: ProductionExecutionPlan,
) -> h6a_evidence.AttemptRecord:
    """Rebuild one exact H6-A attempt from its immutable persisted payload.

    The database stores H6-A's mutation DTO plus H6-B's canonical evidence
    envelope.  A standalone audit must not trust that JSON as an accounting
    summary, so this boundary reconstructs every exact H6-A evidence type and
    asks ``build_attempt_record`` to recompute both semantic hashes.
    """
    if type(item) is not H6AAttemptBatchItem:
        raise ReplayCollisionError("persisted attempt must use exact H6-A batch type")
    if type(plan) is not ProductionExecutionPlan:
        raise ReplayCollisionError("persisted attempt parser requires production plan")
    payload = item.evidence_payload
    if not isinstance(payload, Mapping):
        raise ReplayCollisionError(
            "persisted attempt evidence payload is not a mapping"
        )
    if payload.get("schema_version") != "rob974_h6b_attempt_evidence.v1":
        raise ReplayCollisionError("persisted attempt evidence schema differs")
    if payload.get("row_id") != item.row_id:
        raise ReplayCollisionError("persisted attempt evidence row differs")
    specs = {spec.row_id: spec for spec in plan._identity._h4_plan.row_specs}
    spec = specs.get(item.row_id)
    if spec is None or payload.get("strategy_key") != spec.strategy_key:
        raise ReplayCollisionError("persisted attempt strategy identity differs")

    try:
        fold_traces = tuple(
            h6a_evidence.FoldSelectionTrace(
                fold_id=row["fold_id"],
                fold_index=row["fold_index"],
                selected=row["selected"],
                eligible_symbols_or_pairs=tuple(row["eligible_symbols_or_pairs"]),
                excluded_symbols_or_pairs=tuple(
                    tuple(pair) for pair in row["excluded_symbols_or_pairs"]
                ),
                accepted_input_hash=row["accepted_input_hash"],
                rejection_reason=row["rejection_reason"],
                no_trade_reason_counts=row["no_trade_reason_counts"],
            )
            for row in payload["fold_traces"]
        )
        unique_evidence = tuple(
            h6a_evidence.UniqueGeneratorEvidence(
                fold_id=row["fold_id"],
                candidate_identity_hash=row["candidate_identity_hash"],
                evaluated_decision_units=row["evaluated_decision_units"],
                no_signal=row["no_signal"],
                candidate=row["candidate"],
                generator_rejected=row["generator_rejected"],
                generator_accepted=row["generator_accepted"],
                generator_rejection_subtotal_by_reason=(
                    row["generator_rejection_subtotal_by_reason"]
                ),
                content_hash=row["content_hash"],
            )
            for row in payload["unique_evidence"]
        )
        path_evidence = tuple(
            h6a_evidence.PathScenarioEvidence(
                path_scenario=row["path_scenario"],
                status=row["status"],
                reason_code=row["reason_code"],
                trade_count=row["trade_count"],
                member_trade_keys=tuple(row["member_trade_keys"]),
                no_trade_reason_counts=row["no_trade_reason_counts"],
                artifact_hash=row["artifact_hash"],
            )
            for row in payload["path_scenario_evidence"]
        )
        historical_payload = payload["historical_executor_state"]
        historical = (
            None
            if historical_payload is None
            else h6a_evidence.HistoricalExecutorState(
                order_id=historical_payload["order_id"],
                executor_validated=historical_payload["executor_validated"],
                pair_exec_fail=historical_payload["pair_exec_fail"],
                demo_eligible=historical_payload["demo_eligible"],
                promotion_blocked_reason=(
                    historical_payload["promotion_blocked_reason"]
                ),
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayCollisionError(
            "persisted attempt evidence failed exact H6-A parsing"
        ) from exc
    if len(path_evidence) != 3:
        raise ReplayCollisionError("persisted attempt must carry exact three paths")

    try:
        return h6a_evidence.build_attempt_record(
            row_id=item.row_id,
            experiment_id=item.experiment_id,
            campaign_run_id=plan.campaign_run_id,
            full_campaign_hash=plan.full_campaign_hash,
            strategy_key=spec.strategy_key,
            retry_index=item.retry_index,
            status=item.status,
            reason_code=item.reason_code,
            fold_traces=fold_traces,
            unique_evidence=unique_evidence,
            path_scenario_evidence=path_evidence,
            historical_executor_state=historical,
            claimed_fold_evidence_hash=item.fold_evidence_hash,
            claimed_run_identity=item.run_identity,
        )
    except (TypeError, ValueError) as exc:
        raise ReplayCollisionError(
            "persisted attempt evidence failed H6-A hash reconstruction"
        ) from exc


class ActualCampaignStateInspector:
    """Scope canonical raw experiment/trial rows without aggregating them."""

    provenance = "actual_read_only_campaign_state"

    async def inspect(
        self, session: object, *, plan: ProductionExecutionPlan
    ) -> CampaignDbSnapshot:
        if type(plan) is not ProductionExecutionPlan:
            raise ReplayCollisionError(
                "actual state inspector requires production plan"
            )
        mapping = dict(plan.ordered_mapping)
        expected_ids = tuple(mapping.values())
        experiment_result = await session.execute(
            select(ResearchStrategyExperiment).where(
                ResearchStrategyExperiment.experiment_id.in_(expected_ids)
            )
        )
        experiments = tuple(experiment_result.scalars().all())
        by_experiment_id = {row.experiment_id: row for row in experiments}
        registered_mapping = tuple(
            (row_id, experiment_id)
            for row_id, experiment_id in plan.ordered_mapping
            if experiment_id in by_experiment_id
        )

        campaign_json = ResearchBacktestRun.raw_payload["campaign_run_id"].astext
        trial_result = await session.execute(
            select(ResearchBacktestRun, ResearchStrategyExperiment.experiment_id)
            .join(
                ResearchStrategyExperiment,
                ResearchStrategyExperiment.id
                == ResearchBacktestRun.strategy_experiment_id,
            )
            .where(
                or_(
                    ResearchStrategyExperiment.experiment_id.in_(expected_ids),
                    campaign_json == plan.campaign_run_id,
                )
            )
        )
        items: list[H6AAttemptBatchItem] = []
        mismatch_row_ids: list[str] = []
        out_of_plan: list[str] = []
        for trial, experiment_id in trial_result.all():
            raw = trial.raw_payload
            campaign_run_id = raw.get("campaign_run_id") if type(raw) is dict else None
            if experiment_id not in expected_ids:
                if campaign_run_id == plan.campaign_run_id:
                    out_of_plan.append(experiment_id)
                continue
            if campaign_run_id != plan.campaign_run_id:
                raw_row_id = raw.get("row_id") if type(raw) is dict else None
                mismatch_row_ids.append(
                    raw_row_id if type(raw_row_id) is str else experiment_id
                )
                continue
            items.append(
                _persisted_h6a_item(
                    row=trial,
                    experiment_id=experiment_id,
                    plan=plan,
                )
            )
        item_by_row = {item.row_id: item for item in items}
        if len(item_by_row) != len(items):
            raise ReplayCollisionError("persisted H6-A trials duplicate a row ID")
        attempts = tuple(
            item_by_row[row_id]
            for row_id in CANONICAL_ROW_ORDER
            if row_id in item_by_row
        )
        present = bool(
            registered_mapping or attempts or mismatch_row_ids or out_of_plan
        )
        return CampaignDbSnapshot(
            campaign_run_id=plan.campaign_run_id if present else None,
            registered_mapping=registered_mapping,
            attempts=attempts,
            mismatch_row_ids=tuple(sorted(mismatch_row_ids)),
            out_of_plan_experiment_ids=tuple(sorted(out_of_plan)),
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


@dataclass(frozen=True, slots=True)
class ProductionCampaignInput:
    """Registration/runtime metadata derived only from the actual H4 plan."""

    plan: ProductionExecutionPlan
    guard_policy: ResearchDbPolicy
    strategy_name: str = "rob974-h6b"
    timeframe: str = "4h"
    runner: str = "rob974-h6b"
    provenance: str = "actual_merged_h4_h5"

    def __post_init__(self) -> None:
        if type(self.plan) is not ProductionExecutionPlan:
            raise H6BPlanError("production campaign requires exact execution plan")
        if type(self.guard_policy) is not ResearchDbPolicy:
            raise H6BPlanError("guard_policy must use exact ResearchDbPolicy")
        for name in ("strategy_name", "timeframe", "runner"):
            _exact_nonempty_str(getattr(self, name), name)
        if self.provenance != "actual_merged_h4_h5":
            raise H6BPlanError("production campaign provenance drift")

    def registration_specs(
        self,
    ) -> tuple[
        tuple[StrategyExperimentIdentity, ...],
        tuple[StrategyExperimentIdentity, ...],
    ]:
        s3: list[StrategyExperimentIdentity] = []
        s4: list[StrategyExperimentIdentity] = []
        for spec in self.plan._identity._h4_plan.row_specs:
            components = _plain(spec.components)
            if type(components) is not dict:
                raise H6BPlanError("H4 row components did not unfreeze to exact dict")
            identity = StrategyExperimentIdentity(
                strategy_key=spec.strategy_key,
                strategy_version=spec.strategy_version,
                hypothesis=spec.hypothesis,
                **components,
            )
            (s3 if spec.row_id.startswith("S3-") else s4).append(identity)
        if len(s3) != 24 or len(s4) != 24:
            raise H6BPlanError("production registration split is not exact 24+24")
        return tuple(s3), tuple(s4)


@dataclass(frozen=True, slots=True)
class ProductionExecutionPorts:
    """Actual predecessor ports; fixture provenance is rejected at creation."""

    session_factory: SessionFactory
    h4_runner: ActualH4RunnerPort
    artifacts: ArtifactPairPort
    state_inspector: CampaignStateInspector
    h6a_accounting: ActualMergedH6AAccounting = field(
        default_factory=ActualMergedH6AAccounting
    )
    h5: ActualMergedH5Composition = field(default_factory=ActualMergedH5Composition)
    register_experiments_fn: RegisterExperimentsFn | None = None
    find_existing_trial_fn: FindExistingTrialFn | None = None
    record_trial_fn: RecordTrialFn | None = None
    diagnostics: DiagnosticCapturePort | None = None
    provenance: str = "actual_merged_h4_h5"

    def __post_init__(self) -> None:
        if not callable(self.session_factory):
            raise H6BPlanError("production session factory must be callable")
        if getattr(
            self.h4_runner, "provenance", None
        ) != "actual_merged_h4" or not callable(getattr(self.h4_runner, "run", None)):
            raise H6BPlanError("production H4 runner is not actual merged H4")
        if type(self.h6a_accounting) is not ActualMergedH6AAccounting:
            raise H6BPlanError("production accounting must be exact actual H6-A")
        if type(self.h5) is not ActualMergedH5Composition:
            raise H6BPlanError("production H5 must be exact actual merged adapter")
        if (
            getattr(self.artifacts, "provenance", None)
            != "rob974_h6b_directory_atomic_v1"
        ):
            raise H6BPlanError("production artifacts must be H6-B directory atomic")
        if getattr(
            self.state_inspector, "provenance", None
        ) != "actual_read_only_campaign_state" or not callable(
            getattr(self.state_inspector, "inspect", None)
        ):
            raise H6BPlanError("production state inspector is not actual read-only")
        for name in (
            "register_experiments_fn",
            "find_existing_trial_fn",
            "record_trial_fn",
        ):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise H6BPlanError(
                    f"optional production delegate {name} is not callable"
                )
        if self.diagnostics is not None and (
            getattr(self.diagnostics, "provenance", None) != "actual_merged_rob970_h6a"
            or not callable(getattr(self.diagnostics, "capture_live_exception", None))
        ):
            raise H6BPlanError("production diagnostics must be merged ROB-970/H6-A")
        if self.provenance != "actual_merged_h4_h5":
            raise H6BPlanError("production execution-port provenance drift")


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


@dataclass(frozen=True, slots=True)
class ContractFixtureClosureEvidence:
    """Deterministic CP7 evidence that cannot be mistaken for production."""

    exact_48_mapping_hash: str
    registered_total: int
    primary_attempts: int
    total_attempts: int
    retry_attempts: int
    trial_accounting_hash: str
    fixture_scorecard_semantic_hash: str
    artifact_names: tuple[str, str]
    empirical_runs: int = 0
    rob974_db_connections: int = 0
    rob974_db_queries: int = 0
    rob974_db_writes: int = 0
    real_db_sessions: int = 0
    production_db_queries: int = 0
    production_db_writes: int = 0
    broker_order_fill_calls: int = 0
    actual_h4_contract: str = "NOT_EVALUATED"
    actual_h5_contract: str = "NOT_EVALUATED"
    production_identity: str = "DEFERRED_UNTIL_H4_SOURCE_PINS"
    launchability: str = "NOT_LAUNCHABLE_CONTRACT_FIXTURE"

    def __post_init__(self) -> None:
        _hex64(self.exact_48_mapping_hash, "closure mapping hash")
        _hex64(self.trial_accounting_hash, "closure accounting hash")
        _hex64(
            self.fixture_scorecard_semantic_hash,
            "closure fixture scorecard hash",
        )
        expected_dimensions = {
            "registered_total": 48,
            "primary_attempts": 48,
            "total_attempts": 48,
            "retry_attempts": 0,
        }
        for name, expected in expected_dimensions.items():
            value = getattr(self, name)
            if type(value) is not int or value != expected:
                raise H6BPlanError(
                    f"fixture closure accounting dimension {name} is not exact"
                )
        if (
            type(self.artifact_names) is not tuple
            or any(type(name) is not str for name in self.artifact_names)
            or self.artifact_names != ("scorecard.json", "scorecard.md")
        ):
            raise H6BPlanError("fixture closure artifact names are not exact")
        for name in (
            "empirical_runs",
            "rob974_db_connections",
            "rob974_db_queries",
            "rob974_db_writes",
            "real_db_sessions",
            "production_db_queries",
            "production_db_writes",
            "broker_order_fill_calls",
        ):
            value = getattr(self, name)
            if type(value) is not int or value != 0:
                raise H6BPlanError(f"fixture closure safety counter {name} is nonzero")
        for name in (
            "actual_h4_contract",
            "actual_h5_contract",
            "production_identity",
            "launchability",
        ):
            if type(getattr(self, name)) is not str:
                raise H6BPlanError(f"fixture closure label {name} is not exact str")
        if (
            self.actual_h4_contract,
            self.actual_h5_contract,
            self.production_identity,
            self.launchability,
        ) != (
            "NOT_EVALUATED",
            "NOT_EVALUATED",
            "DEFERRED_UNTIL_H4_SOURCE_PINS",
            "NOT_LAUNCHABLE_CONTRACT_FIXTURE",
        ):
            raise H6BPlanError("fixture closure dependency labels drifted")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": "rob974_h6b_contract_fixture_closure.v1",
            "predecessor_mode": "actual_h6a_contract_fixture_h4_h5",
            "actual_h4_contract": self.actual_h4_contract,
            "actual_h5_contract": self.actual_h5_contract,
            "production_identity": self.production_identity,
            "launchability": self.launchability,
            "exact_48_mapping_hash": self.exact_48_mapping_hash,
            "accounting": {
                "registered_total": self.registered_total,
                "primary_attempts": self.primary_attempts,
                "total_attempts": self.total_attempts,
                "retry_attempts": self.retry_attempts,
                "trial_accounting_hash": self.trial_accounting_hash,
            },
            "artifact": {
                "names": list(self.artifact_names),
                "fixture_scorecard_semantic_hash": (
                    self.fixture_scorecard_semantic_hash
                ),
            },
            "safety_counters": {
                "empirical_runs": self.empirical_runs,
                "rob974_db_connections": self.rob974_db_connections,
                "rob974_db_queries": self.rob974_db_queries,
                "rob974_db_writes": self.rob974_db_writes,
                "real_db_sessions": self.real_db_sessions,
                "production_db_queries": self.production_db_queries,
                "production_db_writes": self.production_db_writes,
                "broker_order_fill_calls": self.broker_order_fill_calls,
            },
        }


def build_contract_fixture_closure_evidence(
    *, plan: ContractFixturePlan, outcome: MaterializationOutcome
) -> ContractFixtureClosureEvidence:
    """Validate one non-vacuous fixture materialization without identity claims."""
    if type(plan) is not ContractFixturePlan:
        raise H6BPlanError("closure plan must be exact ContractFixturePlan")
    if type(outcome) is not MaterializationOutcome:
        raise H6BPlanError("closure outcome must be exact MaterializationOutcome")
    if outcome.exit_code != MATERIALIZED_EXIT or outcome.disposition != "MATERIALIZED":
        raise H6BPlanError("closure requires a confirmed materialized fixture")
    if outcome.db_state != "ABSENT" or outcome.artifact_state != "ABSENT":
        raise H6BPlanError("closure requires two-sided absence before mutation")
    expected_counters = {
        "session_factory": 1,
        "begin": 1,
        "register": 1,
        "h4": 1,
        "record": 1,
        "accounting": 1,
        "h5": 1,
        "stage": 1,
        "rollback": 0,
        "commit": 1,
        "publish": 1,
        "close": 1,
        "db_inspect": 1,
        "artifact_probe": 1,
        "replay_verify": 0,
        "delete": 0,
    }
    for name, expected in expected_counters.items():
        value = getattr(outcome.counters, name)
        if type(value) is not int or value != expected:
            raise H6BPlanError(f"closure mutation counter {name} is not exact")
    if type(outcome.scorecard) is not dict:
        raise H6BPlanError("closure scorecard is absent or non-canonical")
    accounting = outcome.accounting
    published = outcome.published_pair
    if accounting is None or published is None:
        raise H6BPlanError("closure accounting or published pair is absent")
    if outcome.scorecard.get("semantic_verdict") != "NOT_EVALUATED":
        raise H6BPlanError("fixture scorecard made a semantic verdict claim")
    if outcome.scorecard.get("mapping_hash") != plan.contract_fixture_mapping_hash:
        raise H6BPlanError("fixture scorecard mapping hash differs from plan")
    if outcome.scorecard.get("trial_accounting_hash") != getattr(
        accounting, "trial_accounting_hash", None
    ):
        raise H6BPlanError("fixture scorecard accounting hash differs from H6-A")
    json_path = getattr(published, "json_path", None)
    markdown_path = getattr(published, "markdown_path", None)
    semantic_hash = _hex64(
        getattr(published, "semantic_hash", None),
        "closure fixture scorecard hash",
    )
    if not isinstance(json_path, Path) or not isinstance(markdown_path, Path):
        raise H6BPlanError("fixture published pair lacks physical paths")
    if not json_path.is_file() or not markdown_path.is_file():
        raise H6BPlanError("fixture published pair is not physically present")
    dimensions: dict[str, int] = {}
    for name, expected in (
        ("registered_total", 48),
        ("primary_attempts", 48),
        ("total_attempts", 48),
        ("retry_attempts", 0),
    ):
        value = getattr(accounting, name, None)
        if type(value) is not int or value != expected:
            raise H6BPlanError(f"closure H6-A accounting {name} is not exact")
        dimensions[name] = value
    accounting_hash = _hex64(
        getattr(accounting, "trial_accounting_hash", None),
        "closure accounting hash",
    )
    return ContractFixtureClosureEvidence(
        exact_48_mapping_hash=plan.contract_fixture_mapping_hash,
        registered_total=dimensions["registered_total"],
        primary_attempts=dimensions["primary_attempts"],
        total_attempts=dimensions["total_attempts"],
        retry_attempts=dimensions["retry_attempts"],
        trial_accounting_hash=accounting_hash,
        fixture_scorecard_semantic_hash=semantic_hash,
        artifact_names=(json_path.name, markdown_path.name),
    )


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
    ports: ContractFixtureExecutionPorts | ProductionExecutionPorts,
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
    plan: ContractFixturePlan | ProductionExecutionPlan,
    attempts: tuple[H6AAttemptBatchItem, ...],
) -> None:
    if type(attempts) is not tuple or len(attempts) != 48:
        raise H6BPreflightRefused("H4 must return an exact 48-attempt tuple")
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
    *, plan: ContractFixturePlan | ProductionExecutionPlan, registered: Sequence[object]
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


def _validate_exact_production_snapshot(
    *,
    plan: ProductionExecutionPlan,
    snapshot: CampaignDbSnapshot,
    accounting: h6a_accounting.CombinedAccountingReport,
) -> tuple[h6a_evidence.AttemptRecord, ...]:
    """Validate and reconstruct the immutable production replay surface."""

    if snapshot.campaign_run_id != plan.campaign_run_id:
        raise ReplayCollisionError("persisted production campaign belongs to wrong run")
    if snapshot.registered_mapping != plan.ordered_mapping:
        raise ReplayCollisionError(
            "persisted production registration is partial, reordered, or out of plan"
        )
    if snapshot.mismatch_row_ids or snapshot.out_of_plan_experiment_ids:
        raise ReplayCollisionError("persisted production state carries foreign rows")
    if len(snapshot.attempts) != 48:
        raise ReplayCollisionError("persisted production state is not exact 48")
    required = {
        "campaign_run_id": plan.campaign_run_id,
        "expected_total": 48,
        "registered_total": 48,
        "primary_attempts": 48,
        "total_attempts": 48,
        "retry_attempts": 0,
        "accounting_complete": True,
    }
    for name, expected in required.items():
        if getattr(accounting, name) != expected:
            raise ReplayCollisionError(
                f"persisted production accounting field {name} is not exact"
            )
    if sum(accounting.status_counts.values()) != 48:
        raise ReplayCollisionError("persisted production status sum is not 48")
    for name in (
        "missing_row_ids",
        "extra_experiment_ids",
        "mismatch_row_ids",
        "duplicate_or_gap_row_ids",
    ):
        if getattr(accounting, name) != ():
            raise ReplayCollisionError(
                f"persisted production accounting carries non-empty {name}"
            )
    attempts = tuple(
        parse_persisted_attempt_record(item, plan=plan) for item in snapshot.attempts
    )
    if tuple(item.row_id for item in attempts) != CANONICAL_ROW_ORDER:
        raise ReplayCollisionError("persisted production attempt order differs")
    return attempts


def _require_exact_production_attempt_replay(
    *,
    persisted: tuple[h6a_evidence.AttemptRecord, ...],
    recomputed: tuple[h6a_evidence.AttemptRecord, ...],
) -> None:
    if len(persisted) != 48 or len(recomputed) != 48:
        raise ReplayCollisionError("production replay attempts are not exact 48")
    for stored, current in zip(persisted, recomputed, strict=True):
        if (
            stored.row_id,
            stored.experiment_id,
            stored.retry_index,
            stored.status,
            stored.reason_code,
            stored.fold_evidence_hash,
            stored.run_identity,
        ) != (
            current.row_id,
            current.experiment_id,
            current.retry_index,
            current.status,
            current.reason_code,
            current.fold_evidence_hash,
            current.run_identity,
        ):
            raise ReplayCollisionError(
                f"production replay semantic attempt differs at {stored.row_id}"
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


async def materialize_production(
    *,
    plan: ProductionExecutionPlan,
    authorization: IssuedOneShotAuthorization,
    campaign: ProductionCampaignInput,
    ports: ProductionExecutionPorts,
) -> MaterializationOutcome:
    """Run the actual H4/H6-A/H5 path with H6-B as sole lifecycle owner."""
    state = _CoordinatorState(trace=["preflight"])
    try:
        if type(plan) is not ProductionExecutionPlan:
            raise H6BPreflightRefused("production plan must use the exact sealed type")
        if type(campaign) is not ProductionCampaignInput or campaign.plan is not plan:
            raise H6BPreflightRefused("production campaign is not bound to plan")
        if type(ports) is not ProductionExecutionPorts:
            raise H6BPreflightRefused("production ports must use the exact type")
        validate_exact_48_mapping(plan.ordered_mapping)
        s3_specs, s4_specs = campaign.registration_specs()
        authorization._require_plan(plan)
        state.trace.append("artifact_probe")
        state.artifact_probe += 1
        presence = ports.artifacts.probe(output_dir=plan.output_root)
        state.artifact_state = getattr(presence, "state", "MALFORMED")
        if state.artifact_state not in {"ABSENT", "PAIR_PRESENT"}:
            raise ReplayCollisionError(
                f"production artifact forensic state refused: {state.artifact_state}"
            )
        register_context: ApprovedMutationContext | None = None
        record_context: ApprovedMutationContext | None = None
        if state.artifact_state == "ABSENT":
            register_context, record_context = build_h6a_mutation_contexts(
                authorization
            )
            validate_h6a_context_pair(register_context, record_context)
    except BaseException as exc:
        if type(ports) is ProductionExecutionPorts:
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
        authorization._require_session_target(session)

        state.trace.append("begin")
        state.begin += 1
        await session.begin()
        begun = True
        if state.artifact_state == "PAIR_PRESENT":
            state.trace.append("set_transaction_read_only")
            await session.execute(text("SET TRANSACTION READ ONLY"))
        predecessor_session = _InjectedTransactionSession(session)

        state.trace.append("db_state_inspection")
        state.db_inspect += 1
        snapshot = await ports.state_inspector.inspect(predecessor_session, plan=plan)
        if type(snapshot) is not CampaignDbSnapshot:
            raise ReplayCollisionError(
                "production state inspector returned non-canonical snapshot"
            )
        if snapshot.is_absent():
            state.db_state = "ABSENT"
            if state.artifact_state != "ABSENT":
                raise ReplayCollisionError(
                    "production artifact pair exists while database state is absent"
                )
        else:
            state.db_state = "PRESENT_UNVERIFIED"
            if state.artifact_state != "PAIR_PRESENT":
                raise ReplayCollisionError(
                    "production database state exists while artifact pair is absent"
                )
            state.trace.append("h6a_accounting")
            state.accounting_calls += 1
            state.accounting_report = ports.h6a_accounting.reconstruct(
                plan=plan,
                registered_total=len(snapshot.registered_mapping),
                attempts=snapshot.attempts,
            )
            persisted_attempts = _validate_exact_production_snapshot(
                plan=plan,
                snapshot=snapshot,
                accounting=state.accounting_report,
            )
            state.db_state = "EXACT"

            state.trace.append("h4_attempts")
            state.h4 += 1
            h4_result = await ports.h4_runner.run(plan._identity)
            if type(h4_result) is not ActualH4CampaignResult:
                raise ReplayCollisionError(
                    "actual H4 replay returned wrong concrete type"
                )
            if h4_result.identity is not plan._identity:
                raise ReplayCollisionError("actual H4 replay identity differs")
            _require_exact_production_attempt_replay(
                persisted=persisted_attempts,
                recomputed=h4_result.attempts,
            )

            state.trace.append("h5_scorecard")
            state.h5 += 1
            replay_scorecard = ports.h5.build_scorecard(
                plan=plan,
                h4_result=h4_result,
                accounting=state.accounting_report,
            )
            if type(replay_scorecard) is not dict:
                raise ReplayCollisionError(
                    "actual H5 replay scorecard must be exact dict"
                )
            state.scorecard = replay_scorecard
            state.trace.append("artifact_replay_verify")
            state.replay_verify += 1
            state.replay_inspection = ports.artifacts.inspect(
                scorecard=replay_scorecard,
                output_dir=plan.output_root,
                h5_port=ports.h5,
            )
            return await _finish_replay_noop(state=state, session=session)

        if register_context is None or record_context is None:
            raise H6BPreflightRefused("production mutation contexts are unavailable")

        mapping = dict(plan.ordered_mapping)
        state.trace.append("h6a_register")
        state.register += 1
        register_kwargs: dict[str, object] = {}
        if ports.register_experiments_fn is not None:
            register_kwargs["register_experiments_fn"] = ports.register_experiments_fn
        registered_s3, registered_s4 = await register_h6a_campaign(
            predecessor_session,
            approved=register_context,
            full_campaign_hash=plan.full_campaign_hash,
            campaign_run_id=plan.campaign_run_id,
            s3_specs=list(s3_specs),
            s4_specs=list(s4_specs),
            row_id_to_experiment_id=mapping,
            guard_opt_in_enabled=True,
            guard_policy=campaign.guard_policy,
            **register_kwargs,
        )
        registered = (*registered_s3, *registered_s4)
        pk_mapping = _registered_pk_mapping(plan=plan, registered=registered)

        state.trace.append("h4_attempts")
        state.h4 += 1
        h4_result = await ports.h4_runner.run(plan._identity)
        if type(h4_result) is not ActualH4CampaignResult:
            raise H6BPreflightRefused("actual H4 runner returned wrong concrete type")
        if h4_result.identity is not plan._identity:
            raise H6BPreflightRefused(
                "actual H4 result differs from execution identity"
            )
        attempts = h4_result.batch_items()
        _validate_attempt_batch(plan, attempts)

        state.trace.append("h6a_record")
        state.record += 1
        record_kwargs: dict[str, object] = {}
        if ports.find_existing_trial_fn is not None:
            record_kwargs["find_existing_trial_fn"] = ports.find_existing_trial_fn
        if ports.record_trial_fn is not None:
            record_kwargs["record_trial_fn"] = ports.record_trial_fn
        await record_h6a_attempts(
            predecessor_session,
            approved=record_context,
            full_campaign_hash=plan.full_campaign_hash,
            campaign_run_id=plan.campaign_run_id,
            row_id_to_experiment_id=mapping,
            row_id_to_experiment_pk=pk_mapping,
            attempts=attempts,
            strategy_name=campaign.strategy_name,
            timeframe=campaign.timeframe,
            runner=campaign.runner,
            guard_opt_in_enabled=True,
            guard_policy=campaign.guard_policy,
            **record_kwargs,
        )

        state.trace.append("h6a_accounting")
        state.accounting_calls += 1
        state.accounting_report = ports.h6a_accounting.reconstruct(
            plan=plan, registered_total=len(registered), attempts=attempts
        )

        state.trace.append("h5_scorecard")
        state.h5 += 1
        scorecard = ports.h5.build_scorecard(
            plan=plan,
            h4_result=h4_result,
            accounting=state.accounting_report,
        )
        if type(scorecard) is not dict:
            raise H6BPreflightRefused("actual H5 scorecard must be exact dict")
        state.scorecard = scorecard

        state.trace.append("artifact_stage")
        state.stage += 1
        state.staged_pair = ports.artifacts.stage(
            scorecard=scorecard,
            output_dir=plan.output_root,
            h5_port=ports.h5,
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
            disposition = (
                "COMMIT_FAILED"
                if isinstance(exc, CommitRejectedError)
                else "COMMIT_OUTCOME_UNKNOWN"
            )
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
    except BaseException as exc:
        _capture_materializer_exception(state, ports, exc)
        if primary_error is None:
            primary_error = exc
        if isinstance(exc, ReplayCollisionError):
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
        authorization._require_plan(plan)
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
