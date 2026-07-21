"""Pure H6-B boundary for the additive ROB-974 R3 exact-12 lineage.

This module does not own a session, transaction, artifact, or empirical run.
It seals the already-derived registration surface and independently refuses
partial/tampered persisted state before a later R3 launcher can reuse it.
The frozen R2 H6-B materializer remains a separate, unchanged public path.
"""

from __future__ import annotations

import re
import weakref
from dataclasses import dataclass, field

from app.schemas.research_backtest import StrategyExperimentIdentity
from app.services import rob974_r3_h6a_bridge as h6a
from app.services.research_canonical_hash import canonical_sha256

__all__ = [
    "R3MaterializationContract",
    "R3MaterializationSeal",
    "R3MaterializerContractError",
    "R3PersistedSnapshot",
    "R3ReplayCollisionError",
    "issue_r3_materialization_contract",
    "require_issued_r3_materialization_contract",
    "validate_r3_materialization_registration_surface",
    "validate_r3_persisted_snapshot",
]

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTRACT_SEAL = object()
_STATUS_ORDER: tuple[str, ...] = (
    "completed",
    "rejected",
    "crashed",
    "timeout",
)


class R3MaterializerContractError(ValueError):
    """An R3 materialization contract was malformed or caller-forged."""


class R3ReplayCollisionError(RuntimeError):
    """Persisted R3 state differs from the sealed exact-12 contract."""


def _hex64(value: object, name: str) -> str:
    if type(value) is not str or _HEX64_RE.fullmatch(value) is None:
        raise R3MaterializerContractError(f"{name} must be lowercase 64-hex")
    return value


def _mapping_dict(
    ordered_mapping: tuple[tuple[str, str], ...],
) -> dict[str, str]:
    if type(ordered_mapping) is not tuple:
        raise R3MaterializerContractError("ordered mapping must be an exact tuple")
    for item in ordered_mapping:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
        ):
            raise R3MaterializerContractError(
                "ordered mapping entries must be exact string pairs"
            )
    mapping = dict(ordered_mapping)
    if len(mapping) != len(ordered_mapping):
        raise R3MaterializerContractError("ordered mapping contains duplicate rows")
    try:
        h6a.compute_exact_12_mapping_hash(mapping)
    except h6a.Exact12BatchValidationError as exc:
        raise R3MaterializerContractError(str(exc)) from exc
    return mapping


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class R3MaterializationContract:
    """Code-issued exact-12 H6-B envelope over an H6-A identity surface."""

    full_campaign_hash: str
    campaign_run_id: str
    ordered_mapping: tuple[tuple[str, str], ...]
    exact_12_mapping_hash: str
    registration_surface_hash: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _CONTRACT_SEAL:
            raise R3MaterializerContractError("R3 contract was not code-issued")
        _hex64(self.full_campaign_hash, "full_campaign_hash")
        if self.campaign_run_id != h6a.derive_r3_campaign_run_id(
            self.full_campaign_hash
        ):
            raise R3MaterializerContractError("campaign run ID is not R3-derived")
        mapping = _mapping_dict(self.ordered_mapping)
        if self.exact_12_mapping_hash != h6a.compute_exact_12_mapping_hash(mapping):
            raise R3MaterializerContractError("exact-12 mapping hash differs")
        _hex64(self.registration_surface_hash, "registration_surface_hash")

    @property
    def expected_total(self) -> int:
        return 12

    @property
    def family_counts(self) -> tuple[tuple[str, int], tuple[str, int]]:
        return (("S3", 3), ("S4", 9))


_ISSUED_CONTRACTS: weakref.WeakSet[R3MaterializationContract] = weakref.WeakSet()


def _registration_surface_hash(
    *,
    s3_specs: tuple[StrategyExperimentIdentity, ...],
    s4_specs: tuple[StrategyExperimentIdentity, ...],
    mapping: dict[str, str],
) -> str:
    expected_s3, expected_s4 = h6a.validate_r3_registration_surface(
        s3_specs=s3_specs,
        s4_specs=s4_specs,
        row_id_to_experiment_id=mapping,
    )
    return canonical_sha256(
        {
            "S3": [expected_s3[row_id] for row_id in h6a.R3_CANONICAL_ROW_ORDER[:3]],
            "S4": [expected_s4[row_id] for row_id in h6a.R3_CANONICAL_ROW_ORDER[3:]],
        }
    )


def issue_r3_materialization_contract(
    *,
    full_campaign_hash: str,
    campaign_run_id: str,
    s3_specs: tuple[StrategyExperimentIdentity, ...],
    s4_specs: tuple[StrategyExperimentIdentity, ...],
    row_id_to_experiment_id: dict[str, str],
) -> R3MaterializationContract:
    """Issue a plan only after the full 3+9 registration preflight succeeds."""

    if type(row_id_to_experiment_id) is not dict:
        raise R3MaterializerContractError("R3 mapping must be an exact dict")
    try:
        mapping_hash = h6a.compute_exact_12_mapping_hash(row_id_to_experiment_id)
        surface_hash = _registration_surface_hash(
            s3_specs=s3_specs,
            s4_specs=s4_specs,
            mapping=row_id_to_experiment_id,
        )
    except h6a.Exact12BatchValidationError as exc:
        raise R3MaterializerContractError(str(exc)) from exc
    contract = R3MaterializationContract(
        full_campaign_hash=full_campaign_hash,
        campaign_run_id=campaign_run_id,
        ordered_mapping=tuple(row_id_to_experiment_id.items()),
        exact_12_mapping_hash=mapping_hash,
        registration_surface_hash=surface_hash,
        _seal=_CONTRACT_SEAL,
    )
    _ISSUED_CONTRACTS.add(contract)
    return contract


def require_issued_r3_materialization_contract(
    contract: object,
) -> R3MaterializationContract:
    """Reject value-equal copies and any envelope not returned by the issuer."""

    if (
        type(contract) is not R3MaterializationContract
        or contract not in _ISSUED_CONTRACTS
    ):
        raise R3MaterializerContractError(
            "materialization contract is not an issued live R3 capability"
        )
    return contract


def validate_r3_materialization_registration_surface(
    *,
    contract: R3MaterializationContract,
    s3_specs: tuple[StrategyExperimentIdentity, ...],
    s4_specs: tuple[StrategyExperimentIdentity, ...],
) -> None:
    """Recompute the caller's complete registration surface against the seal."""

    issued = require_issued_r3_materialization_contract(contract)
    mapping = _mapping_dict(issued.ordered_mapping)
    try:
        observed = _registration_surface_hash(
            s3_specs=s3_specs, s4_specs=s4_specs, mapping=mapping
        )
    except h6a.Exact12BatchValidationError as exc:
        raise R3MaterializerContractError(str(exc)) from exc
    if observed != issued.registration_surface_hash:
        raise R3MaterializerContractError("registration surface differs from seal")


@dataclass(frozen=True, slots=True)
class R3PersistedSnapshot:
    """Raw persisted projection; partial state remains representable for refusal."""

    campaign_run_id: str
    registered_mapping: tuple[tuple[str, str], ...]
    attempts: tuple[h6a.R3AttemptBatchItem, ...]
    status_counts: tuple[tuple[str, int], ...]
    mismatch_row_ids: tuple[str, ...] = ()
    out_of_plan_experiment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.campaign_run_id) is not str or not self.campaign_run_id:
            raise R3MaterializerContractError(
                "snapshot campaign_run_id must be an exact non-empty str"
            )
        if type(self.registered_mapping) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
            for item in self.registered_mapping
        ):
            raise R3MaterializerContractError(
                "snapshot registered mapping must contain exact string pairs"
            )
        if type(self.attempts) is not tuple or any(
            type(item) is not h6a.R3AttemptBatchItem for item in self.attempts
        ):
            raise R3MaterializerContractError(
                "snapshot attempts must use exact R3 attempt tuples"
            )
        if type(self.status_counts) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not int
            or item[1] < 0
            for item in self.status_counts
        ):
            raise R3MaterializerContractError(
                "snapshot status counts must be exact non-negative pairs"
            )
        for name in ("mismatch_row_ids", "out_of_plan_experiment_ids"):
            value = getattr(self, name)
            if type(value) is not tuple or any(type(item) is not str for item in value):
                raise R3MaterializerContractError(
                    f"snapshot {name} must be an exact string tuple"
                )


@dataclass(frozen=True, slots=True)
class R3MaterializationSeal:
    full_campaign_hash: str
    campaign_run_id: str
    exact_12_mapping_hash: str
    registered_total: int
    primary_attempts: int
    total_attempts: int
    retry_attempts: int
    status_counts: tuple[tuple[str, int], ...]
    attempt_batch_hash: str


def _validate_attempts(
    *,
    attempts: tuple[h6a.R3AttemptBatchItem, ...],
    mapping: dict[str, str],
) -> None:
    synthetic_pks = {
        row_id: index for index, row_id in enumerate(h6a.R3_CANONICAL_ROW_ORDER, 1)
    }
    try:
        h6a.validate_r3_attempt_surface(
            attempts=attempts,
            row_id_to_experiment_id=mapping,
            row_id_to_experiment_pk=synthetic_pks,
        )
    except h6a.Exact12BatchValidationError as exc:
        raise R3ReplayCollisionError(str(exc)) from exc


def validate_r3_persisted_snapshot(
    *,
    contract: R3MaterializationContract,
    snapshot: R3PersistedSnapshot,
    recomputed_attempts: tuple[h6a.R3AttemptBatchItem, ...],
) -> R3MaterializationSeal:
    """Validate exact persisted accounting and semantic replay, without writes."""

    issued = require_issued_r3_materialization_contract(contract)
    if type(snapshot) is not R3PersistedSnapshot:
        raise R3ReplayCollisionError("snapshot must use the exact R3 snapshot type")
    mapping = _mapping_dict(issued.ordered_mapping)
    if snapshot.campaign_run_id != issued.campaign_run_id:
        raise R3ReplayCollisionError("persisted campaign run ID differs")
    if snapshot.registered_mapping != issued.ordered_mapping:
        raise R3ReplayCollisionError(
            "persisted registration is partial, reordered, or out of plan"
        )
    if snapshot.mismatch_row_ids or snapshot.out_of_plan_experiment_ids:
        raise R3ReplayCollisionError("persisted registration carries foreign rows")
    _validate_attempts(attempts=snapshot.attempts, mapping=mapping)
    _validate_attempts(attempts=recomputed_attempts, mapping=mapping)
    for stored, current in zip(snapshot.attempts, recomputed_attempts, strict=True):
        if (
            stored.row_id != current.row_id
            or stored.fingerprint() != current.fingerprint()
        ):
            raise R3ReplayCollisionError(
                "persisted/recomputed semantic attempt order or evidence differs"
            )
    expected_counts = tuple(
        (status, sum(item.status == status for item in snapshot.attempts))
        for status in _STATUS_ORDER
    )
    if (
        snapshot.status_counts != expected_counts
        or sum(count for _status, count in snapshot.status_counts) != 12
    ):
        raise R3ReplayCollisionError("persisted status-count surface is not exact 12")
    attempt_batch_hash = canonical_sha256(
        [
            {
                "row_id": item.row_id,
                "experiment_id": item.experiment_id,
                "retry_index": item.retry_index,
                "fingerprint": item.fingerprint(),
            }
            for item in snapshot.attempts
        ]
    )
    return R3MaterializationSeal(
        full_campaign_hash=issued.full_campaign_hash,
        campaign_run_id=issued.campaign_run_id,
        exact_12_mapping_hash=issued.exact_12_mapping_hash,
        registered_total=12,
        primary_attempts=12,
        total_attempts=12,
        retry_attempts=0,
        status_counts=expected_counts,
        attempt_batch_hash=attempt_batch_hash,
    )
