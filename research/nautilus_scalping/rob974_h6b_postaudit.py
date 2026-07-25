"""ROB-984 H6-B standalone first-statement READ ONLY post-audit.

The audit owns a separate session, establishes PostgreSQL READ ONLY before
the first campaign query, fetches canonical raw rows once, reconstructs all
accounting through H6-A, and delegates scorecard semantics to H5. It never
commits, repairs, stages, publishes, or invokes a mutation/broker surface.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import rob974_h6a_accounting as h6a_accounting
import rob974_h6a_evidence as h6a_evidence
import rob974_h6b_artifacts as h6b_artifacts
from sqlalchemy import text

from app.services.rob974_h6b_materializer import (
    MATERIALIZED_EXIT,
    POSTAUDIT_FAILURE,
    ActualCampaignStateInspector,
    ActualMergedH5Composition,
    ActualMergedH6AAccounting,
    ContractFixturePlan,
    DatabaseTarget,
    ProductionExecutionPlan,
    parse_persisted_attempt_record,
)

__all__ = [
    "H5PostAuditPort",
    "NamedAttemptEvidenceSeal",
    "PostAuditAuthority",
    "PostAuditCounters",
    "PostAuditError",
    "PostAuditMismatch",
    "PostAuditOutcome",
    "PostAuditPorts",
    "PostAuditPreflightError",
    "ProductionPostAuditAuthority",
    "PostAuditQueryPort",
    "PostAuditRawSnapshot",
    "PostAuditSeal",
    "ReadOnlyQueryViolation",
    "run_contract_fixture_postaudit",
    "run_production_postaudit",
]

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTRACT_FIXTURE_DATABASE = "rob984_contract_fixture_test_db"
_READ_ONLY_SQL = "SET TRANSACTION READ ONLY"
_PROJECT_TEST_DB_TARGET = DatabaseTarget(
    host="localhost", port=5432, database="test_db", user="postgres"
)
_PROJECT_TEST_DB_APPROVAL = "ROB984_CP9_ORCH_PROJECT_TEST_DB"


class PostAuditError(RuntimeError):
    """Base standalone audit failure."""


class PostAuditPreflightError(PostAuditError):
    """Target/plan/port mismatch before a session exists."""


class PostAuditMismatch(PostAuditError):
    """Raw rows, H6-A reconstruction, evidence, or H5 pair differs."""


class ReadOnlyQueryViolation(PostAuditError):
    """The fetch adapter attempted lifecycle ownership or non-SELECT SQL."""


def _hex64(value: object, name: str) -> str:
    if type(value) is not str or _HEX64_RE.fullmatch(value) is None:
        raise PostAuditPreflightError(f"{name} must be lowercase hex64")
    return value


@dataclass(frozen=True, slots=True)
class PostAuditAuthority:
    """Exact CP5 target authority; this checkpoint remains fixture-only."""

    expected_target: DatabaseTarget
    observed_target: DatabaseTarget
    inherited_target: DatabaseTarget | None
    output_dir: Path
    mode: str = "contract_fixture"

    def __post_init__(self) -> None:
        if type(self.expected_target) is not DatabaseTarget:
            raise PostAuditPreflightError("expected target must use exact type")
        if type(self.observed_target) is not DatabaseTarget:
            raise PostAuditPreflightError("observed target must use exact type")
        if (
            self.inherited_target is not None
            and type(self.inherited_target) is not DatabaseTarget
        ):
            raise PostAuditPreflightError(
                "inherited target must use exact type or None"
            )
        if self.mode != "contract_fixture":
            raise PostAuditPreflightError("CP5 authority must remain contract_fixture")
        if not isinstance(self.output_dir, Path) or not self.output_dir.is_absolute():
            raise PostAuditPreflightError("output_dir must be an absolute Path")


@dataclass(frozen=True, slots=True)
class ProductionPostAuditAuthority:
    """Exact CP9 standalone-audit authority for the disposable project DB."""

    expected_target: DatabaseTarget
    observed_target: DatabaseTarget
    inherited_target: DatabaseTarget | None
    output_dir: Path
    approval_source: str = _PROJECT_TEST_DB_APPROVAL

    def __post_init__(self) -> None:
        for name in ("expected_target", "observed_target"):
            if type(getattr(self, name)) is not DatabaseTarget:
                raise PostAuditPreflightError(f"{name} must use exact type")
        if (
            self.inherited_target is not None
            and type(self.inherited_target) is not DatabaseTarget
        ):
            raise PostAuditPreflightError(
                "inherited target must use exact type or None"
            )
        if not isinstance(self.output_dir, Path) or not self.output_dir.is_absolute():
            raise PostAuditPreflightError("output_dir must be an absolute Path")
        if self.approval_source != _PROJECT_TEST_DB_APPROVAL:
            raise PostAuditPreflightError("project test-DB approval source differs")


@dataclass(frozen=True, slots=True)
class PostAuditRawSnapshot:
    """One scoped raw-row fetch; never pre-aggregated SQL counts."""

    full_campaign_hash: str
    campaign_run_id: str
    registered_mapping: tuple[tuple[str, str], ...]
    attempts: tuple[h6a_evidence.AttemptRecord, ...]
    out_of_plan_experiment_ids: tuple[str, ...] = ()
    out_of_campaign_trial_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _hex64(self.full_campaign_hash, "snapshot full campaign hash")
        if type(self.campaign_run_id) is not str or not self.campaign_run_id:
            raise PostAuditPreflightError("snapshot campaign_run_id must be exact str")
        if type(self.registered_mapping) is not tuple:
            raise PostAuditPreflightError("registered mapping must be exact tuple")
        for item in self.registered_mapping:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
            ):
                raise PostAuditPreflightError(
                    "registered mapping entries must be exact string pairs"
                )
        if type(self.attempts) is not tuple or any(
            type(item) is not h6a_evidence.AttemptRecord for item in self.attempts
        ):
            raise PostAuditPreflightError(
                "raw attempts must be exact H6-A AttemptRecord tuples"
            )
        for name in (
            "out_of_plan_experiment_ids",
            "out_of_campaign_trial_ids",
        ):
            values = getattr(self, name)
            if type(values) is not tuple or any(
                type(item) is not str for item in values
            ):
                raise PostAuditPreflightError(f"{name} must be an exact string tuple")


@dataclass(frozen=True, slots=True)
class NamedAttemptEvidenceSeal:
    row_id: str
    unique_hashes: tuple[tuple[str, str], ...]
    ordered_path_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PostAuditSeal:
    full_campaign_hash: str
    campaign_run_id: str
    exact_48_mapping_hash: str
    experiments: int
    trials: int
    strategy_counts: tuple[tuple[str, int], tuple[str, int]]
    primary_attempts: int
    total_attempts: int
    retry_attempts: int
    status_counts: tuple[tuple[str, int], ...]
    out_of_plan_experiments: int
    out_of_campaign_trials: int
    scenario_names: tuple[str, str, str]
    named_evidence: tuple[NamedAttemptEvidenceSeal, ...]
    trial_accounting_hash: str


class PostAuditQueryPort(Protocol):
    provenance: str

    async def fetch_raw_rows(
        self, session: object, *, plan: ContractFixturePlan
    ) -> PostAuditRawSnapshot: ...


class H5PostAuditPort(Protocol):
    provenance: str

    def canonical_json_bytes(self, scorecard: Mapping[str, object]) -> bytes: ...

    def semantic_hash(self, scorecard: Mapping[str, object]) -> str: ...

    def render_markdown(self, scorecard: Mapping[str, object]) -> bytes: ...

    def verify_persisted_scorecard(
        self,
        *,
        scorecard: dict[str, object],
        semantic_hash: str,
        expected: PostAuditSeal,
    ) -> None: ...


SessionFactory = Callable[[], object]


@dataclass(frozen=True, slots=True)
class PostAuditPorts:
    session_factory: SessionFactory
    query: PostAuditQueryPort
    h5: H5PostAuditPort
    provenance: str = "contract_fixture"

    def __post_init__(self) -> None:
        if not callable(self.session_factory):
            raise PostAuditPreflightError("session_factory must be callable")
        if getattr(
            self.query, "provenance", None
        ) != "contract_fixture" or not callable(
            getattr(self.query, "fetch_raw_rows", None)
        ):
            raise PostAuditPreflightError(
                "query port is not contract_fixture raw fetch"
            )
        if getattr(self.h5, "provenance", None) != "contract_fixture":
            raise PostAuditPreflightError("H5 post-audit port is not contract_fixture")
        for name in (
            "canonical_json_bytes",
            "semantic_hash",
            "render_markdown",
            "verify_persisted_scorecard",
        ):
            if not callable(getattr(self.h5, name, None)):
                raise PostAuditPreflightError(f"H5 post-audit port lacks {name}")
        if self.provenance != "contract_fixture":
            raise PostAuditPreflightError("CP5 ports must remain contract_fixture")


@dataclass(frozen=True, slots=True)
class PostAuditCounters:
    session_factory: int
    begin: int
    read_only_statement: int
    query: int
    artifact_read: int
    rollback: int
    close: int
    commit: int = 0
    mutation: int = 0


@dataclass(frozen=True, slots=True)
class PostAuditOutcome:
    exit_code: int
    disposition: str
    trace: tuple[str, ...]
    counters: PostAuditCounters
    primary_error: BaseException | None
    rollback_error: BaseException | None
    close_error: BaseException | None
    seal: PostAuditSeal | None
    persisted_pair: h6b_artifacts.PersistedScorecardPair | None


@dataclass(slots=True)
class _AuditState:
    trace: list[str] = field(default_factory=lambda: ["preflight"])
    session_factory: int = 0
    begin: int = 0
    read_only_statement: int = 0
    query: int = 0
    artifact_read: int = 0
    rollback: int = 0
    close: int = 0
    rollback_error: BaseException | None = None
    close_error: BaseException | None = None
    seal: PostAuditSeal | None = None
    persisted_pair: h6b_artifacts.PersistedScorecardPair | None = None

    def counters(self) -> PostAuditCounters:
        return PostAuditCounters(
            session_factory=self.session_factory,
            begin=self.begin,
            read_only_statement=self.read_only_statement,
            query=self.query,
            artifact_read=self.artifact_read,
            rollback=self.rollback,
            close=self.close,
        )


class _SelectOnlyAuditSession:
    """The query adapter can execute SELECT objects and nothing else."""

    __slots__ = ("__session",)

    def __init__(self, session: object) -> None:
        object.__setattr__(self, "_SelectOnlyAuditSession__session", session)

    async def execute(
        self, statement: object, *args: object, **kwargs: object
    ) -> object:
        if getattr(statement, "is_select", None) is not True:
            raise ReadOnlyQueryViolation(
                "post-audit query adapter may execute SQLAlchemy SELECT objects only"
            )
        return await self.__session.execute(statement, *args, **kwargs)

    def __getattr__(self, name: str) -> object:
        raise ReadOnlyQueryViolation(
            f"post-audit query adapter cannot access session.{name}"
        )


def _validate_preflight(
    *,
    plan: ContractFixturePlan,
    authority: PostAuditAuthority,
    ports: PostAuditPorts,
) -> None:
    if type(plan) is not ContractFixturePlan:
        raise PostAuditPreflightError("plan must be exact ContractFixturePlan")
    if type(authority) is not PostAuditAuthority:
        raise PostAuditPreflightError("authority must be exact PostAuditAuthority")
    if type(ports) is not PostAuditPorts:
        raise PostAuditPreflightError("ports must be exact PostAuditPorts")
    if authority.expected_target != authority.observed_target:
        raise PostAuditPreflightError("observed target differs byte-for-byte")
    if (
        authority.inherited_target is not None
        and authority.inherited_target != authority.expected_target
    ):
        raise PostAuditPreflightError("inherited target conflicts byte-for-byte")
    if authority.expected_target.database != _CONTRACT_FIXTURE_DATABASE:
        raise PostAuditPreflightError(
            "CP5 database must be the exact visibly test-only fixture name"
        )
    if authority.expected_target.database == "rob974_db":
        raise PostAuditPreflightError("rob974_db is forbidden to the worker")
    if len(plan.ordered_mapping) != 48:
        raise PostAuditPreflightError("audit plan must carry exact 48 mapping")


def _build_h6a_seal(
    *, plan: ContractFixturePlan, snapshot: PostAuditRawSnapshot
) -> PostAuditSeal:
    if type(snapshot) is not PostAuditRawSnapshot:
        raise PostAuditMismatch("query returned a non-canonical raw snapshot")
    if snapshot.full_campaign_hash != plan._fixture_campaign_hash:
        raise PostAuditMismatch("raw rows carry the wrong full campaign hash")
    if snapshot.campaign_run_id != plan._fixture_run_id:
        raise PostAuditMismatch("raw rows carry the wrong campaign run ID")
    if snapshot.registered_mapping != plan.ordered_mapping:
        raise PostAuditMismatch(
            "registered strategy/config/experiment mapping is not exact and ordered"
        )
    if snapshot.out_of_plan_experiment_ids:
        raise PostAuditMismatch("out-of-plan experiment rows are present")
    if snapshot.out_of_campaign_trial_ids:
        raise PostAuditMismatch("out-of-campaign trial rows are present")
    if len(snapshot.attempts) != 48:
        raise PostAuditMismatch("raw trial rows must contain exactly 48 attempts")

    expected_row_ids = tuple(row_id for row_id, _ in plan.ordered_mapping)
    expected_experiment_ids = dict(plan.ordered_mapping)
    if tuple(item.row_id for item in snapshot.attempts) != expected_row_ids:
        raise PostAuditMismatch("raw trial rows are missing, extra, or reordered")
    named_evidence: list[NamedAttemptEvidenceSeal] = []
    accounting_rows: list[h6a_accounting.AttemptAccountingRow] = []
    for attempt in snapshot.attempts:
        if attempt.experiment_id != expected_experiment_ids[attempt.row_id]:
            raise PostAuditMismatch("trial experiment ID differs from exact mapping")
        if attempt.campaign_run_id != plan._fixture_run_id:
            raise PostAuditMismatch("trial campaign run ID differs")
        if attempt.full_campaign_hash != plan._fixture_campaign_hash:
            raise PostAuditMismatch("trial full campaign hash differs")
        if attempt.retry_index != 0:
            raise PostAuditMismatch("post-audit requires retry_index=0 for all trials")
        unique_hashes = tuple(
            (item.fold_id, item.content_hash) for item in attempt.unique_evidence
        )
        if tuple(name for name, _ in unique_hashes) != h6a_evidence.FOLD_IDS:
            raise PostAuditMismatch("named unique evidence is missing or reordered")
        path_hashes = tuple(
            (item.path_scenario, item.artifact_hash)
            for item in attempt.path_scenario_evidence
        )
        if tuple(name for name, _ in path_hashes) != h6a_evidence.PATH_SCENARIOS:
            raise PostAuditMismatch("named path evidence is missing or reordered")
        named_evidence.append(
            NamedAttemptEvidenceSeal(
                row_id=attempt.row_id,
                unique_hashes=unique_hashes,
                ordered_path_hashes=path_hashes,
            )
        )
        accounting_rows.append(
            h6a_accounting.AttemptAccountingRow(
                row_id=attempt.row_id,
                experiment_id=attempt.experiment_id,
                retry_index=attempt.retry_index,
                status=attempt.status,
                reason_code=attempt.reason_code,
                fold_evidence_hash=attempt.fold_evidence_hash,
                run_identity=attempt.run_identity,
            )
        )

    accounting = h6a_accounting.build_combined_accounting(
        campaign_run_id=plan._fixture_run_id,
        canonical_row_ids=expected_row_ids,
        row_id_to_experiment_id=expected_experiment_ids,
        registered_total=len(snapshot.registered_mapping),
        attempts=tuple(accounting_rows),
    )
    required = {
        "expected_total": 48,
        "registered_total": 48,
        "primary_attempts": 48,
        "total_attempts": 48,
        "retry_attempts": 0,
        "accounting_complete": True,
    }
    for name, expected in required.items():
        if getattr(accounting, name) != expected:
            raise PostAuditMismatch(f"H6-A accounting field {name} is not exact")
    if sum(accounting.status_counts.values()) != 48:
        raise PostAuditMismatch("H6-A status-count sum must equal 48")
    for name in (
        "missing_row_ids",
        "extra_experiment_ids",
        "mismatch_row_ids",
        "duplicate_or_gap_row_ids",
    ):
        if getattr(accounting, name) != ():
            raise PostAuditMismatch(f"H6-A accounting carries non-empty {name}")

    s3_count = sum(row_id.startswith("S3-") for row_id in expected_row_ids)
    s4_count = sum(row_id.startswith("S4-") for row_id in expected_row_ids)
    if (s3_count, s4_count) != (24, 24):
        raise PostAuditMismatch("strategy experiment split must be exactly 24+24")
    return PostAuditSeal(
        full_campaign_hash=plan._fixture_campaign_hash,
        campaign_run_id=plan._fixture_run_id,
        exact_48_mapping_hash=plan.contract_fixture_mapping_hash,
        experiments=len(snapshot.registered_mapping),
        trials=len(snapshot.attempts),
        strategy_counts=(("S3", s3_count), ("S4", s4_count)),
        primary_attempts=accounting.primary_attempts,
        total_attempts=accounting.total_attempts,
        retry_attempts=accounting.retry_attempts,
        status_counts=tuple(accounting.status_counts.items()),
        out_of_plan_experiments=len(snapshot.out_of_plan_experiment_ids),
        out_of_campaign_trials=len(snapshot.out_of_campaign_trial_ids),
        scenario_names=h6a_evidence.PATH_SCENARIOS,
        named_evidence=tuple(named_evidence),
        trial_accounting_hash=accounting.trial_accounting_hash,
    )


def _outcome(
    state: _AuditState,
    *,
    exit_code: int,
    disposition: str,
    primary_error: BaseException | None,
) -> PostAuditOutcome:
    return PostAuditOutcome(
        exit_code=exit_code,
        disposition=disposition,
        trace=tuple(state.trace),
        counters=state.counters(),
        primary_error=primary_error,
        rollback_error=state.rollback_error,
        close_error=state.close_error,
        seal=state.seal,
        persisted_pair=state.persisted_pair,
    )


async def run_contract_fixture_postaudit(
    *,
    plan: ContractFixturePlan,
    authority: PostAuditAuthority,
    ports: PostAuditPorts,
) -> PostAuditOutcome:
    """Run a standalone audit; success is physical/accounting verification."""
    state = _AuditState()
    try:
        _validate_preflight(plan=plan, authority=authority, ports=ports)
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        return _outcome(
            state,
            exit_code=POSTAUDIT_FAILURE,
            disposition="POSTAUDIT_FAILURE",
            primary_error=exc,
        )

    session: object | None = None
    begun = False
    primary_error: BaseException | None = None
    native_interrupt: BaseException | None = None
    try:
        state.trace.append("session_factory")
        state.session_factory += 1
        session = ports.session_factory()
        if session is None or isinstance(session, Awaitable):
            raise PostAuditPreflightError(
                "session factory must synchronously return one session"
            )
        state.trace.append("begin")
        state.begin += 1
        await session.begin()
        begun = True

        state.trace.append("set_transaction_read_only")
        state.read_only_statement += 1
        await session.execute(text(_READ_ONLY_SQL))

        state.trace.append("fetch_canonical_raw_rows")
        state.query += 1
        snapshot = await ports.query.fetch_raw_rows(
            _SelectOnlyAuditSession(session), plan=plan
        )
        state.trace.append("h6a_reconstruct")
        state.seal = _build_h6a_seal(plan=plan, snapshot=snapshot)

        state.trace.append("physical_scorecard_read")
        state.artifact_read += 1
        state.persisted_pair = h6b_artifacts.read_persisted_scorecard_pair(
            output_dir=authority.output_dir,
            h5_port=ports.h5,
        )
        state.trace.append("h5_scorecard_compare")
        comparison = ports.h5.verify_persisted_scorecard(
            scorecard=state.persisted_pair.parsed_scorecard,
            semantic_hash=state.persisted_pair.semantic_hash,
            expected=state.seal,
        )
        if comparison is not None:
            raise PostAuditMismatch("H5 verification must return None or raise")
    except BaseException as exc:
        primary_error = exc
        if not isinstance(exc, Exception):
            native_interrupt = exc

    if session is None:
        outcome = _outcome(
            state,
            exit_code=POSTAUDIT_FAILURE,
            disposition="POSTAUDIT_FAILURE",
            primary_error=primary_error,
        )
        if native_interrupt is not None:
            native_interrupt.rob984_postaudit_outcome = outcome
            raise native_interrupt
        return outcome

    if begun:
        state.trace.append("rollback_read_only")
        state.rollback += 1
        try:
            await session.rollback()
        except BaseException as exc:
            state.rollback_error = exc
            if primary_error is None:
                primary_error = exc
            if not isinstance(exc, Exception):
                native_interrupt = native_interrupt or exc
    state.trace.append("session_close")
    state.close += 1
    try:
        await session.close()
    except BaseException as exc:
        state.close_error = exc
        if primary_error is None:
            primary_error = exc
        if not isinstance(exc, Exception):
            native_interrupt = native_interrupt or exc

    if primary_error is None:
        outcome = _outcome(
            state,
            exit_code=MATERIALIZED_EXIT,
            disposition="POSTAUDIT_VERIFIED_READ_ONLY",
            primary_error=None,
        )
    else:
        outcome = _outcome(
            state,
            exit_code=POSTAUDIT_FAILURE,
            disposition="POSTAUDIT_FAILURE",
            primary_error=primary_error,
        )
    if native_interrupt is not None:
        native_interrupt.rob984_postaudit_outcome = outcome
        raise native_interrupt
    return outcome


def _validate_production_preflight(
    *,
    plan: ProductionExecutionPlan,
    authority: ProductionPostAuditAuthority,
    session_factory: SessionFactory,
) -> None:
    if type(plan) is not ProductionExecutionPlan:
        raise PostAuditPreflightError("production audit requires exact execution plan")
    if type(authority) is not ProductionPostAuditAuthority:
        raise PostAuditPreflightError("production audit authority must use exact type")
    if not callable(session_factory):
        raise PostAuditPreflightError("production session_factory must be callable")
    if (
        authority.expected_target != _PROJECT_TEST_DB_TARGET
        or authority.observed_target != _PROJECT_TEST_DB_TARGET
    ):
        raise PostAuditPreflightError(
            "production post-audit target differs from reviewed project test_db"
        )
    if authority.inherited_target not in (None, _PROJECT_TEST_DB_TARGET):
        raise PostAuditPreflightError("inherited project test-DB target conflicts")
    if authority.output_dir != plan.output_root:
        raise PostAuditPreflightError("post-audit output differs from execution plan")


def _require_project_test_db_session(session: object) -> None:
    try:
        url = session.get_bind().url
        target = DatabaseTarget(
            host=url.host,
            port=url.port,
            database=url.database,
            user=url.username,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PostAuditPreflightError(
            "post-audit session bind target is unavailable or malformed"
        ) from exc
    if target != _PROJECT_TEST_DB_TARGET:
        raise PostAuditPreflightError(
            "post-audit session bind differs byte-for-byte from approval"
        )


def _build_production_h6a_seal(
    *, plan: ProductionExecutionPlan, snapshot: object
) -> PostAuditSeal:
    from app.services.rob974_h6b_materializer import CampaignDbSnapshot

    if type(snapshot) is not CampaignDbSnapshot:
        raise PostAuditMismatch("actual query returned a non-canonical snapshot")
    if snapshot.campaign_run_id != plan.campaign_run_id:
        raise PostAuditMismatch("raw rows carry the wrong campaign run ID")
    if snapshot.registered_mapping != plan.ordered_mapping:
        raise PostAuditMismatch("registered exact-48 mapping differs")
    if snapshot.mismatch_row_ids:
        raise PostAuditMismatch("out-of-campaign rows are present")
    if snapshot.out_of_plan_experiment_ids:
        raise PostAuditMismatch("out-of-plan experiments are present")
    if len(snapshot.attempts) != 48:
        raise PostAuditMismatch("raw trial rows must contain exactly 48 attempts")

    attempts = tuple(
        parse_persisted_attempt_record(item, plan=plan) for item in snapshot.attempts
    )
    if tuple(attempt.row_id for attempt in attempts) != tuple(
        row_id for row_id, _ in plan.ordered_mapping
    ):
        raise PostAuditMismatch("raw trial rows are missing, extra, or reordered")
    accounting = ActualMergedH6AAccounting().reconstruct(
        plan=plan,
        registered_total=len(snapshot.registered_mapping),
        attempts=snapshot.attempts,
    )
    required = {
        "expected_total": 48,
        "registered_total": 48,
        "primary_attempts": 48,
        "total_attempts": 48,
        "retry_attempts": 0,
        "accounting_complete": True,
    }
    for name, expected in required.items():
        if getattr(accounting, name) != expected:
            raise PostAuditMismatch(f"H6-A accounting field {name} is not exact")
    if sum(accounting.status_counts.values()) != 48:
        raise PostAuditMismatch("H6-A status-count sum must equal 48")
    for name in (
        "missing_row_ids",
        "extra_experiment_ids",
        "mismatch_row_ids",
        "duplicate_or_gap_row_ids",
    ):
        if getattr(accounting, name) != ():
            raise PostAuditMismatch(f"H6-A accounting carries non-empty {name}")

    named: list[NamedAttemptEvidenceSeal] = []
    for attempt in attempts:
        unique_hashes = tuple(
            (row.fold_id, row.content_hash) for row in attempt.unique_evidence
        )
        path_hashes = tuple(
            (row.path_scenario, row.artifact_hash)
            for row in attempt.path_scenario_evidence
        )
        if tuple(name for name, _ in unique_hashes) != h6a_evidence.FOLD_IDS:
            raise PostAuditMismatch("named unique evidence differs")
        if tuple(name for name, _ in path_hashes) != h6a_evidence.PATH_SCENARIOS:
            raise PostAuditMismatch("named path evidence differs")
        named.append(
            NamedAttemptEvidenceSeal(
                row_id=attempt.row_id,
                unique_hashes=unique_hashes,
                ordered_path_hashes=path_hashes,
            )
        )
    return PostAuditSeal(
        full_campaign_hash=plan.full_campaign_hash,
        campaign_run_id=plan.campaign_run_id,
        exact_48_mapping_hash=plan.exact_48_mapping_hash,
        experiments=48,
        trials=48,
        strategy_counts=(("S3", 24), ("S4", 24)),
        primary_attempts=accounting.primary_attempts,
        total_attempts=accounting.total_attempts,
        retry_attempts=accounting.retry_attempts,
        status_counts=tuple(accounting.status_counts.items()),
        out_of_plan_experiments=0,
        out_of_campaign_trials=0,
        scenario_names=h6a_evidence.PATH_SCENARIOS,
        named_evidence=tuple(named),
        trial_accounting_hash=accounting.trial_accounting_hash,
    )


def _verify_production_scorecard(
    *, pair: h6b_artifacts.PersistedScorecardPair, expected: PostAuditSeal
) -> None:
    scorecard = pair.parsed_scorecard
    lineage = scorecard.get("lineage")
    accounting = scorecard.get("h6a_accounting")
    if type(lineage) is not dict or type(accounting) is not dict:
        raise PostAuditMismatch("persisted H5 lineage/accounting shape differs")
    expected_lineage = {
        "full_campaign_hash": expected.full_campaign_hash,
        "campaign_run_id": expected.campaign_run_id,
        "h6a_trial_accounting_hash": expected.trial_accounting_hash,
    }
    for name, value in expected_lineage.items():
        if lineage.get(name) != value:
            raise PostAuditMismatch(f"persisted H5 lineage field {name} differs")
    expected_accounting = {
        "actual_h6a_contract": "PASS",
        "expected_total": 48,
        "registered_total": 48,
        "primary_attempts": 48,
        "retry_attempts": 0,
        "accounting_complete": True,
        "trial_accounting_hash": expected.trial_accounting_hash,
    }
    for name, value in expected_accounting.items():
        if accounting.get(name) != value:
            raise PostAuditMismatch(f"persisted H5 accounting field {name} differs")
    status_counts = accounting.get("status_counts")
    if type(status_counts) is not dict or sum(status_counts.values()) != 48:
        raise PostAuditMismatch("persisted H5 status counts differ")


async def run_production_postaudit(
    *,
    plan: ProductionExecutionPlan,
    authority: ProductionPostAuditAuthority,
    session_factory: SessionFactory,
) -> PostAuditOutcome:
    """Audit committed test-DB state from a separate READ ONLY transaction."""
    state = _AuditState()
    try:
        _validate_production_preflight(
            plan=plan, authority=authority, session_factory=session_factory
        )
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        return _outcome(
            state,
            exit_code=POSTAUDIT_FAILURE,
            disposition="POSTAUDIT_FAILURE",
            primary_error=exc,
        )

    session: object | None = None
    begun = False
    primary_error: BaseException | None = None
    native_interrupt: BaseException | None = None
    try:
        state.trace.append("session_factory")
        state.session_factory += 1
        session = session_factory()
        if session is None or isinstance(session, Awaitable):
            raise PostAuditPreflightError(
                "session factory must synchronously return one session"
            )
        _require_project_test_db_session(session)
        state.trace.append("begin")
        state.begin += 1
        await session.begin()
        begun = True

        state.trace.append("set_transaction_read_only")
        state.read_only_statement += 1
        await session.execute(text(_READ_ONLY_SQL))

        state.trace.append("fetch_canonical_raw_rows")
        state.query += 1
        snapshot = await ActualCampaignStateInspector().inspect(
            _SelectOnlyAuditSession(session), plan=plan
        )
        state.trace.append("h6a_reconstruct")
        state.seal = _build_production_h6a_seal(plan=plan, snapshot=snapshot)

        state.trace.append("physical_scorecard_read")
        state.artifact_read += 1
        state.persisted_pair = h6b_artifacts.read_persisted_scorecard_pair(
            output_dir=authority.output_dir,
            h5_port=ActualMergedH5Composition(),
        )
        state.trace.append("h5_scorecard_compare")
        _verify_production_scorecard(pair=state.persisted_pair, expected=state.seal)
    except BaseException as exc:
        primary_error = exc
        if not isinstance(exc, Exception):
            native_interrupt = exc

    if session is not None and begun:
        state.trace.append("rollback_read_only")
        state.rollback += 1
        try:
            await session.rollback()
        except BaseException as exc:
            state.rollback_error = exc
            if primary_error is None:
                primary_error = exc
            if not isinstance(exc, Exception):
                native_interrupt = native_interrupt or exc
    if session is not None:
        state.trace.append("session_close")
        state.close += 1
        try:
            await session.close()
        except BaseException as exc:
            state.close_error = exc
            if primary_error is None:
                primary_error = exc
            if not isinstance(exc, Exception):
                native_interrupt = native_interrupt or exc

    outcome = _outcome(
        state,
        exit_code=MATERIALIZED_EXIT if primary_error is None else POSTAUDIT_FAILURE,
        disposition=(
            "POSTAUDIT_VERIFIED_READ_ONLY"
            if primary_error is None
            else "POSTAUDIT_FAILURE"
        ),
        primary_error=primary_error,
    )
    if native_interrupt is not None:
        native_interrupt.rob984_postaudit_outcome = outcome
        raise native_interrupt
    return outcome
