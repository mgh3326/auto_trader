"""App-side ROB-974 R3 exact-12 registration and attempt boundary.

The literal R3 roster is deliberately duplicated here.  This module imports
no research module and independently completes all 3+9 preflight checks
before the first family registration primitive or per-attempt DB primitive.
"""

from __future__ import annotations

import base64
import re
import types
from collections.abc import Awaitable, Callable, Mapping
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
from app.services.research_canonical_hash import (
    IDENTITY_COMPONENTS,
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
    "R3_CANONICAL_ROW_ORDER",
    "R3ApprovedMutationContext",
    "R3AttemptBatchItem",
    "Exact12ApprovalContextError",
    "Exact12BatchValidationError",
    "Exact12TerminalEvidenceMismatch",
    "RECORD_R3_ATTEMPTS_OPERATION_KIND",
    "REGISTER_R3_CAMPAIGN_OPERATION_KIND",
    "compute_exact_12_mapping_hash",
    "derive_r3_campaign_run_id",
    "record_r3_attempts",
    "register_r3_campaign",
    "validate_r3_attempt_surface",
    "validate_r3_registration_surface",
]

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_R3_S3_ROW_ORDER: tuple[str, ...] = tuple(f"S3-R3-{index:02d}" for index in range(3))
_R3_S4_ROW_ORDER: tuple[str, ...] = tuple(f"S4-R3-{index:02d}" for index in range(9))
R3_CANONICAL_ROW_ORDER: tuple[str, ...] = _R3_S3_ROW_ORDER + _R3_S4_ROW_ORDER
REGISTER_R3_CAMPAIGN_OPERATION_KIND = "rob974_r3_h6a_register_exact_12"
RECORD_R3_ATTEMPTS_OPERATION_KIND = "rob974_r3_h6a_record_exact_12"
_R3_RUN_ID_PREFIX = "rob974r3-"
_STATUSES: tuple[str, ...] = ("completed", "rejected", "crashed", "timeout")
_REASONS: dict[str, frozenset[str]] = {
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
_IDENTITY_HASH_FIELDS: tuple[str, ...] = tuple(
    f"{component}_hash" for component in IDENTITY_COMPONENTS
)
_FULL_IDENTITY_FIELDS: tuple[str, ...] = (
    "strategy_key",
    "strategy_version",
) + _IDENTITY_HASH_FIELDS
_FAMILY_SEMANTICS: dict[str, tuple[str, str, str, str]] = {
    "S3": (
        "rob974.r3.s3.threshold-relaxation",
        "rob974_r3_s3_gate.v1",
        "rob974.r3.s3.threshold-relaxation",
        "0bdfc36e13057076ce0fdd242c61f13be9e9ec01d78958d426ad4a1f46e7793f",
    ),
    "S4": (
        "rob974.r3.s4.threshold-relaxation",
        "rob974_r3_s4_gate.v1",
        "rob974.r3.s4.threshold-relaxation",
        "75ad9550edcd1571f7b69c686095bbcda8a8163cbd43394ea376118d8be49e27",
    ),
}
_MISSING = object()


class Exact12ApprovalContextError(ValueError):
    """The R3 mutation capability does not bind this exact operation."""


class Exact12BatchValidationError(ValueError):
    """The R3 registration/attempt surface is not literal exact-12."""


class Exact12TerminalEvidenceMismatch(RuntimeError):
    """An exact R3 attempt key already has different semantic evidence."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return types.MappingProxyType(
            {key: _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _unfreeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _unfreeze(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_unfreeze(item) for item in value]
    return value


def _hex64(value: object, name: str) -> str:
    if type(value) is not str or _HEX64_RE.fullmatch(value) is None:
        raise Exact12BatchValidationError(f"{name} must be lowercase 64-hex")
    return value


def compute_exact_12_mapping_hash(row_id_to_experiment_id: dict[str, str]) -> str:
    """Validate literal type, insertion order, IDs, and uniqueness before hash."""

    if type(row_id_to_experiment_id) is not dict:
        raise Exact12BatchValidationError("exact-12 mapping must be an exact dict")
    if tuple(row_id_to_experiment_id) != R3_CANONICAL_ROW_ORDER:
        raise Exact12BatchValidationError(
            "exact-12 mapping order must be S3-R3-00..02,S4-R3-00..08"
        )
    experiment_ids = tuple(row_id_to_experiment_id.values())
    if len(set(experiment_ids)) != 12:
        raise Exact12BatchValidationError("exact-12 experiment IDs must be unique")
    for row_id, experiment_id in row_id_to_experiment_id.items():
        _hex64(experiment_id, f"experiment ID for {row_id!r}")
    return canonical_sha256(row_id_to_experiment_id)


def derive_r3_campaign_run_id(full_campaign_hash: str) -> str:
    _hex64(full_campaign_hash, "full_campaign_hash")
    digest = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "rob974_r3_h6a_primary_run"}
    )
    suffix = base64.urlsafe_b64encode(bytes.fromhex(digest)).decode().rstrip("=")
    return f"{_R3_RUN_ID_PREFIX}{suffix}"


@dataclass(frozen=True, slots=True)
class R3ApprovedMutationContext:
    operation_kind: str
    canonical_plan_hash: str
    derived_run_id: str
    exact_12_mapping_hash: str
    approval_token: str

    def __post_init__(self) -> None:
        if type(self.operation_kind) is not str or not self.operation_kind:
            raise Exact12ApprovalContextError("operation_kind must be non-empty str")
        for name in ("canonical_plan_hash", "exact_12_mapping_hash"):
            value = getattr(self, name)
            if type(value) is not str or _HEX64_RE.fullmatch(value) is None:
                raise Exact12ApprovalContextError(f"{name} must be lowercase 64-hex")
        if type(self.derived_run_id) is not str or not self.derived_run_id:
            raise Exact12ApprovalContextError("derived_run_id must be non-empty str")
        if type(self.approval_token) is not str or not self.approval_token:
            raise Exact12ApprovalContextError("approval_token must be non-empty str")


def _require_approval(
    approved: R3ApprovedMutationContext,
    *,
    operation_kind: str,
    full_campaign_hash: str,
    campaign_run_id: str,
    mapping_hash: str,
) -> None:
    if type(approved) is not R3ApprovedMutationContext:
        raise Exact12ApprovalContextError("approval must use the exact R3 issued type")
    if approved.operation_kind != operation_kind:
        raise Exact12ApprovalContextError("approval operation differs")
    if approved.canonical_plan_hash != full_campaign_hash:
        raise Exact12ApprovalContextError("approval campaign hash differs")
    if approved.derived_run_id != campaign_run_id:
        raise Exact12ApprovalContextError("approval run ID differs")
    if approved.exact_12_mapping_hash != mapping_hash:
        raise Exact12ApprovalContextError("approval exact-12 mapping hash differs")
    if campaign_run_id != derive_r3_campaign_run_id(full_campaign_hash):
        raise Exact12ApprovalContextError("campaign run ID is not R3-derived")


def _spec_row_id(spec: StrategyExperimentIdentity) -> str | None:
    if isinstance(spec.params, Mapping):
        row_id = spec.params.get("row_id")
        return row_id if type(row_id) is str else None
    return None


def _preflight_slice(
    specs: tuple[StrategyExperimentIdentity, ...],
    *,
    expected_order: tuple[str, ...],
    expected_family: str,
    mapping: dict[str, str],
) -> dict[str, dict[str, str]]:
    if type(specs) is not tuple or any(
        type(spec) is not StrategyExperimentIdentity for spec in specs
    ):
        raise Exact12BatchValidationError(
            "R3 registration specs must be exact StrategyExperimentIdentity tuples"
        )
    if tuple(_spec_row_id(spec) for spec in specs) != expected_order:
        raise Exact12BatchValidationError("R3 registration slice order/split drift")
    (
        expected_strategy_key,
        expected_version,
        expected_contract_key,
        expected_contract_hash,
    ) = _FAMILY_SEMANTICS[expected_family]
    expected: dict[str, dict[str, str]] = {}
    for row_id, spec in zip(expected_order, specs, strict=True):
        if (
            spec.strategy_key != expected_strategy_key
            or spec.strategy_version != expected_version
        ):
            raise Exact12BatchValidationError(
                f"{row_id}: strategy key/version differs from R3 {expected_family}"
            )
        if type(spec.strategy) is not dict or (
            spec.strategy.get("slug"),
            spec.strategy.get("lineage"),
            spec.strategy.get("strategy_key"),
            spec.strategy.get("strategy_version"),
        ) != (
            expected_family,
            "R3",
            expected_strategy_key,
            expected_version,
        ):
            raise Exact12BatchValidationError(
                f"{row_id}: strategy component is not semantic R3 {expected_family}"
            )
        if type(spec.code) is not dict or (
            spec.code.get("contract_key") != expected_contract_key
            or spec.code.get("contract_hash") != expected_contract_hash
        ):
            raise Exact12BatchValidationError(
                f"{row_id}: code component is not the R3 {expected_family} contract"
            )
        hashes = compute_identity_hashes(spec.components())
        experiment_id = derive_experiment_id(
            spec.strategy_key, spec.strategy_version, hashes
        )
        if experiment_id != mapping[row_id]:
            raise Exact12BatchValidationError(
                f"{row_id}: spec does not derive the sealed experiment ID"
            )
        expected[row_id] = {
            "experiment_id": experiment_id,
            "strategy_key": spec.strategy_key,
            "strategy_version": spec.strategy_version,
            **hashes,
        }
    return expected


def validate_r3_registration_surface(
    *,
    s3_specs: tuple[StrategyExperimentIdentity, ...],
    s4_specs: tuple[StrategyExperimentIdentity, ...],
    row_id_to_experiment_id: dict[str, str],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Pure full-campaign preflight used before either registration call."""

    compute_exact_12_mapping_hash(row_id_to_experiment_id)
    expected_s3 = _preflight_slice(
        s3_specs,
        expected_order=_R3_S3_ROW_ORDER,
        expected_family="S3",
        mapping=row_id_to_experiment_id,
    )
    expected_s4 = _preflight_slice(
        s4_specs,
        expected_order=_R3_S4_ROW_ORDER,
        expected_family="S4",
        mapping=row_id_to_experiment_id,
    )
    s3_contract_hashes = {spec.code["contract_hash"] for spec in s3_specs}
    s4_contract_hashes = {spec.code["contract_hash"] for spec in s4_specs}
    if (
        len(s3_contract_hashes) != 1
        or len(s4_contract_hashes) != 1
        or s3_contract_hashes == s4_contract_hashes
    ):
        raise Exact12BatchValidationError(
            "R3 family contract hashes must be internally fixed and mutually distinct"
        )
    return expected_s3, expected_s4


def _verify_registered(
    rows: list[ResearchStrategyExperiment],
    *,
    order: tuple[str, ...],
    expected: dict[str, dict[str, str]],
) -> None:
    if type(rows) is not list or len(rows) != len(order):
        raise Exact12BatchValidationError("registration primitive returned wrong count")
    for row_id, row in zip(order, rows, strict=True):
        for field in (*_FULL_IDENTITY_FIELDS, "experiment_id"):
            if getattr(row, field, None) != expected[row_id][field]:
                raise Exact12BatchValidationError(
                    f"registration primitive returned reordered/tampered {row_id}"
                )


RegisterExperimentsFn = Callable[..., Awaitable[list[ResearchStrategyExperiment]]]


async def _default_register_r3_slice(
    session: AsyncSession,
    *,
    specs: list[StrategyExperimentIdentity],
    guard_opt_in_enabled: bool,
    guard_policy: ResearchDbPolicy,
) -> list[ResearchStrategyExperiment]:
    """Register one already-sealed 3/9 slice without touching R2's exact-24 API."""

    if type(specs) is not list or len(specs) not in (3, 9):
        raise Exact12BatchValidationError(
            "default R3 registrar requires exact 3/9 slice"
        )
    target: ResearchDbTarget = resolve_research_db_target(session)
    assert_research_write_authorized(
        opt_in_enabled=guard_opt_in_enabled,
        target=target,
        policy=guard_policy,
    )
    registered: list[ResearchStrategyExperiment] = []
    for spec in specs:
        registered.append(await registry.register_experiment(session, spec))
    return registered


async def register_r3_campaign(
    session: AsyncSession,
    *,
    approved: R3ApprovedMutationContext,
    full_campaign_hash: str,
    campaign_run_id: str,
    s3_specs: tuple[StrategyExperimentIdentity, ...],
    s4_specs: tuple[StrategyExperimentIdentity, ...],
    row_id_to_experiment_id: dict[str, str],
    guard_opt_in_enabled: bool,
    guard_policy: ResearchDbPolicy,
    register_experiments_fn: RegisterExperimentsFn = _default_register_r3_slice,
) -> tuple[list[ResearchStrategyExperiment], list[ResearchStrategyExperiment]]:
    mapping_hash = compute_exact_12_mapping_hash(row_id_to_experiment_id)
    _require_approval(
        approved,
        operation_kind=REGISTER_R3_CAMPAIGN_OPERATION_KIND,
        full_campaign_hash=full_campaign_hash,
        campaign_run_id=campaign_run_id,
        mapping_hash=mapping_hash,
    )
    # Both slices are fully checked before the first await/DB primitive.
    expected_s3, expected_s4 = validate_r3_registration_surface(
        s3_specs=s3_specs,
        s4_specs=s4_specs,
        row_id_to_experiment_id=row_id_to_experiment_id,
    )
    registered_s3 = await register_experiments_fn(
        session,
        specs=list(s3_specs),
        guard_opt_in_enabled=guard_opt_in_enabled,
        guard_policy=guard_policy,
    )
    _verify_registered(registered_s3, order=_R3_S3_ROW_ORDER, expected=expected_s3)
    registered_s4 = await register_experiments_fn(
        session,
        specs=list(s4_specs),
        guard_opt_in_enabled=guard_opt_in_enabled,
        guard_policy=guard_policy,
    )
    _verify_registered(registered_s4, order=_R3_S4_ROW_ORDER, expected=expected_s4)
    return registered_s3, registered_s4


@dataclass(frozen=True, slots=True)
class R3AttemptBatchItem:
    row_id: str
    experiment_id: str
    retry_index: int
    status: str
    reason_code: str | None
    fold_evidence_hash: str
    run_identity: str
    evidence_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.row_id not in R3_CANONICAL_ROW_ORDER:
            raise Exact12BatchValidationError("attempt row_id is outside R3")
        _hex64(self.experiment_id, "experiment_id")
        if type(self.retry_index) is not int or self.retry_index != 0:
            raise Exact12BatchValidationError(
                "R3 primary attempt retry_index must be 0"
            )
        if self.status not in _STATUSES:
            raise Exact12BatchValidationError("attempt status is outside closed set")
        if self.status == "completed":
            if self.reason_code is not None:
                raise Exact12BatchValidationError(
                    "completed attempt reason must be null"
                )
        elif self.reason_code not in _REASONS[self.status]:
            raise Exact12BatchValidationError("attempt reason/status mismatch")
        _hex64(self.fold_evidence_hash, "fold_evidence_hash")
        _hex64(self.run_identity, "run_identity")
        if not isinstance(self.evidence_payload, Mapping):
            raise Exact12BatchValidationError("evidence_payload must be a mapping")
        object.__setattr__(self, "evidence_payload", _freeze(self.evidence_payload))

    def idempotency_key(self, campaign_run_id: str) -> str:
        return f"{campaign_run_id}:{self.experiment_id}:0"

    def fingerprint(self) -> str:
        return canonical_sha256(
            {
                "status": self.status,
                "reason_code": self.reason_code,
                "fold_evidence_hash": self.fold_evidence_hash,
                "run_identity": self.run_identity,
                "evidence_payload": _unfreeze(self.evidence_payload),
            }
        )


def validate_r3_attempt_surface(
    *,
    attempts: tuple[R3AttemptBatchItem, ...],
    row_id_to_experiment_id: dict[str, str],
    row_id_to_experiment_pk: dict[str, int],
) -> None:
    compute_exact_12_mapping_hash(row_id_to_experiment_id)
    if (
        type(attempts) is not tuple
        or len(attempts) != 12
        or any(type(item) is not R3AttemptBatchItem for item in attempts)
    ):
        raise Exact12BatchValidationError("attempt batch must be exact 12-item tuple")
    if tuple(item.row_id for item in attempts) != R3_CANONICAL_ROW_ORDER:
        raise Exact12BatchValidationError("attempt batch order/split drift")
    for item in attempts:
        if item.experiment_id != row_id_to_experiment_id[item.row_id]:
            raise Exact12BatchValidationError("attempt experiment ID differs")
        try:
            item.fingerprint()
        except (TypeError, ValueError) as exc:
            raise Exact12BatchValidationError(
                f"{item.row_id}: attempt evidence payload is not canonical"
            ) from exc
    if (
        type(row_id_to_experiment_pk) is not dict
        or tuple(row_id_to_experiment_pk) != R3_CANONICAL_ROW_ORDER
    ):
        raise Exact12BatchValidationError("experiment PK mapping order differs")
    pks = tuple(row_id_to_experiment_pk.values())
    if any(type(pk) is not int or pk <= 0 for pk in pks) or len(set(pks)) != 12:
        raise Exact12BatchValidationError("experiment PKs must be unique positive ints")


async def _default_find_existing_trial(
    session: AsyncSession, *, experiment_pk: int, idempotency_key: str
) -> ResearchBacktestRun | None:
    return await session.scalar(
        select(ResearchBacktestRun).where(
            ResearchBacktestRun.strategy_experiment_id == experiment_pk,
            ResearchBacktestRun.trial_idempotency_key == idempotency_key,
        )
    )


def _require_stored_attempt_payload(
    row: ResearchBacktestRun,
    *,
    expected_payload: dict[str, Any],
    expected_experiment_pk: int,
    expected_idempotency_key: str,
    expected_status: str,
    row_id: str,
    collision_context: str,
) -> None:
    raw_payload = row.raw_payload
    outer_matches = (
        getattr(row, "strategy_experiment_id", _MISSING) == expected_experiment_pk
        and getattr(row, "trial_idempotency_key", _MISSING)
        == expected_idempotency_key
        and getattr(row, "trial_status", _MISSING) == expected_status
    )
    payload_matches = type(raw_payload) is dict and not any(
        raw_payload.get(key, _MISSING) != value for key, value in expected_payload.items()
    )
    if not outer_matches or not payload_matches:
        raise Exact12TerminalEvidenceMismatch(
            f"{row_id}: {collision_context} exact-12 replay refused"
        )


FindExistingTrialFn = Callable[..., Awaitable[ResearchBacktestRun | None]]
RecordTrialFn = Callable[..., Awaitable[ResearchBacktestRun]]


async def record_r3_attempts(
    session: AsyncSession,
    *,
    approved: R3ApprovedMutationContext,
    full_campaign_hash: str,
    campaign_run_id: str,
    row_id_to_experiment_id: dict[str, str],
    row_id_to_experiment_pk: dict[str, int],
    attempts: tuple[R3AttemptBatchItem, ...],
    strategy_name: str,
    timeframe: str,
    runner: str,
    guard_opt_in_enabled: bool,
    guard_policy: ResearchDbPolicy,
    find_existing_trial_fn: FindExistingTrialFn = _default_find_existing_trial,
    record_trial_fn: RecordTrialFn = registry.record_trial,
) -> list[ResearchBacktestRun]:
    mapping_hash = compute_exact_12_mapping_hash(row_id_to_experiment_id)
    _require_approval(
        approved,
        operation_kind=RECORD_R3_ATTEMPTS_OPERATION_KIND,
        full_campaign_hash=full_campaign_hash,
        campaign_run_id=campaign_run_id,
        mapping_hash=mapping_hash,
    )
    # Complete attempt + PK preflight happens before the first lookup/write.
    validate_r3_attempt_surface(
        attempts=attempts,
        row_id_to_experiment_id=row_id_to_experiment_id,
        row_id_to_experiment_pk=row_id_to_experiment_pk,
    )
    prepared: list[tuple[R3AttemptBatchItem, str, str, BacktestTrialRequest]] = []
    for item in attempts:
        key = item.idempotency_key(campaign_run_id)
        fingerprint = item.fingerprint()
        prepared.append(
            (
                item,
                key,
                fingerprint,
                BacktestTrialRequest(
                    status=item.status,
                    strategy_name=strategy_name,
                    timeframe=timeframe,
                    runner=runner,
                    idempotency_key=key,
                    raw_payload={
                        "r3_h6a_evidence_fingerprint": fingerprint,
                        "full_campaign_hash": full_campaign_hash,
                        "campaign_run_id": campaign_run_id,
                        "exact_12_mapping_hash": mapping_hash,
                        "row_id": item.row_id,
                        "experiment_id": item.experiment_id,
                        "retry_index": 0,
                        "status": item.status,
                        "reason_code": item.reason_code,
                        "fold_evidence_hash": item.fold_evidence_hash,
                        "run_identity": item.run_identity,
                        "evidence_payload": _unfreeze(item.evidence_payload),
                    },
                ),
            )
        )
    target: ResearchDbTarget = resolve_research_db_target(session)
    assert_research_write_authorized(
        opt_in_enabled=guard_opt_in_enabled,
        target=target,
        policy=guard_policy,
    )
    existing_rows: list[ResearchBacktestRun | None] = []
    for item, key, _fingerprint, _request in prepared:
        existing = await find_existing_trial_fn(
            session,
            experiment_pk=row_id_to_experiment_pk[item.row_id],
            idempotency_key=key,
        )
        existing_rows.append(existing)
    for (item, _key, _fingerprint, request), existing in zip(
        prepared, existing_rows, strict=True
    ):
        if existing is not None:
            _require_stored_attempt_payload(
                existing,
                expected_payload=request.raw_payload,
                expected_experiment_pk=row_id_to_experiment_pk[item.row_id],
                expected_idempotency_key=request.idempotency_key,
                expected_status=item.status,
                row_id=item.row_id,
                collision_context="divergent stored",
            )
    existing_count = sum(row is not None for row in existing_rows)
    if existing_count not in (0, 12):
        raise Exact12TerminalEvidenceMismatch(
            "partial exact-12 replay asymmetry refused before append"
        )
    if existing_count == 12:
        return [row for row in existing_rows if row is not None]

    results: list[ResearchBacktestRun] = []
    for (item, _key, _fingerprint, request), existing in zip(
        prepared, existing_rows, strict=True
    ):
        if existing is not None:  # unreachable after the all-or-none check
            raise AssertionError("partial R3 replay escaped asymmetry preflight")
        returned = await record_trial_fn(
            session, experiment_id=item.experiment_id, request=request
        )
        _require_stored_attempt_payload(
            returned,
            expected_payload=request.raw_payload,
            expected_experiment_pk=row_id_to_experiment_pk[item.row_id],
            expected_idempotency_key=request.idempotency_key,
            expected_status=item.status,
            row_id=item.row_id,
            collision_context="concurrent divergent",
        )
        results.append(returned)
    return results
